"""Observation ingestion, UTC alignment, coverage-aware hourly targets and audits.

CSV timestamps are interpreted as interval starts. A ten-minute observation is
available no earlier than the interval end. Original source files are read only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

import numpy as np
import pandas as pd

TURBINES = ("turbine_1", "turbine_2")
MEASUREMENTS = ("power", "wind_speed", "temperature")
CSV_COLUMNS = {
    "ID": "id",
    "Статистическое время": "timestamp",
    "Средняя скорость ветра(m/s)": "wind_speed",
    "Нормализованная активная мощность": "power",
    "Средняя температура окружающей среды(°C)": "temperature",
}


def source_timezone(value: str = "+05:00"):
    """Allow an explicit fixed offset or an IANA timezone; never infer location."""
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if match:
        hours, minutes = int(match[2]), int(match[3])
        if hours > 23 or minutes > 59:
            raise ValueError(f"Invalid fixed timezone offset: {value}")
        offset = timedelta(hours=hours, minutes=minutes)
        return timezone(offset if match[1] == "+" else -offset)
    return value


def utc_timestamp(value: Any, *, naive_timezone: str | None = None) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("Timestamp cannot be missing")
    if timestamp.tzinfo is None:
        if naive_timezone is None:
            raise ValueError("An explicit timezone or UTC offset is required")
        timestamp = timestamp.tz_localize(source_timezone(naive_timezone))
    return timestamp.tz_convert("UTC")


def read_observations(path: str | Path, timezone_name: str = "+05:00") -> pd.DataFrame:
    """Read Russian competition CSV or the canonical supplemental CSV format."""
    path = Path(path)
    raw = pd.read_csv(path, encoding="utf-8-sig").rename(columns=CSV_COLUMNS)
    required = {"timestamp", *MEASUREMENTS}
    if not required.issubset(raw.columns):
        raise ValueError(f"{path}: missing columns {sorted(required - set(raw.columns))}")
    parsed = pd.to_datetime(raw.pop("timestamp"), errors="raise", format="mixed")
    if parsed.isna().any():
        raise ValueError(f"{path}: missing timestamps")
    if parsed.dt.tz is None:
        parsed = parsed.dt.tz_localize(source_timezone(timezone_name), ambiguous="raise", nonexistent="raise")
    index = pd.DatetimeIndex(parsed.dt.tz_convert("UTC"), name="timestamp")
    if index.duplicated().any():
        raise ValueError(f"{path}: duplicate timestamps are ambiguous")
    if ((index.minute % 10 != 0) | (index.second != 0) | (index.microsecond != 0) | (index.nanosecond != 0)).any():
        raise ValueError(f"{path}: observations must fall on a ten-minute grid")
    raw.index = index
    for column in MEASUREMENTS:
        raw[column] = pd.to_numeric(raw[column], errors="raise").astype(float)
    minimum_available = pd.Series(index + pd.Timedelta(minutes=10), index=index)
    if "available_at" in raw:
        available = pd.to_datetime(raw["available_at"], utc=True, errors="raise")
        if available.isna().any():
            raise ValueError(f"{path}: missing observation availability")
        raw["available_at"] = available.where(available >= minimum_available, minimum_available)
    else:
        raw["available_at"] = minimum_available
    frame = raw[[column for column in ("id", *MEASUREMENTS, "available_at") if column in raw]].sort_index()
    frame.attrs["sources"] = [str(path.resolve())]
    return frame


def _merge_observations(frames: list[pd.DataFrame]) -> pd.DataFrame:
    merged = pd.concat(frames).sort_index(kind="stable")
    overlap = merged.index[merged.index.duplicated(keep=False)]
    if len(overlap):
        groups = merged.loc[overlap.unique()].groupby(level=0)
        if (groups[list(MEASUREMENTS)].nunique(dropna=False) > 1).any().any():
            raise ValueError("Conflicting measurement values for the same turbine and timestamp")
        # Identical updates are idempotent; retain the earliest genuine availability.
        merged = merged.sort_values("available_at", kind="stable")
        merged = merged.loc[~merged.index.duplicated(keep="first")].sort_index()
    merged.attrs["sources"] = [source for frame in frames for source in frame.attrs.get("sources", [])]
    return merged


def load_observations(config: Mapping[str, Any], as_of: Any = None) -> dict[str, pd.DataFrame]:
    settings = config.get("data", {})
    zone = settings.get("timezone", "+05:00")
    inputs = config["inputs"]
    directory = Path(settings.get("supplemental_dir", "data/incoming"))
    observations = {}
    for turbine in TURBINES:
        paths = inputs[turbine]
        if isinstance(paths, (str, Path)):
            paths = [paths]
        frames = [read_observations(path, zone) for path in paths]
        frames.extend(read_observations(path, zone) for path in sorted((directory / turbine).glob("*.csv")))
        combined = _merge_observations(frames)
        if as_of is not None:
            cutoff = utc_timestamp(as_of)
            combined = combined.loc[(combined["available_at"] <= cutoff) &
                                    (combined.index + pd.Timedelta(minutes=10) <= cutoff)]
        observations[turbine] = combined
    return observations


def _target_settings(config: Mapping[str, Any]) -> tuple[int, float, float]:
    settings = config.get("data", {})
    minimum = settings.get("min_hourly_samples", 5)
    lower = float(settings.get("target_min", settings.get("power_min", 0)))
    upper = float(settings.get("target_max", settings.get("power_max", 1)))
    if not isinstance(minimum, int) or isinstance(minimum, bool) or not 1 <= minimum <= 6:
        raise ValueError("min_hourly_samples must be an integer between 1 and 6")
    if not np.isfinite([lower, upper]).all() or lower >= upper:
        raise ValueError("target_min must be finite and smaller than target_max")
    return minimum, lower, upper


def hourly_targets(observations: Mapping[str, pd.DataFrame], config: Mapping[str, Any], as_of: Any = None) -> pd.DataFrame:
    """Average valid power only with enough samples; never interpolate or fill zero.

    The farm proxy is an equal-weight index and requires both valid turbine hours.
    `as_of` cuts measurements by availability and excludes every unfinished hour.
    """
    minimum, lower, upper = _target_settings(config)
    cutoff = None if as_of is None else utc_timestamp(as_of)
    targets, coverage, hourly_availability = {}, {}, {}
    for turbine in TURBINES:
        frame = observations[turbine]
        availability = frame.get("available_at", pd.Series(frame.index + pd.Timedelta(minutes=10), index=frame.index))
        if cutoff is not None:
            frame = frame.loc[(availability <= cutoff) & (frame.index + pd.Timedelta(minutes=10) <= cutoff)]
            availability = availability.reindex(frame.index)
        power = frame["power"].where(np.isfinite(frame["power"]) & frame["power"].between(lower, upper))
        count = power.resample("h", label="left", closed="left").count()
        average = power.resample("h", label="left", closed="left").mean().where(count >= minimum)
        available = availability.where(power.notna()).resample("h", label="left", closed="left").max()
        hour_end = pd.Series(available.index + pd.Timedelta(hours=1), index=available.index)
        available = available.where(available >= hour_end, hour_end).where(average.notna())
        if cutoff is not None:
            average = average.loc[average.index + pd.Timedelta(hours=1) <= cutoff]
            count = count.reindex(average.index)
            available = available.reindex(average.index)
        targets[turbine], coverage[turbine] = average, count
        hourly_availability[f"{turbine}_available_at"] = available
    result = pd.DataFrame(targets)
    result["farm_proxy"] = result[list(TURBINES)].mean(axis=1, skipna=False)
    for column, values in hourly_availability.items():
        result[column] = values.reindex(result.index)
    result.index.name = "timestamp"
    result.attrs["coverage"] = pd.DataFrame(coverage)
    result.attrs["aggregation"] = "equal_weight_normalized_index_not_MW"
    return result


def audit_data(observations: Mapping[str, pd.DataFrame], config: Mapping[str, Any]) -> dict[str, Any]:
    """Return JSON-serializable evidence without changing measurements."""
    minimum, lower, upper = _target_settings(config)
    zone = config.get("data", {}).get("timezone", "+05:00")
    audit: dict[str, Any] = {
        "timezone_assumption": zone,
        "timestamp_semantics_assumption": "start of ten-minute interval; available at interval end",
        "aggregation": {"minimum_samples_per_hour": minimum, "expected_samples_per_hour": 6,
                        "power_bounds": [lower, upper], "farm": "equal-weight normalized proxy; not MW"},
        "turbines": {},
    }
    hourly = hourly_targets(observations, config)
    for turbine in TURBINES:
        frame = observations[turbine]
        if frame.empty:
            audit["turbines"][turbine] = {"rows": 0}
            continue
        expected = pd.date_range(frame.index.min(), frame.index.max(), freq="10min")
        missing = expected.difference(frame.index)
        gaps = frame.index.to_series().diff().dt.total_seconds().div(600).sub(1).clip(lower=0)
        numeric = {}
        for column in MEASUREMENTS:
            values = frame[column]
            finite = values.loc[np.isfinite(values)]
            numeric[column] = {
                "missing": int(values.isna().sum()), "nonfinite": int((~np.isfinite(values)).sum()),
                "minimum": float(finite.min()) if len(finite) else None,
                "maximum": float(finite.max()) if len(finite) else None,
                "zero_count": int(values.eq(0).sum()), "negative_count": int(values.lt(0).sum()),
                "quantiles": {str(q): float(value) for q, value in finite.quantile([.01, .5, .99]).items()} if len(finite) else {},
            }
        power = frame["power"]
        audit["turbines"][turbine] = {
            "sources": frame.attrs.get("sources", []), "rows": len(frame),
            "unique_timestamps": int(frame.index.nunique()), "duplicate_timestamps": int(frame.index.duplicated().sum()),
            "start_utc": frame.index.min().isoformat(), "end_utc": frame.index.max().isoformat(),
            "start_local": frame.index.min().tz_convert(source_timezone(zone)).isoformat(),
            "end_local": frame.index.max().tz_convert(source_timezone(zone)).isoformat(),
            "expected_ten_minute_slots": len(expected), "missing_ten_minute_slots": len(missing),
            "longest_gap_missing_slots": int(gaps.max()) if len(frame) > 1 else 0,
            "longest_gap_hours": float(gaps.max() / 6) if len(frame) > 1 else 0,
            "gap_count": int(gaps.gt(0).sum()), "measurement_stats": numeric,
            "power_outside_bounds": int((~power.between(lower, upper) & power.notna()).sum()),
            "valid_hourly_targets": int(hourly[turbine].notna().sum()),
            "hourly_coverage_counts": {str(int(k)): int(v) for k, v in hourly.attrs["coverage"][turbine].value_counts().sort_index().items()},
            "february_2026_rows": int(((frame.index.tz_convert(source_timezone(zone)) >= pd.Timestamp("2026-02-01", tz=source_timezone(zone))) &
                                        (frame.index.tz_convert(source_timezone(zone)) < pd.Timestamp("2026-03-01", tz=source_timezone(zone)))).sum()),
        }
    first, second = (observations[name] for name in TURBINES)
    shared_power = pd.concat([first["power"], second["power"]], axis=1, sort=True).dropna()
    correlation = None
    if len(shared_power) > 1 and shared_power.iloc[:, 0].std() > 0 and shared_power.iloc[:, 1].std() > 0:
        correlation = float(shared_power.iloc[:, 0].corr(shared_power.iloc[:, 1]))
    audit["alignment"] = {
        "shared_timestamps": len(first.index.intersection(second.index)),
        "only_turbine_1": len(first.index.difference(second.index)),
        "only_turbine_2": len(second.index.difference(first.index)),
        "union_timestamps": len(first.index.union(second.index)),
        "hourly_slots": len(hourly), "valid_farm_proxy_hours": int(hourly["farm_proxy"].notna().sum()),
        "power_correlation_on_shared_timestamps": correlation,
    }
    audit["limitations"] = [
        "CSV names advertise 2026-02-28; inspect observed end dates instead.",
        "Neither timezone nor timestamp interval convention is encoded in the CSV.",
        "Normalization denominator and turbine rated capacities are not supplied in the CSV.",
        "Historical availability of initial CSV rows assumes interval end; original delivery timestamps are unknown.",
    ]
    return audit


def ingest_observations(config: Mapping[str, Any], turbine: str, source_path: str | Path,
                        received_at: Any = None) -> dict[str, Any]:
    """Store a validated immutable supplemental batch without editing originals.

    Repeating the same source bytes is a no-op. Backfilled measurements become
    available only at received_at (or their later interval end), including replay.
    """
    if turbine not in TURBINES:
        raise ValueError(f"Unknown turbine: {turbine}")
    received = utc_timestamp(received_at if received_at is not None else datetime.now(timezone.utc))
    source_path = Path(source_path)
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    settings = config.get("data", {})
    directory = Path(settings.get("supplemental_dir", "data/incoming")) / turbine
    destination = directory / f"{source_hash}.csv"
    if destination.exists():
        return {"status": "unchanged", "path": str(destination), "source_sha256": source_hash}
    batch = read_observations(source_path, settings.get("timezone", "+05:00"))
    batch["available_at"] = batch["available_at"].where(batch["available_at"] >= received, received)
    existing = load_observations(config)[turbine]
    merged = _merge_observations([existing, batch])  # Conflict detection before writing.
    directory.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".csv.tmp")
    batch.to_csv(temporary, index_label="timestamp", encoding="utf-8")
    temporary.replace(destination)
    manifest = {"status": "added", "path": str(destination), "source_sha256": source_hash,
                "source_path": str(source_path.resolve()), "received_at": received.isoformat(),
                "batch_rows": len(batch), "new_timestamps": len(merged) - len(existing)}
    destination.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
