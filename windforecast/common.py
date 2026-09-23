from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path

import pandas as pd


def read_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    config["_root"] = str(path.parent)
    d = config["data"]
    if not 1 <= d["min_hourly_samples"] <= 6:
        raise ValueError("min_hourly_samples must be in 1..6")
    if d["target_max"] <= d["target_min"]:
        raise ValueError("Invalid target scale")
    for name, item in config["inputs"].items():
        config["inputs"][name] = str(resolve(config, item))
    d["supplemental_dir"] = str(resolve(config, d.get("supplemental_dir", "data/incoming")))
    return config


def resolve(config, path):
    p = Path(path)
    return p if p.is_absolute() else Path(config.get("_root", ".")) / p


def utc(value, timezone="+05:00"):
    stamp = pd.Timestamp(value)
    return (stamp.tz_localize(timezone) if stamp.tzinfo is None else stamp).tz_convert("UTC")


def targets_for(issue, horizon):
    if issue != issue.floor("h"):
        raise ValueError("Issue time must be exactly on an hour")
    if not 1 <= horizon <= 48:
        raise ValueError("Horizon must be in 1..48")
    return pd.date_range(issue + pd.Timedelta(hours=1), periods=horizon, freq="h", name="target_time")


def daily_issues(start, end, config):
    tz = config["data"]["timezone"]
    first, last = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if first.tzinfo is not None or last.tzinfo is not None:
        raise ValueError("Use local YYYY-MM-DD dates for daily replay")
    if first > last:
        raise ValueError("Start date exceeds end date")
    return [(day + pd.Timedelta(hours=config["run"]["issue_hour_local"])).tz_localize(tz).tz_convert("UTC")
            for day in pd.date_range(first, last, freq="D")]


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def file_hash(path):
    return digest_bytes(Path(path).read_bytes())


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False).encode("utf-8")


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(json_bytes(value))
    os.replace(tmp, path)


def frame_bytes(frame):
    return frame.to_csv(index=True, float_format="%.17g", lineterminator="\n").encode("utf-8")


def source_version():
    root = Path(__file__).parent
    return digest_bytes(b"".join(p.name.encode() + p.read_bytes() for p in sorted(root.glob("*.py"))))


def environment():
    return {name: importlib.metadata.version(name) for name in ["numpy", "pandas", "scikit-learn", "requests", "tzdata", "eccodes"]}
