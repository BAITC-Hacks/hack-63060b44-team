from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog import Catalog, read_json
from .jobs import BusyError, JobManager

LOCAL_ORIGINS = [f"http://{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (3000, 5173, 8000)]


class ForecastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_time: datetime
    horizon: Literal[24, 48] = 48
    model: Literal["auto", "persistence", "extra_trees", "extra_trees_weather"] = "auto"

    @field_validator("issue_time")
    @classmethod
    def aware_hour(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Укажите часовой пояс, например +05:00.")
        value = value.astimezone(timezone.utc)
        if value.minute or value.second or value.microsecond:
            raise ValueError("Момент выпуска должен приходиться на полный час.")
        return value


def create_app(root: Path | None = None, config_path: Path | None = None):
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    catalog = Catalog(root, config_path)
    jobs = JobManager(root, catalog.config_path)

    @asynccontextmanager
    async def lifespan(app):
        jobs.recover()
        yield

    app = FastAPI(title="WindPulse AI", version="1.0.0", lifespan=lifespan)
    app.state.catalog = catalog
    app.state.jobs = jobs
    app.add_middleware(CORSMiddleware, allow_origins=LOCAL_ORIGINS, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"], allow_credentials=False)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_mutations(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            same_origin = origin == f"{request.url.scheme}://{request.headers.get('host')}"
            if origin and origin not in LOCAL_ORIGINS and not same_origin:
                return JSONResponse(status_code=403, content={"detail": {"message": "Запуск разрешён только из локального интерфейса."}})
            if request.headers.get("sec-fetch-site") == "cross-site" and origin not in LOCAL_ORIGINS:
                return JSONResponse(status_code=403, content={"detail": {"message": "Внешний сайт не может запускать локальные задания."}})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": "historical_cache"}

    @app.get("/api/bootstrap")
    def bootstrap():
        runs = catalog.runs()
        default = "a587f5347a6fc4512eec87fc"
        if default not in {row["run_id"] for row in runs}:
            default = runs[0]["run_id"] if runs else None
        return {"config": catalog.config_summary(), "audit": read_json(root / "reports/data_audit.json"),
                "runs": runs, "default_run_id": default, "availability": catalog.availability(), "active_job": jobs.active()}

    @app.get("/api/runs")
    def runs():
        return {"runs": catalog.runs()}

    def require_run(run_id):
        try:
            return catalog.entry(run_id)
        except KeyError:
            raise HTTPException(404, detail={"message": "Сохранённый выпуск не найден.", "action": "Выберите другой выпуск в истории."})

    @app.get("/api/runs/{run_id}/csv")
    def download(run_id: str):
        directory, _, summary = require_run(run_id)
        return FileResponse(directory / "forecast.csv", media_type="text/csv",
                            filename=f"windpulse-{run_id}-{summary['horizon']}h.csv")

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, horizon: int = Query(48)):
        if horizon not in (24, 48):
            raise HTTPException(422, detail={"message": "Горизонт должен быть 24 или 48 часов."})
        require_run(run_id)
        return catalog.detail(run_id, horizon)

    @app.get("/api/quality")
    def quality():
        return catalog.quality()

    @app.get("/api/data")
    def data():
        return catalog.data()

    @app.post("/api/jobs", status_code=202)
    def start_job(payload: ForecastRequest):
        availability = catalog.availability()
        if not availability["forecast_enabled"]:
            raise HTTPException(409, detail={"message": "Недостаточно локальных данных для нового расчёта.",
                                            "action": availability["message"]})
        try:
            return {"job": jobs.create(payload.issue_time.isoformat(), payload.horizon, payload.model)}
        except BusyError as exc:
            raise HTTPException(409, detail={"message": str(exc), "action": "Дождитесь завершения текущего задания."})

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        try:
            return {"job": jobs.get(job_id)}
        except KeyError:
            raise HTTPException(404, detail={"message": "Задание не найдено."})

    # Unknown API routes must not be masked by the SPA fallback.
    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def unknown_api(path: str):
        raise HTTPException(404, detail={"message": "Такого метода API нет."})

    dist = root / "web/dist"
    @app.get("/assets/{asset_path:path}")
    def asset(asset_path: str):
        directory = (dist / "assets").resolve()
        path = (directory / asset_path).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.get("/windpulse.svg")
    def favicon():
        path = dist / "windpulse.svg"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/svg+xml")

    @app.get("/{path:path}")
    def frontend(path: str):
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return {"service": "WindPulse AI", "mode": "historical_cache",
                "message": "Соберите интерфейс: cd web; npm install; npm run build. Для разработки откройте Vite на порту 5173.",
                "api": "/api/bootstrap"}

    return app


app = create_app()
