"""Deterministic direct multi-horizon models with explicit information cutoffs.

The hourly index denotes the *start* of an observation interval. An interval is
available only when ``start + 1 hour <= issue``. Targets are issue + 1..48 hours.
Training rows emulate daily issues, including the information cutoff for every
row. No future actual wind, temperature, or power enters the feature matrix.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline


MODEL_VERSION = "direct-extra-trees-v1"
TURBINES = ("turbine_1", "turbine_2")
SERIES = (*TURBINES, "farm_proxy")
WEATHER_COLUMNS = (
    "wind_speed", "temperature", "weather_run_time", "weather_available_at",
    "weather_source",
)


class ForecastModelError(ValueError):
    """A requested forecast cannot be produced without violating its contract."""


def _utc(value: Any, name: str) -> pd.Timestamp:
    value = pd.Timestamp(value)
    if pd.isna(value) or value.tzinfo is None:
        raise ForecastModelError(f"{name} must be an explicit timezone-aware timestamp")
    return value.tz_convert("UTC")


def forecast_targets(issue: Any, horizon: int) -> pd.DatetimeIndex:
    """Return the declared competition convention: issue + 1h through + Hh."""
    issue = _utc(issue, "issue")
    if issue != issue.floor("h"):
        raise ForecastModelError("issue must be aligned to a full hour")
    if isinstance(horizon, bool) or int(horizon) != horizon or not 1 <= horizon <= 48:
        raise ForecastModelError("horizon must be an integer between 1 and 48")
    return pd.date_range(issue + pd.Timedelta(hours=1), periods=int(horizon), freq="h", name="target_time")


def _settings(config: Mapping) -> tuple[dict, tuple[float, float], np.ndarray]:
    settings = dict(config.get("model", {}))
    data = config.get("data", {})
    bounds = data.get("power_bounds", [data.get("power_min", data.get("target_min", 0)),
                                      data.get("power_max", data.get("target_max", 1))])
    lower, upper = map(float, bounds)
    if not np.isfinite([lower, upper]).all() or lower >= upper:
        raise ForecastModelError("target bounds must be finite and strictly increasing")
    weights = np.asarray(data.get("turbine_weights", [1, 1]), dtype=float)
    if weights.shape != (2,) or not np.isfinite(weights).all() or (weights <= 0).any():
        raise ForecastModelError("data.turbine_weights must contain two positive finite numbers")
    weights = weights / weights.sum()
    return settings, (lower, upper), weights


def observations_asof(hourly: pd.DataFrame, issue: Any, weights=(0.5, 0.5)) -> pd.DataFrame:
    """Filter completed intervals first; do not let future values affect validation."""
    issue = _utc(issue, "issue")
    if not isinstance(hourly.index, pd.DatetimeIndex) or hourly.index.tz is None:
        raise ForecastModelError("hourly observations must have a timezone-aware DatetimeIndex")
    if any(column not in hourly for column in TURBINES):
        raise ForecastModelError("hourly observations require turbine_1 and turbine_2")
    columns = list(TURBINES) + [f"{column}_available_at" for column in TURBINES
                              if f"{column}_available_at" in hourly]
    observed = hourly.loc[hourly.index + pd.Timedelta(hours=1) <= issue, columns].copy()
    # Input QC may attach DataFrames in attrs; pandas compares attrs while
    # concatenating training frames, where DataFrame equality is not scalar.
    # Provenance belongs in the manifest, never in numeric feature metadata.
    observed.attrs = {}
    observed.index = observed.index.tz_convert("UTC")
    if observed.index.has_duplicates or not observed.index.equals(observed.index.floor("h")):
        raise ForecastModelError("completed observations must have unique, hour-aligned timestamps")
    observed = observed.sort_index()
    for column in TURBINES:
        availability = f"{column}_available_at"
        if availability in observed:
            times = [pd.NaT if pd.isna(value) else _utc(value, availability)
                     for value in observed[availability]]
            observed[availability] = pd.to_datetime(times, utc=True)
            if (observed[column].notna() & observed[availability].isna()).any():
                raise ForecastModelError(f"missing availability for a nonmissing {column} target")
            observed.loc[observed[availability] > issue, column] = np.nan
        else:
            observed[availability] = observed.index + pd.Timedelta(hours=1)
        observed[column] = pd.to_numeric(observed[column], errors="raise").astype(float)
        if np.isinf(observed[column]).any():
            raise ForecastModelError(f"infinite observed power in {column}")
    # A farm target exists only when both turbine targets exist.
    observed["farm_proxy"] = observed["turbine_1"] * weights[0] + observed["turbine_2"] * weights[1]
    return observed


def _information_asof(observed: pd.DataFrame, issue: pd.Timestamp) -> pd.DataFrame:
    """Censor late backfill at each synthetic issue, not just the live issue."""
    historical = observed.loc[observed.index + pd.Timedelta(hours=1) <= issue].copy()
    for column in TURBINES:
        historical.loc[historical[f"{column}_available_at"] > issue, column] = np.nan
    historical.loc[historical[list(TURBINES)].isna().any(axis=1), "farm_proxy"] = np.nan
    return historical


def _power_features(observed: pd.DataFrame, issue: pd.Timestamp, targets: pd.DatetimeIndex,
                    timezone: str) -> pd.DataFrame:
    """Features are calculated from an already-cutoff observation table."""
    historical = _information_asof(observed, issue)
    base: dict[str, float] = {}
    for column in SERIES:
        series = historical[column]
        valid = series.dropna()
        base[f"{column}_last"] = float(valid.iloc[-1]) if len(valid) else np.nan
        base[f"{column}_age_hours"] = ((issue - valid.index[-1]).total_seconds() / 3600) if len(valid) else np.nan
        for lag in (1, 2, 3, 6, 12, 24, 48, 72, 168):
            base[f"{column}_lag_{lag}"] = series.get(issue - pd.Timedelta(hours=lag), np.nan)
        for hours in (6, 24, 72, 168):
            window = series.loc[series.index >= issue - pd.Timedelta(hours=hours)].dropna()
            base[f"{column}_mean_{hours}"] = float(window.mean()) if len(window) else np.nan
            base[f"{column}_std_{hours}"] = float(window.std(ddof=0)) if len(window) else np.nan
            base[f"{column}_coverage_{hours}"] = len(window) / hours
    rows = []
    for target in targets:
        local = target.tz_convert(timezone)
        row = dict(base)
        row.update({
            "horizon": (target - issue).total_seconds() / 3600,
            "hour_sin": np.sin(2 * np.pi * local.hour / 24),
            "hour_cos": np.cos(2 * np.pi * local.hour / 24),
            "year_sin": np.sin(2 * np.pi * (local.dayofyear - 1) / 365.25),
            "year_cos": np.cos(2 * np.pi * (local.dayofyear - 1) / 365.25),
            "weekday": local.dayofweek,
        })
        for column in SERIES:
            for lag in (24, 48, 168):
                # Lookup only in historical, so e.g. target-24h cannot leak
                # a first-day measurement into the second forecast day.
                row[f"{column}_target_lag_{lag}"] = historical[column].get(target - pd.Timedelta(hours=lag), np.nan)
        rows.append(row)
    return pd.DataFrame(rows, index=targets, dtype=float)


def validate_weather(weather: Mapping | None, issue: Any, targets: pd.DatetimeIndex) -> tuple[dict, dict]:
    """Verify exact valid-time coverage and both vintage/availability cutoffs.

    Availability is supplied by the source adapter; this function cannot prove
    that provider metadata itself is authentic. Retrieval time is not treated
    as historical availability evidence.
    """
    issue = _utc(issue, "weather issue")
    if not isinstance(weather, Mapping):
        raise ForecastModelError("weather model requires archived forecasts for both turbines")
    checked, provenance = {}, {}
    for turbine in TURBINES:
        frame = weather.get(turbine)
        if not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex):
            raise ForecastModelError(f"missing timestamp-indexed weather for {turbine}")
        if frame.index.tz is None or frame.index.has_duplicates:
            raise ForecastModelError(f"weather index for {turbine} must be timezone-aware and unique")
        missing_columns = set(WEATHER_COLUMNS) - set(frame.columns)
        if missing_columns:
            raise ForecastModelError(f"weather for {turbine} lacks {sorted(missing_columns)}")
        frame = frame.copy()
        frame.index = frame.index.tz_convert("UTC")
        if not targets.isin(frame.index).all():
            raise ForecastModelError(f"weather for {turbine} does not cover every forecast target")
        frame = frame.loc[targets, list(WEATHER_COLUMNS)].copy()
        for column in ("weather_run_time", "weather_available_at"):
            # Timestamp parsing with utc=True alone would silently accept naive input.
            values = [_utc(value, column) for value in frame[column]]
            frame[column] = pd.DatetimeIndex(values)
            if (frame[column] > issue).any():
                raise ForecastModelError(f"{turbine}: {column} later than forecast issue")
        if (frame["weather_available_at"] < frame["weather_run_time"]).any():
            raise ForecastModelError(f"{turbine}: weather availability precedes model run")
        for column in ("wind_speed", "temperature"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
            if not np.isfinite(frame[column].to_numpy(dtype=float)).all():
                raise ForecastModelError(f"{turbine}: nonfinite weather {column}")
        if (frame["wind_speed"] < 0).any():
            raise ForecastModelError(f"{turbine}: negative forecast wind speed")
        if frame["weather_source"].isna().any() or frame["weather_source"].astype(str).str.strip().eq("").any():
            raise ForecastModelError(f"{turbine}: missing weather source")
        checked[turbine] = frame
        provenance[turbine] = {
            "run_times": sorted({value.isoformat() for value in frame["weather_run_time"]}),
            "availability_times": sorted({value.isoformat() for value in frame["weather_available_at"]}),
            "sources": sorted(set(frame["weather_source"].astype(str))),
        }
    return checked, provenance


def _weather_features(features: pd.DataFrame, weather: Mapping, issue: pd.Timestamp,
                      targets: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    checked, provenance = validate_weather(weather, issue, targets)
    result = features.copy()
    for turbine, frame in checked.items():
        result[f"{turbine}_forecast_wind_speed"] = frame["wind_speed"]
        result[f"{turbine}_forecast_wind_cubed"] = frame["wind_speed"] ** 3
        result[f"{turbine}_forecast_temperature"] = frame["temperature"]
        result[f"{turbine}_weather_run_age_hours"] = (issue - frame["weather_run_time"]).dt.total_seconds() / 3600
    return result, provenance


def forecast(hourly: pd.DataFrame, issue: Any, horizon: int, config: Mapping,
             model_name: str = "extra_trees", weather: Mapping | None = None,
             weather_history: Mapping | None = None) -> tuple[pd.DataFrame, dict, dict]:
    """Fit as of ``issue`` and predict; return predictions, JSON info, artifact.

    Supported names: ``persistence``, ``extra_trees`` (observations only), and
    ``extra_trees_weather``. Weather training history maps exact, timezone-aware
    historical issues to the same two-turbine weather mapping as ``weather``.
    Missing historical issues are omitted and reported; invalid supplied weather
    is an error. No model automatically substitutes observations for forecasts.

    ``config.model`` supports train_days=180, min_train_days=30, seed=42,
    n_estimators=128, min_samples_leaf=5, max_features=0.85, n_jobs=1.
    ``config.data`` supports timezone, power_min/power_max, and turbine_weights.
    Hourly ``turbine_N_available_at`` metadata censors late-arriving backfill at
    each training issue. Without it, delivery at interval completion is assumed.
    """
    if model_name not in {"persistence", "extra_trees", "extra_trees_weather"}:
        raise ForecastModelError(f"unknown model {model_name!r}")
    issue = _utc(issue, "issue")
    targets = forecast_targets(issue, horizon)
    settings, bounds, weights = _settings(config)
    observed = observations_asof(hourly, issue, weights)
    lower, upper = bounds
    for column in TURBINES:
        values = observed[column].dropna()
        if values.empty:
            raise ForecastModelError(f"no completed observations available for {column} at {issue.isoformat()}")
        if ((values < lower) | (values > upper)).any():
            raise ForecastModelError(f"{column}: observed target outside configured bounds {bounds}")
    latest = {column: observed[column].dropna().index[-1] for column in TURBINES}
    info = {
        "model_name": model_name,
        "model_version": MODEL_VERSION if model_name != "persistence" else "last-completed-hour-v1",
        "issue_time": issue.isoformat(),
        "feature_mode": "archive_weather" if model_name == "extra_trees_weather" else "observations_only",
        "target_bounds": list(bounds), "turbine_weights": weights.tolist(),
        "observation_cutoff": (issue - pd.Timedelta(hours=1)).isoformat(),
        "latest_observation_hour": {key: value.isoformat() for key, value in latest.items()},
        "observation_age_hours": {key: (issue - value).total_seconds() / 3600 for key, value in latest.items()},
        "training": {}, "weather": None, "sklearn_version": sklearn.__version__,
    }
    predictions = pd.DataFrame(index=targets)
    artifact: dict[str, Any] = {"model_info": info, "models": {}, "feature_columns": []}
    if model_name == "persistence":
        for column in TURBINES:
            predictions[column] = float(observed[column].dropna().iloc[-1])
    else:
        train_days = int(settings.get("train_days", 180))
        minimum_days = int(settings.get("min_train_days", 30))
        if train_days < minimum_days or minimum_days < 1:
            raise ForecastModelError("model.train_days must be >= model.min_train_days >= 1")
        timezone = config.get("data", {}).get("timezone", "+05:00")
        future_features = _power_features(observed, issue, targets, timezone)
        weather_mode = model_name == "extra_trees_weather"
        archive = {}
        if weather_mode:
            future_features, info["weather"] = _weather_features(future_features, weather, issue, targets)
            for key, value in (weather_history or {}).items():
                timestamp = _utc(key, "historical weather issue")
                if timestamp in archive:
                    raise ForecastModelError("duplicate historical weather issue after UTC conversion")
                archive[timestamp] = value
        feature_parts, label_parts, issue_parts = [], [], []
        weather_provenance, missing_weather_issues = [], []
        start = issue - pd.Timedelta(days=train_days)
        for historical_issue in pd.date_range(start, issue - pd.Timedelta(days=1), freq="24h"):
            training_targets = forecast_targets(historical_issue, horizon)
            training_targets = training_targets[training_targets + pd.Timedelta(hours=1) <= issue]
            if not len(training_targets):
                continue
            labels = observed.reindex(training_targets)[list(TURBINES)]
            if not labels.notna().any().any():
                continue
            # Do not train on synthetic issues before both turbines are observed.
            past = _information_asof(observed, historical_issue)
            if not past[list(TURBINES)].notna().any().all():
                continue
            if weather_mode and historical_issue not in archive:
                missing_weather_issues.append(historical_issue.isoformat())
                continue
            features = _power_features(past, historical_issue, training_targets, timezone)
            if weather_mode:
                features, provenance = _weather_features(features, archive[historical_issue], historical_issue, training_targets)
                weather_provenance.append({"issue_time": historical_issue.isoformat(), "weather": provenance})
            feature_parts.append(features)
            label_parts.append(labels)
            issue_parts.extend([historical_issue] * len(features))
        if not feature_parts:
            raise ForecastModelError("no leakage-free daily training examples available")
        X = pd.concat(feature_parts, ignore_index=True)
        y = pd.concat(label_parts, ignore_index=True)
        row_issues = pd.Series(issue_parts)
        fitted_models, counts = {}, {}
        for column in TURBINES:
            mask = y[column].notna()
            issue_count = int(row_issues.loc[mask].nunique())
            if issue_count < minimum_days:
                raise ForecastModelError(
                    f"{column}: only {issue_count} training issue days available; need {minimum_days} "
                    f"for {model_name}. Fetch more exact-issued archives or explicitly select observations-only mode."
                )
            model = Pipeline([
                ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                ("regressor", ExtraTreesRegressor(
                    n_estimators=int(settings.get("n_estimators", 128)),
                    min_samples_leaf=int(settings.get("min_samples_leaf", 5)),
                    max_features=float(settings.get("max_features", 0.85)),
                    random_state=int(settings.get("seed", 42)),
                    n_jobs=int(settings.get("n_jobs", 1)),
                )),
            ])
            model.fit(X.loc[mask], y.loc[mask, column])
            predictions[column] = model.predict(future_features)
            fitted_models[column] = model
            counts[column] = {"rows": int(mask.sum()), "issue_days": issue_count}
        info["training"] = {
            "issue_start": row_issues.min().isoformat(), "issue_end": row_issues.max().isoformat(),
            "label_start": min(part.index.min() for part in label_parts).isoformat(),
            "label_end": max(part.index.max() for part in label_parts).isoformat(),
            "per_turbine": counts, "train_days": train_days, "min_train_days": minimum_days,
            "seed": int(settings.get("seed", 42)),
            "parameters": fitted_models[TURBINES[0]].named_steps["regressor"].get_params(),
            "weather_issues": weather_provenance,
            "missing_weather_issues": missing_weather_issues,
        }
        info["feature_columns"] = list(X.columns)
        artifact.update({"models": fitted_models, "feature_columns": list(X.columns)})
    predictions["farm_proxy"] = predictions[TURBINES[0]] * weights[0] + predictions[TURBINES[1]] * weights[1]
    if not np.isfinite(predictions.to_numpy()).all():
        raise ForecastModelError("model produced nonfinite predictions")
    if ((predictions < lower) | (predictions > upper)).any().any():
        raise ForecastModelError("model produced out-of-bounds predictions; no silent clipping was applied")
    info["prediction_checks"] = {"finite": True, "within_bounds": True, "complete_horizon": True, "clipped": False}
    return predictions, info, artifact
