"""Isolated adapter: instrument existing calls without modifying model source."""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from .catalog import read_json
from .jobs import JOB_ID, now, write_json


class Recorder:
    def __init__(self, path):
        self.path = path
        self.job = read_json(path)
        self.starts = {}

    def save(self):
        write_json(self.path, self.job)

    def stage(self, key, status, evidence):
        stage = next(item for item in self.job["stages"] if item["id"] == key)
        stage.update(status=status, evidence=evidence)
        if status == "running" and stage["started_at"] is None:
            stage["started_at"] = now()
            self.starts[key] = time.perf_counter()
        if status == "completed":
            stage["finished_at"] = now()
            stage["duration_seconds"] = (time.perf_counter() - self.starts[key]) if key in self.starts else None
        self.save()


def execute(root, job_id):
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("Invalid job ID")
    recorder = Recorder(root / "artifacts/web_jobs" / job_id / "job.json")
    job = recorder.job
    job.update(status="running", started_at=now(), worker_pid=os.getpid())
    recorder.save()
    try:
        # Slow scientific imports occur in the child; browsing never waits for them.
        from windforecast import pipeline, models
        from windforecast.common import read_config
        from windforecast.weather import WeatherStore
        import requests

        config = read_config(job["config_path"])
        config["run"]["output_dir"] = str(root / "artifacts/web_forecasts")

        def deny_network(*args, **kwargs):
            raise RuntimeError("Сеть отключена для веб-расчёта. Требуется локальный погодный кэш.")

        requests.sessions.Session.request = deny_network
        original_get = WeatherStore.get

        def cache_get(self, issue, targets, turbine, mode="cache"):
            if mode != "cache":
                raise RuntimeError("Веб-интерфейс разрешает только погодный кэш.")
            return original_get(self, issue, targets, turbine, mode="cache")

        WeatherStore.get = cache_get
        original_log = pipeline.log_event
        original_forecast = pipeline.forecast
        original_validate = pipeline.validate_forecast
        original_fit = models.Pipeline.fit
        fit_started = False

        def log(root, event, **details):
            original_log(root, event, **details)
            recorder.job["events"].append({"time": now(), "event": event,
                                            **{key: str(value) for key, value in details.items()}})
            if event == "validate_inputs":
                recorder.stage("inputs", "running", "Загрузка измерений и проверка часовых целей.")
            elif event == "inputs_checked":
                recorder.stage("inputs", "completed", "Измерения и доступность часовых целей проверены существующим конвейером.")
            elif event == "acquire_weather":
                recorder.stage("weather", "running", "Проверка точного архивного выпуска и истории обучения в локальном кэше.")
            elif event == "train_and_predict":
                recorder.stage("weather", "completed", "Погодные выпуски доступны; чтение и проверка локального кэша завершены.")
            elif event == "unchanged_inputs":
                recorder.stage("weather", "completed", "Локальный погодный кэш проверен.")
                for key in ("features", "forecast", "validation", "saving"):
                    recorder.stage(key, "completed", "Переиспользован существующий результат с теми же входами; хеши артефактов проверены. Этап повторно не выполнялся.")
            recorder.save()

        def fit(self, *args, **kwargs):
            nonlocal fit_started
            if not fit_started:
                fit_started = True
                recorder.stage("features", "completed", "Подготовлены обучающие и прогнозные признаки; начался первый вызов обучения.")
                recorder.stage("forecast", "running", "Выполняется обучение и прогноз двух турбин существующей моделью.")
            return original_fit(self, *args, **kwargs)

        def forecast(*args, **kwargs):
            if kwargs.get("model_name") == "persistence":
                recorder.stage("features", "completed", "Persistence использует последнее доступное измерение; отдельное построение признаков не требуется.")
                recorder.stage("forecast", "running", "Выполняется существующая базовая модель persistence.")
            else:
                recorder.stage("features", "running", "Существующая модель готовит обучающие примеры и признаки без утечки будущих данных.")
            result = original_forecast(*args, **kwargs)
            recorder.stage("forecast", "completed", "Существующая модель рассчитала прогнозы турбин и общий индекс.")
            return result

        def validate(*args, **kwargs):
            recorder.stage("validation", "running", "Проверка часов, конечности значений, диапазона и происхождения погоды.")
            result = original_validate(*args, **kwargs)
            recorder.stage("validation", "completed", "Все проверки существующего конвейера пройдены.")
            recorder.stage("saving", "running", "Запись прогноза, снимков входных данных, модели и манифеста.")
            return result

        pipeline.log_event = log
        pipeline.forecast = forecast
        pipeline.validate_forecast = validate
        models.Pipeline.fit = fit
        directory = pipeline.run_forecast(config, job["issue_time"], horizon=job["horizon"],
                                         model_name=job["model"], weather_mode="cache")
        saving = next(stage for stage in job["stages"] if stage["id"] == "saving")
        if saving["status"] == "running":
            recorder.stage("saving", "completed", "Прогноз и манифест сохранены; конвейер успешно завершился.")
        job.update(status="completed", finished_at=now(), run_id=directory.name)
        recorder.save()
    except Exception as exc:
        detail = str(exc)
        if "weather" in detail.lower() or "cache" in detail.lower() or "кэш" in detail.lower():
            message = "Для выбранной даты не удалось использовать проверенный погодный кэш."
            action = "Выберите сохранённый выпуск или подготовьте недостающий кэш явной командой из README; приложение ничего не скачивает."
        elif isinstance(exc, FileNotFoundError):
            message = "Не найден необходимый локальный файл."
            action = "Восстановите исходные CSV по инструкции в README или откройте сохранённый результат."
        else:
            message = "Расчёт не завершён."
            action = "Выберите другую дату, проверьте данные и зависимости; подробная причина приведена ниже."
        for stage in job["stages"]:
            if stage["status"] == "running":
                stage.update(status="error", finished_at=now(), evidence=message)
                if stage["id"] in recorder.starts:
                    stage["duration_seconds"] = time.perf_counter() - recorder.starts[stage["id"]]
        job.update(status="failed", finished_at=now(), error={"message": message, "action": action, "detail": detail})
        recorder.save()
    finally:
        lock_path = root / "artifacts/web_jobs/.active.lock"
        lock = read_json(lock_path, {})
        if lock.get("job_id") == job_id:
            lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    execute(args.root.resolve(), args.job)
