from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .common import environment, file_hash, source_version, utc
from .models import forecast
from .pipeline import ENTITIES, validate_forecast


def read_snapshot(path):
    frame = pd.read_csv(path, index_col=0, float_precision="round_trip")
    frame.index = pd.to_datetime(frame.index, utc=True)
    for col in frame:
        if col in ("weather_run_time", "weather_available_at", "available_at") or col.endswith("_available_at"):
            frame[col] = pd.to_datetime(frame[col], utc=True)
    return frame


def verify_run(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["artifact_sha256"].items():
        if file_hash(directory / name) != expected:
            raise ValueError(f"Artifact checksum mismatch: {name}")
    if manifest["environment"] != environment() or manifest["source_version"] != source_version():
        raise ValueError("Code/dependencies differ from this run; restore recorded versions before refitting")
    hourly = read_snapshot(directory / "hourly_inputs.csv")
    weather = None
    history = None
    if manifest["weather_mode"] == "archived_forecast":
        weather = {t: read_snapshot(directory / f"weather_current_{t}.csv") for t in ("turbine_1", "turbine_2")}
        history = {}
        for path in sorted(directory.glob("weather_history_*_turbine_1.csv")):
            issue_text = path.name.removeprefix("weather_history_").removesuffix("_turbine_1.csv")
            issue = pd.Timestamp(pd.to_datetime(issue_text, format="%Y%m%dT%H%M")).tz_localize("UTC")
            history[issue] = {t: read_snapshot(directory / f"weather_history_{issue_text}_{t}.csv") for t in weather}
    issue = utc(manifest["issue_time"])
    predictions, _, _ = forecast(hourly, issue, manifest["horizon"], manifest["config"], manifest["model"], weather, history)
    expected = read_snapshot(directory / "forecast.csv")
    validate_forecast(predictions, issue, expected.index, manifest["config"], weather)
    error = np.max(np.abs(predictions[ENTITIES].to_numpy() - expected[ENTITIES].to_numpy()))
    if error > 1e-12:
        raise ValueError(f"Refit differs from saved prediction by {error}")
    return {"status": "reproduced", "run_id": manifest["run_id"], "max_absolute_difference": float(error)}
