from __future__ import annotations

import contextlib
import json
import os
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from .common import (digest_bytes, environment, file_hash, frame_bytes, json_bytes,
                     resolve, save_json, source_version, targets_for, utc)
from .data import hourly_targets, load_observations
from .models import forecast


ENTITIES = ["turbine_1", "turbine_2", "farm_proxy"]


def log_event(root, event, **details):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "events.jsonl").open("a", encoding="utf-8") as out:
        out.write(json_bytes({"time": pd.Timestamp.now(tz="UTC"), "event": event, **details}).decode() + "\n")


@contextlib.contextmanager
def exclusive(root):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".run.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Another run holds {lock}; inspect its PID before removing a stale lock") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def select_model(config, issue, requested, weather_mode):
    if requested != "auto":
        return requested, {"method": "explicit_cli_choice"}
    path = resolve(config, config["run"].get("selection_file", "reports/model_selection.json"))
    if path.exists():
        selection = json.loads(path.read_text(encoding="utf-8"))
        if utc(selection["known_at"]) <= issue:
            saved_config = selection.get("config")
            if not isinstance(saved_config, dict) or not all(key in saved_config for key in ("data", "model", "weather")):
                return "persistence", {"method": "incompatible_validation", "reason": "validation configuration is missing",
                                       "sha256": file_hash(path)}

            def relevant_settings(value):
                # Moving files does not change the fitted procedure or weather location.
                ignored = {"data": {"supplemental_dir"}, "model": set(), "weather": {"cache_dir"}}
                settings = {section: {key: item for key, item in value.get(section, {}).items()
                                      if key not in omitted} for section, omitted in ignored.items()}
                settings["coordinates"] = {name: {key: turbine.get(key) for key in ("latitude", "longitude")}
                                           for name, turbine in value.get("turbines", {}).items()}
                return settings

            previous, current = relevant_settings(saved_config), relevant_settings(config)
            changed = [key for key in current if previous[key] != current[key]]
            if changed:
                return "persistence", {"method": "incompatible_validation", "changed_settings": changed,
                                       "reason": "run validation again for the current settings", "sha256": file_hash(path)}
            name = selection["selected_model"]
            if name.endswith("_weather") and weather_mode == "off":
                raise ValueError("Selected model requires archived weather; choose weather cache/network or an explicit observation model")
            return name, {"method": "past_rolling_validation", "selection": selection, "sha256": file_hash(path)}
    return "persistence", {"method": "baseline_until_past_validation_available"}


def validate_forecast(predictions, issue, targets, config, weather=None):
    if not predictions.index.equals(targets):
        raise ValueError("Forecast hours are missing, duplicated, or misaligned")
    values = predictions[ENTITIES].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("Forecast contains non-finite values")
    low, high = config["data"]["target_min"], config["data"]["target_max"]
    if (values < low - 1e-12).any() or (values > high + 1e-12).any():
        raise ValueError("Forecast is outside the configured normalized range")
    if not np.allclose(predictions.farm_proxy, predictions[["turbine_1", "turbine_2"]].mean(axis=1)):
        raise ValueError("Farm proxy disagrees with equal-weight turbine mean")
    if weather is not None:
        for turbine, frame in weather.items():
            if not frame.index.equals(targets):
                raise ValueError(f"Weather hours missing or misaligned for {turbine}")
            if not np.isfinite(frame[["wind_speed", "temperature"]].to_numpy(dtype=float)).all():
                raise ValueError("Non-finite weather inputs")
            for col in ("weather_run_time", "weather_available_at"):
                dates = pd.to_datetime(frame[col], utc=True)
                if dates.isna().any() or (dates > issue).any():
                    raise ValueError(f"Weather {col} is missing or later than forecast issue")
    return {"status": "passed", "hours": len(targets), "finite": True, "bounds": [low, high],
            "weather_provenance_checked": weather is not None}


def run_forecast(config, issue, horizon=48, model_name="auto", weather_mode="cache", observations=None):
    issue = utc(issue, config["data"]["timezone"])
    targets = targets_for(issue, horizon)
    root = resolve(config, config["run"]["output_dir"])
    with exclusive(root):
        log_event(root, "validate_inputs", issue=issue)
        try:
            observations = load_observations(config) if observations is None else observations
            hourly = hourly_targets(observations, config, as_of=issue)
            input_quality = {}
            for turbine, raw in observations.items():
                known = raw.loc[(raw.index + pd.Timedelta(minutes=10) <= issue) & (raw.available_at <= issue)]
                valid = np.isfinite(known.power) & known.power.between(config["data"]["target_min"], config["data"]["target_max"])
                input_quality[turbine] = {"known_rows": len(known), "excluded_power_rows": int((~valid).sum()),
                                         "valid_hourly_targets": int(hourly[turbine].notna().sum()),
                                         "missing_hourly_targets": int(hourly[turbine].isna().sum())}
            log_event(root, "inputs_checked", issue=issue, quality=input_quality)
            model_name, selection = select_model(config, issue, model_name, weather_mode)
            weather, history = None, None
            weather_blobs = {}
            if weather_mode != "off":
                from .weather import WeatherStore
                store = WeatherStore(config, Path(config.get("_root", ".")))
                log_event(root, "acquire_weather", issue=issue, mode=weather_mode)
                weather = {t: store.get(issue, targets, t, mode=weather_mode) for t in ("turbine_1", "turbine_2")}
                weather_blobs.update({f"current_{t}": frame_bytes(f) for t, f in weather.items()})
                if model_name.endswith("_weather"):
                    history = {}
                    days = int(config["weather"].get("training_days", 45))
                    # Preserve historical training vintages when February actuals are
                    # absent: select days with known labels, not merely last N dates.
                    candidates = []
                    for day in range(int(config["model"].get("train_days", 180)), 0, -1):
                        candidate = issue - pd.Timedelta(days=day)
                        candidate_targets = targets_for(candidate, horizon)
                        if hourly[["turbine_1", "turbine_2"]].reindex(candidate_targets).notna().any().any():
                            candidates.append(candidate)
                    for old_issue in candidates[-days:]:
                        old_targets = targets_for(old_issue, horizon)
                        history[old_issue] = {t: store.get(old_issue, old_targets, t, mode=weather_mode) for t in weather}
                        for t, f in history[old_issue].items():
                            weather_blobs[f"history_{old_issue.strftime('%Y%m%dT%H%M')}_{t}"] = frame_bytes(f)
            elif model_name.endswith("_weather"):
                raise ValueError("Weather model requires --weather cache or network")
            normalized_config = {k: v for k, v in config.items() if not k.startswith("_")}
            snapshot = frame_bytes(hourly)
            provenance = {"issue_time": issue, "horizon": horizon, "model": model_name,
                          "weather_mode": "observation_only" if weather is None else "archived_forecast",
                          "config": normalized_config, "source_version": source_version(),
                          "environment": environment(), "hourly_sha256": digest_bytes(snapshot),
                          "weather_sha256": {k: digest_bytes(v) for k, v in sorted(weather_blobs.items())},
                          "current_weather_provenance": {t: f.attrs.get("weather_manifest", {}) for t, f in (weather or {}).items()},
                          "training_weather_provenance": {stamp.isoformat(): {t: f.attrs.get("weather_manifest", {}) for t, f in frames.items()}
                                                           for stamp, frames in (history or {}).items()},
                          "model_selection": selection}
            run_id = digest_bytes(json_bytes(provenance))[:24]
            directory = root / "runs" / run_id
            manifest_file = directory / "manifest.json"
            if manifest_file.exists():
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                for name, expected in manifest["artifact_sha256"].items():
                    if file_hash(directory / name) != expected:
                        raise ValueError(f"Stored artifact was changed: {directory / name}")
                log_event(root, "unchanged_inputs", issue=issue, run_id=run_id)
                return directory
            log_event(root, "train_and_predict", issue=issue, model=model_name, run_id=run_id)
            predictions, info, fitted = forecast(hourly, issue, horizon, config, model_name=model_name,
                                                 weather=weather, weather_history=history)
            checks = validate_forecast(predictions, issue, targets, config, weather)
            result = predictions[ENTITIES].copy()
            result.insert(0, "horizon", np.arange(1, horizon + 1))
            result.insert(0, "issue_time", issue.isoformat())
            result["model"] = model_name
            result["model_version"] = run_id
            result["forecast_kind"] = provenance["weather_mode"]
            for turbine in ("turbine_1", "turbine_2"):
                for col in ("weather_run_time", "weather_available_at", "weather_source"):
                    result[f"{col}_{turbine}"] = weather[turbine][col] if weather is not None else "not_used"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "forecast.csv").write_bytes(frame_bytes(result))
            (directory / "hourly_inputs.csv").write_bytes(snapshot)
            (directory / "model.pkl").write_bytes(pickle.dumps(fitted, protocol=5))
            for key, blob in weather_blobs.items():
                (directory / f"weather_{key}.csv").write_bytes(blob)
            last_actual = {t: hourly[t].last_valid_index() for t in ("turbine_1", "turbine_2")}
            stale_hours = {t: None if stamp is None else max(0, (issue - stamp - pd.Timedelta(hours=1)).total_seconds() / 3600)
                           for t, stamp in last_actual.items()}
            input_files = {t: [{"path": p, "sha256": file_hash(p)} for p in observations[t].attrs.get("sources", [config["inputs"][t]])]
                           for t in ("turbine_1", "turbine_2")}
            manifest = {**provenance, "run_id": run_id, "created_at": pd.Timestamp.now(tz="UTC"),
                        "input_files": input_files, "input_quality": input_quality, "last_observed_hour": last_actual, "stale_hours": stale_hours,
                        "model_details": info, "checks": checks, "assumptions": config.get("assumptions", []),
                        "weather_manifests": {t: f.attrs.get("weather_manifest", {}) for t, f in (weather or {}).items()},
                        "artifact_sha256": {p.name: file_hash(p) for p in directory.iterdir() if p.is_file()}}
            save_json(manifest_file, manifest)
            log_event(root, "forecast_saved", issue=issue, run_id=run_id, stale_hours=stale_hours)
            return directory
        except Exception as exc:
            log_event(root, "failed", issue=issue, error_type=type(exc).__name__, error=str(exc))
            raise
