from __future__ import annotations

import csv
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from windpulse_api.app import create_app
from windpulse_api.catalog import Catalog, empty_stages
from windpulse_api.jobs import BusyError, JobManager


RUN = "a" * 24
ISSUE = "2026-01-31T18:00:00+00:00"


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def table(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def repository(tmp_path):
    config = {"inputs": {"turbine_1": "data/raw/1.csv", "turbine_2": "data/raw/2.csv"},
              "data": {"timezone": "+05:00", "target_min": 0, "target_max": 1, "min_hourly_samples": 5},
              "run": {"output_dir": "artifacts", "issue_hour_local": 23},
              "weather": {"cache_dir": "data/weather"}, "turbines": {}}
    dump(tmp_path / "config.example.json", config)
    directory = tmp_path / "artifacts/runs" / RUN
    manifest = {"run_id": RUN, "issue_time": ISSUE, "horizon": 48, "model": "extra_trees_weather",
                "created_at": "2026-09-23T00:00:03+00:00", "weather_mode": "archived_forecast",
                "checks": {"status": "passed"}, "stale_hours": {"turbine_1": 0, "turbine_2": None},
                "last_observed_hour": {"turbine_1": "2026-01-31T17:00:00+00:00", "turbine_2": None},
                "model_details": {"feature_mode": "archive_weather"}, "config": {"turbines": {}}}
    dump(directory / "manifest.json", manifest)
    from datetime import datetime, timedelta
    issue = datetime.fromisoformat(ISSUE)
    table(directory / "forecast.csv", [{"target_time": (issue + timedelta(hours=h)).isoformat(),
                                        "horizon": h, "turbine_1": h / 100, "turbine_2": h / 100,
                                        "farm_proxy": h / 100} for h in range(1, 49)])
    table(directory / "hourly_inputs.csv", [
        {"timestamp": "2026-01-31T17:00:00+00:00", "turbine_1": 0.2, "turbine_2": 0.4, "farm_proxy": 0.3,
         "turbine_1_available_at": ISSUE, "turbine_2_available_at": "2026-01-31T19:00:00+00:00"},
        {"timestamp": "2026-02-01T00:00:00+00:00", "turbine_1": 0.8, "turbine_2": 0.9, "farm_proxy": 0.85,
         "turbine_1_available_at": "2026-02-01T01:00:00+00:00", "turbine_2_available_at": "2026-02-01T01:00:00+00:00"},
    ])
    dump(tmp_path / "reports/replay_summary.json", [{"run_id": RUN}])
    return tmp_path


@pytest.fixture
def client(repository):
    with TestClient(create_app(repository), base_url="http://localhost") as api:
        yield api


def test_saved_browsing_preserves_nulls_and_never_exposes_future_actuals(client, repository):
    bootstrap = client.get("/api/bootstrap").json()
    assert bootstrap["default_run_id"] == RUN
    assert bootstrap["availability"]["forecast_enabled"] is False
    assert not (repository / "artifacts/web_jobs").exists()
    full = client.get(f"/api/runs/{RUN}").json()
    short = client.get(f"/api/runs/{RUN}?horizon=24").json()
    assert len(full["forecast"]) == 48 and short["forecast"] == full["forecast"][:24]
    assert full["summary"]["stale_hours"] == {"turbine_1": 0, "turbine_2": None}
    assert len(full["actual_history"]) == 1
    assert full["actual_history"][0]["turbine_1"] == 0.2
    assert full["actual_history"][0]["turbine_2"] is None
    assert full["actual_history"][0]["farm_proxy"] is None
    assert all(stage["duration_seconds"] is None for stage in full["stages"])
    original = repository / "artifacts/runs" / RUN / "forecast.csv"
    assert client.get(f"/api/runs/{RUN}/csv").content == original.read_bytes()


def test_run_ids_and_local_requests_are_validated(client):
    assert client.get("/api/runs/not-a-run").status_code == 404
    assert client.get("/api/runs/..%2f..%2fconfig.example.json").status_code == 404
    assert client.get(f"/api/runs/{RUN}?horizon=100").status_code == 422
    assert client.get("/api/unknown").status_code == 404
    assert client.get("/api/bootstrap", headers={"host": "attacker.example"}).status_code == 400
    assert client.post("/api/jobs", headers={"origin": "https://attacker.example"},
                       json={"issue_time": ISSUE}).status_code == 403
    assert client.post("/api/jobs", json={"issue_time": "2026-01-31T18:00:00"}).status_code == 422
    assert client.post("/api/jobs", json={"issue_time": "2026-01-31T18:05:00+00:00"}).status_code == 422
    assert client.post("/api/jobs", json={"issue_time": ISSUE, "weather_mode": "network"}).status_code == 422
    assert client.post("/api/jobs", json={"issue_time": ISSUE, "model": "bogus"}).status_code == 422
    response = client.post("/api/jobs", json={"issue_time": ISSUE})
    assert response.status_code == 409 and "локальных" in response.json()["detail"]["message"]


def test_frontend_built_after_server_start_is_served_safely(client, repository):
    assert client.get("/assets/not-built.js").status_code == 404
    assets = repository / "web/dist/assets"
    assets.mkdir(parents=True)
    (assets / "index.js").write_text("export const ready = true;", encoding="utf-8")
    (assets.parent / "index.html").write_text("<!doctype html><title>WindPulse</title>", encoding="utf-8")
    assert client.get("/assets/index.js").text == "export const ready = true;"
    assert "WindPulse" in client.get("/").text
    assert client.get("/assets/..%2f..%2f..%2fconfig.example.json").status_code == 404


def test_original_events_do_not_use_later_reopen_chain(repository):
    events = []
    for suffix, keys in [("00", ["validate_inputs", "inputs_checked", "acquire_weather", "train_and_predict", "forecast_saved"]),
                         ("01", ["validate_inputs", "inputs_checked", "acquire_weather", "unchanged_inputs"])]:
        for second, key in enumerate(keys):
            events.append({"event": key, "issue": ISSUE, "run_id": RUN,
                           "time": f"2026-09-23T00:{suffix}:0{second}+00:00"})
    (repository / "artifacts/events.jsonl").write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")
    detail = Catalog(repository).detail(RUN)
    assert [row["event"] for row in detail["events"]][-1] == "forecast_saved"
    stages = {row["id"]: row for row in detail["stages"]}
    assert stages["inputs"]["duration_seconds"] == 1
    assert stages["weather"]["duration_seconds"] == 1
    assert all(stages[key]["duration_seconds"] is None for key in ("features", "forecast", "validation", "saving"))


def test_weather_snapshot_is_not_assumed_to_be_model_input(repository):
    path = repository / "artifacts/runs" / RUN / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(model="persistence", model_details={"feature_mode": "observations_only"})
    dump(path, manifest)
    assert Catalog(repository).runs()[0]["weather_used"] is False


def test_quality_uses_saved_metrics_and_missing_control_predictions_stay_missing(repository):
    row = {"model": "persistence", "entity": "farm_proxy", "horizon": "all", "n": 48,
           "mae": 0.123456789, "rmse": 0.3, "bias": -0.07}
    for dataset in ("validation_weather", "holdout"):
        table(repository / "reports" / dataset / "metrics.csv", [row])
        dump(repository / "reports" / dataset / "selection.json", {"n_folds": 1, "first_issue": ISSUE, "last_issue": ISSUE})
    table(repository / "reports/validation_weather/predictions.csv", [
        {"issue_time": ISSUE, "target_time": "2026-01-31T19:00:00+00:00", "model": "persistence",
         "entity": "farm_proxy", "horizon": 1, "prediction": 0.1, "actual": 0.2}])
    datasets = {row["id"]: row for row in Catalog(repository).quality()["datasets"]}
    assert datasets["holdout"]["predictions"] == []
    assert datasets["validation_weather"]["metrics"][0]["mae"] == row["mae"]
    assert datasets["validation_weather"]["metrics"][0]["bias"] == row["bias"]


def test_job_busy_response_and_acceptance_are_nonblocking(client, monkeypatch):
    monkeypatch.setattr(client.app.state.catalog, "availability", lambda: {"forecast_enabled": True})
    def busy(*args):
        raise BusyError("Другой расчёт выполняется")
    monkeypatch.setattr(client.app.state.jobs, "create", busy)
    assert client.post("/api/jobs", json={"issue_time": ISSUE}).status_code == 409
    monkeypatch.setattr(client.app.state.jobs, "create", lambda *args: {"job_id": "b" * 32, "status": "queued"})
    response = client.post("/api/jobs", json={"issue_time": ISSUE, "horizon": 24})
    assert response.status_code == 202 and response.json()["job"]["status"] == "queued"


def test_restart_marks_dead_job_interrupted_and_preserves_live_owner(repository, monkeypatch):
    manager = JobManager(repository, repository / "config.example.json")
    job_id = "b" * 32
    path = manager.directory / job_id / "job.json"
    job = {"job_id": job_id, "status": "running", "worker_pid": 987654321, "stages": empty_stages()}
    job["stages"][0]["status"] = "running"
    dump(path, job)
    dump(manager.lock_path, {"job_id": job_id, "owner_pid": 987654321})
    monkeypatch.setattr("windpulse_api.jobs.process_alive", lambda pid: True)
    manager.recover()
    assert manager.get(job_id)["status"] == "running"
    with pytest.raises(BusyError):
        manager.create(ISSUE, 48, "auto")
    monkeypatch.setattr("windpulse_api.jobs.process_alive", lambda pid: False)
    manager.recover()
    restored = manager.get(job_id)
    assert restored["status"] == "interrupted" and restored["stages"][0]["status"] == "error"
    assert not manager.lock_path.exists()


def test_worker_forces_cache_only_and_records_real_failure(repository, monkeypatch):
    """Isolate the adapter's boundaries: no scientific imports or real network."""
    import requests
    from windpulse_api.worker import execute
    job_id = "c" * 32
    path = repository / "artifacts/web_jobs" / job_id / "job.json"
    dump(path, {"job_id": job_id, "issue_time": ISSUE, "horizon": 48, "model": "auto", "status": "queued",
                "config_path": str(repository / "config.example.json"), "stages": empty_stages(), "events": []})
    parent = types.ModuleType("windforecast")
    pipeline = types.ModuleType("windforecast.pipeline")
    models = types.ModuleType("windforecast.models")
    common = types.ModuleType("windforecast.common")
    weather = types.ModuleType("windforecast.weather")
    class DummyStore:
        def get(self, *args, **kwargs):
            return None
    weather.WeatherStore = DummyStore
    models.Pipeline = type("Pipeline", (), {"fit": lambda *args: None})
    common.read_config = lambda path: {"run": {"output_dir": "artifacts"}}
    pipeline.log_event = lambda *args, **kwargs: None
    pipeline.forecast = lambda *args, **kwargs: None
    pipeline.validate_forecast = lambda *args, **kwargs: None
    calls = []
    def run(config, issue, **kwargs):
        calls.append((config, issue, kwargs))
        pipeline.log_event(None, "validate_inputs", issue=issue)
        pipeline.log_event(None, "inputs_checked", issue=issue)
        pipeline.log_event(None, "acquire_weather", issue=issue)
        with pytest.raises(RuntimeError, match="Сеть отключена"):
            requests.get("https://example.invalid")
        with pytest.raises(RuntimeError, match="только погодный кэш"):
            DummyStore().get(None, None, None, mode="network")
        raise RuntimeError("No verified cached weather: missing source.json")
    pipeline.run_forecast = run
    parent.pipeline, parent.models = pipeline, models
    for name, module in [("windforecast", parent), ("windforecast.pipeline", pipeline), ("windforecast.models", models),
                         ("windforecast.common", common), ("windforecast.weather", weather)]:
        monkeypatch.setitem(sys.modules, name, module)
    # The worker changes this method inside its process; restore it in this in-process test.
    monkeypatch.setattr(requests.sessions.Session, "request", requests.sessions.Session.request)
    execute(repository, job_id)
    job = json.loads(path.read_text(encoding="utf-8"))
    assert Path(calls[0][0]["run"]["output_dir"]) == repository / "artifacts/web_forecasts"
    assert calls[0][2]["weather_mode"] == "cache"
    assert job["status"] == "failed" and "кэш" in job["error"]["message"]
    assert job["stages"][0]["status"] == "completed"
    assert job["stages"][1]["status"] == "error"
    assert job["stages"][1]["duration_seconds"] >= 0
    assert all(stage["status"] == "pending" for stage in job["stages"][2:])
