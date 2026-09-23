from __future__ import annotations

import csv
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TURBINES = ("turbine_1", "turbine_2")
ENTITIES = (*TURBINES, "farm_proxy")
RUN_ID = re.compile(r"[a-f0-9]{24}\Z")
FEBRUARY_NOTICE = "Фактические измерения за февраль 2026 года не предоставлены. Точность февральского прогноза пока не рассчитана."
STAGES = (
    ("inputs", "Проверка исходных данных"),
    ("weather", "Выбор доступного погодного выпуска"),
    ("features", "Подготовка признаков"),
    ("forecast", "Расчёт прогноза"),
    ("validation", "Проверка результата"),
    ("saving", "Сохранение прогноза"),
)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def stamp(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(value):
    return stamp(value).isoformat() if value else None


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def csv_rows(path):
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def empty_stages():
    return [{"id": key, "label": label, "status": "pending", "started_at": None,
             "finished_at": None, "duration_seconds": None, "evidence": "Этап ещё не выполнялся."}
            for key, label in STAGES]


class Catalog:
    """Serve only run IDs discovered inside approved local artifact directories.

    Browsing needs neither pandas nor a fitted model and never deserializes pickle.
    Saved report metrics are read verbatim, with no recomputation or adjustment.
    """

    def __init__(self, root: Path, config_path: Path | None = None):
        self.root = Path(root).resolve()
        self.config_path = (config_path or self.root / "config.example.json").resolve()
        self.config = read_json(self.config_path, {})
        self._index = {}
        self._refreshed = 0.0

    def refresh(self, force=False):
        if not force and time.monotonic() - self._refreshed < 2:
            return
        artifact_root = self.root / "artifacts"
        roots = [artifact_root / "runs", *sorted(artifact_root.glob("*/runs"))]
        replay = read_json(self.root / "reports/replay_summary.json", [])
        replay_ids = {row.get("run_id") for row in replay if isinstance(row, dict)}
        found = {}
        for runs in roots:
            for directory in sorted(runs.glob("*")):
                if not RUN_ID.fullmatch(directory.name) or not directory.is_dir():
                    continue
                if not directory.resolve().is_relative_to(artifact_root.resolve()):
                    continue
                manifest = read_json(directory / "manifest.json")
                if not isinstance(manifest, dict) or manifest.get("run_id") != directory.name:
                    continue
                if not (directory / "forecast.csv").is_file():
                    continue
                summary = {
                    "run_id": directory.name, "issue_time": iso(manifest["issue_time"]),
                    "horizon": manifest["horizon"], "model": manifest["model"],
                    "status": manifest.get("checks", {}).get("status", "unknown"),
                    "stale_hours": {t: manifest.get("stale_hours", {}).get(t) for t in TURBINES},
                    "last_observed_hour": {t: iso(manifest.get("last_observed_hour", {}).get(t)) for t in TURBINES},
                    "created_at": iso(manifest.get("created_at")),
                    "is_replay": directory.name in replay_ids,
                    "weather_used": manifest.get("model_details", {}).get("feature_mode") == "archive_weather",
                    "collection": str(runs.relative_to(artifact_root)).replace("\\", "/"),
                    "csv_url": f"/api/runs/{directory.name}/csv",
                }
                found[directory.name] = (directory, manifest, summary)
        self._index = found
        self._refreshed = time.monotonic()

    def runs(self):
        self.refresh()
        return sorted((entry[2] for entry in self._index.values()),
                      key=lambda row: (row["issue_time"], row["model"], row["run_id"]), reverse=True)

    def entry(self, run_id):
        self.refresh()
        if not RUN_ID.fullmatch(run_id) or run_id not in self._index:
            raise KeyError(run_id)
        return self._index[run_id]

    def config_summary(self):
        config = self.config
        data = config.get("data", {})
        return {"timezone": data.get("timezone", "+05:00"),
                "issue_hour_local": config.get("run", {}).get("issue_hour_local", 23),
                "target_min": data.get("target_min"), "target_max": data.get("target_max"),
                "min_hourly_samples": data.get("min_hourly_samples"),
                "turbines": config.get("turbines", {}), "weather": config.get("weather", {}),
                "models": ["auto", "persistence", "extra_trees", "extra_trees_weather"]}

    def availability(self):
        inputs = self.config.get("inputs", {})
        raw = bool(inputs) and all((self.config_path.parent / value).is_file() for value in inputs.values())
        cache = self.config_path.parent / self.config.get("weather", {}).get("cache_dir", "data/weather")
        cached = cache.is_dir() and any(cache.glob("noaa_gfs/*/*/f*/source.json"))
        enabled = raw and cached
        return {"saved_runs": len(self.runs()), "forecast_enabled": enabled,
                "raw_inputs_present": raw, "weather_cache_present": cached, "mode": "historical_cache",
                "message": "Расчёт использует только локальный погодный кэш; доступность выбранной даты проверяется при запуске."
                if enabled else "Для нового расчёта нужны исходные CSV и погодный кэш. Сохранённые результаты доступны без обучения."}

    def events_for(self, directory, manifest):
        # Choose the original recorded execution, not a later cache-hit replay.
        log = directory.parent.parent / "events.jsonl"
        if not log.is_file():
            return []
        executions, current = [], []
        issue = stamp(manifest["issue_time"])
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                if stamp(event.get("issue")) != issue:
                    continue
            except (ValueError, TypeError):
                continue
            if event.get("event") == "validate_inputs":
                current = []
            current.append(event)
            if event.get("run_id") == manifest["run_id"] and event.get("event") == "forecast_saved":
                executions.append(current[:])
        return executions[0] if executions else []

    def saved_stages(self, manifest, events):
        by_event = {event["event"]: event for event in events}
        stages = empty_stages()
        for stage in stages:
            stage.update(status="completed", evidence="Подтверждено сохранённым артефактом. Отдельное время этапа не записано.")
        boundaries = {"inputs": ("validate_inputs", "inputs_checked"),
                      "weather": ("acquire_weather", "train_and_predict")}
        for stage in stages:
            pair = boundaries.get(stage["id"])
            if pair and all(event in by_event for event in pair):
                start, finish = (iso(by_event[event]["time"]) for event in pair)
                stage.update(started_at=start, finished_at=finish,
                             duration_seconds=max(0, (stamp(finish) - stamp(start)).total_seconds()),
                             evidence="Начало и завершение зафиксированы в сохранённом журнале.")
            if stage["id"] in {"features", "forecast"}:
                stage["evidence"] = "Подготовка признаков и обучение записаны общим событием train_and_predict; отдельные длительности неизвестны."
            if stage["id"] == "validation":
                stage["evidence"] = "Сохранённые проверки результата: " + manifest.get("checks", {}).get("status", "unknown")
                stage["status"] = "completed" if manifest.get("checks", {}).get("status") == "passed" else "pending"
            if stage["id"] == "saving":
                event = by_event.get("forecast_saved")
                stage["finished_at"] = iso(event["time"]) if event else iso(manifest.get("created_at"))
                stage["evidence"] = "Сохранены прогноз CSV и манифест происхождения. Начало сохранения отдельно не записано."
            if stage["id"] == "weather" and manifest.get("weather_mode") == "observation_only":
                stage.update(status="completed", evidence="Этот сохранённый запуск использовал только измерения; погода не требовалась.")
        return stages

    def detail(self, run_id, horizon=48):
        directory, manifest, summary = self.entry(run_id)
        forecast = []
        for row in csv_rows(directory / "forecast.csv"):
            if int(row["horizon"]) <= horizon:
                forecast.append({"target_time": iso(row["target_time"]), "horizon": int(row["horizon"]),
                                 **{entity: number(row.get(entity)) for entity in ENTITIES}})
        issue = stamp(manifest["issue_time"])
        history = []
        for row in csv_rows(directory / "hourly_inputs.csv"):
            target = stamp(row.get("timestamp", row.get("target_time")))
            if target is None or not issue - timedelta(hours=24) <= target < issue:
                continue
            actual = {"target_time": target.isoformat()}
            for turbine in TURBINES:
                available = stamp(row.get(f"{turbine}_available_at"))
                actual[turbine] = (number(row.get(turbine)) if available and available <= issue
                                   and target + timedelta(hours=1) <= issue else None)
            actual["farm_proxy"] = (sum(actual[t] for t in TURBINES) / 2
                                    if all(actual[t] is not None for t in TURBINES) else None)
            history.append(actual)
        weather, metadata = {}, {}
        for turbine in TURBINES:
            weather[turbine] = [{**{key: iso(row[key]) for key in ("target_time", "weather_run_time", "weather_available_at")},
                                 "wind_speed": number(row.get("wind_speed")), "temperature": number(row.get("temperature")),
                                 "weather_source": row.get("weather_source")}
                                for row in csv_rows(directory / f"weather_current_{turbine}.csv")[:horizon]]
            saved = manifest.get("weather_manifests", {}).get(turbine, {})
            metadata[turbine] = {key: value for key, value in saved.items() if key != "files"}
            if saved.get("provider") == "noaa_gfs":
                metadata[turbine].update(wind_height_m=10, temperature_height_m=2,
                                         resolution_warning="Сетка GFS 1° грубая для рельефа площадки; ближайший узел одинаков для обеих турбин.")
        events = self.events_for(directory, manifest)
        stages = self.saved_stages(manifest, events)
        if directory.parent.parent.name == "web_forecasts":
            # New web runs have measured stage boundaries. Keep them available
            # when their result is opened later, without replaying any animation.
            for path in sorted((self.root / "artifacts/web_jobs").glob("*/job.json")):
                job = read_json(path, {})
                recorded = job.get("events", [])
                if (job.get("status") == "completed" and job.get("run_id") == run_id
                        and any(event.get("event") == "forecast_saved" for event in recorded)):
                    stages = job.get("stages", stages)
                    break
        provenance = {key: manifest.get(key) for key in ("run_id", "source_version", "hourly_sha256", "model_details",
                                                        "checks", "model_selection", "assumptions", "input_quality")}
        provenance["forecast_sha256"] = manifest.get("artifact_sha256", {}).get("forecast.csv")
        return {"summary": summary, "forecast": forecast, "actual_history": history,
                "weather": weather, "weather_metadata": metadata, "stages": stages,
                "events": events, "provenance": provenance, "turbines": manifest.get("config", {}).get("turbines", {})}

    def quality(self):
        datasets = []
        definitions = [("validation_weather", "Выбор модели · 7 выпусков", "selection"),
                       ("pre_holdout_selection", "Выбор перед контрольной проверкой · 5 выпусков", "selection"),
                       ("holdout", "Контрольная проверка · 1 выпуск", "control"),
                       ("validation_observations", "Модели без погоды · 13 выпусков", "selection"),
                       ("validation_coverage6", "Покрытие 6/6 · 13 выпусков", "sensitivity")]
        for dataset_id, label, role in definitions:
            directory = self.root / "reports" / dataset_id
            if not (directory / "metrics.csv").is_file():
                continue
            selection = read_json(directory / "selection.json", {})
            predictions = []
            source = directory / "predictions.csv"
            if not source.exists() and dataset_id == "pre_holdout_selection":
                source = self.root / "reports/validation_weather/predictions.csv"
            rows = csv_rows(source)
            for row in rows:
                if dataset_id == "pre_holdout_selection" and stamp(row["issue_time"]) > stamp("2026-01-26T18:00:00+00:00"):
                    continue
                predictions.append({**{key: row[key] for key in ("model", "entity")},
                                    "issue_time": iso(row["issue_time"]), "target_time": iso(row["target_time"]),
                                    "horizon": int(row["horizon"]), "prediction": number(row["prediction"]), "actual": number(row["actual"])})
            issues = sorted({row["issue_time"] for row in predictions})
            metrics = [{key: value if key in ("model", "entity", "horizon") else number(value)
                        for key, value in row.items()} for row in csv_rows(directory / "metrics.csv")]
            note = ("Один исторический 48-часовой выпуск. Модель выбрана на первых пяти выпусках; целевые часы этих групп не пересекаются."
                    if role == "control" else "Временная валидация для выбора модели; не является оценкой точности февральского прогноза.")
            datasets.append({"id": dataset_id, "label": label, "role": role,
                             "first_issue": iso(selection.get("first_issue")) or (issues[0] if issues else None),
                             "last_issue": iso(selection.get("last_issue")) or (issues[-1] if issues else None),
                             "n_folds": selection.get("n_folds", len(issues)), "selected_model": selection.get("selected_model"),
                             "metrics": metrics, "predictions": predictions, "note": note})
        return {"datasets": datasets, "february_notice": FEBRUARY_NOTICE,
                "control_selection": read_json(self.root / "reports/pre_holdout_selection/selection.json", {})}

    def data(self):
        return {"audit": read_json(self.root / "reports/data_audit.json"), "timezone": self.config.get("data", {}).get("timezone", "+05:00"),
                "aggregation_rule": "Среднее корректных десятиминутных значений за час при наличии не менее 5 из 6 отсчётов. Время строки — начало интервала; значение доступно после его завершения.",
                "farm_definition": "Общий индекс — среднее двух нормализованных значений с равными весами. Это безразмерная шкала 0–1; номиналы и физическая нормализация не подтверждены.",
                "assumptions": ["Для всех исходных меток времени принят фиксированный UTC+05:00.",
                                "Метка исходной строки означает начало десятиминутного интервала.",
                                "Исходное измерение считается доступным в конце интервала; журнала фактической доставки нет.",
                                "Мощность нормализована в диапазоне 0–1; знаменатель нормализации и номиналы неизвестны.",
                                "Общий индекс — среднее двух нормализованных турбин с одинаковыми весами."],
                "weather_limitations": ["NOAA GFS: архивные прогнозы, сетка 1°, ветер на высоте 10 м, температура на высоте 2 м.",
                                        "Обе площадки попадают в один ближайший узел сетки; горный рельеф описывается грубо.",
                                        "Исходный шаг 3 часа; почасовые значения получены линейной интерполяцией U/V ветра и температуры.",
                                        "Время Last-Modified ограничивает доступность версии объекта, но не доказывает доставку прогноза команде в прошлом.",
                                        "Валидация получает новые измерения ежедневно и не доказывает качество при месяце без обновления фактов."],
                "questions": ["Какой часовой пояс и его изменения использовались регистратором?",
                              "Метка времени обозначает начало или конец интервала?",
                              "Как определена нормализация и каковы номинальные мощности турбин?",
                              "Каков точный час выпуска и крайний срок передачи конкурсного прогноза?",
                              "Как определены конкурсная цель, единицы и формат выходного файла?",
                              "Будут ли предоставлены фактические измерения за февраль и журналы их доступности?"],
                "february_notice": FEBRUARY_NOTICE}
