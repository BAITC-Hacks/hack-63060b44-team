from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .common import daily_issues, file_hash, resolve, save_json, utc
from .data import hourly_targets, load_observations
from .pipeline import ENTITIES, run_forecast


def score_rows(rows, scale):
    records = []
    for (model, entity), group in rows.groupby(["model", "entity"]):
        scopes = [("all", group), ("1-24", group[group.horizon <= 24]), ("25-48", group[group.horizon > 24])]
        scopes += [(str(h), group[group.horizon == h]) for h in sorted(group.horizon.unique())]
        for label, chunk in scopes:
            valid = chunk.dropna(subset=["actual", "prediction"])
            if valid.empty:
                continue
            error = valid.prediction - valid.actual
            mae, rmse = float(error.abs().mean()), float(np.sqrt(np.mean(error**2)))
            records.append({"model": model, "entity": entity, "horizon": label, "n": len(valid),
                            "expected": len(chunk), "coverage": len(valid) / len(chunk), "mae": mae,
                            "rmse": rmse, "bias": float(error.mean()), "nmae": mae / scale, "nrmse": rmse / scale})
    return pd.DataFrame(records)


def evaluate(config, start, end, models, weather_mode="off", horizon=48, every_days=1, output="reports/validation", update_selection=True):
    if every_days < 1:
        raise ValueError("every_days must be positive")
    observations = load_observations(config)
    truth = hourly_targets(observations, config)
    issues = daily_issues(start, end, config)[::every_days]
    if not issues:
        raise ValueError("No validation issues requested")
    if issues[-1] + pd.Timedelta(hours=horizon + 1) > truth.index.max() + pd.Timedelta(hours=1):
        raise ValueError("Validation horizon extends beyond known measurements. February accuracy cannot be computed.")
    records, folds, availability_times = [], [], []
    for issue in issues:
        for model in models:
            directory = run_forecast(config, issue, horizon, model, weather_mode, observations)
            predicted = pd.read_csv(directory / "forecast.csv")
            predicted.index = pd.to_datetime(predicted.target_time, utc=True)
            fold = {"issue": issue.isoformat(), "model": model, "run": str(directory),
                    "manifest_sha256": file_hash(directory / "manifest.json")}
            folds.append(fold)
            for entity in ENTITIES:
                actual = truth[entity].reindex(predicted.index)
                turbines = [entity] if entity != "farm_proxy" else ["turbine_1", "turbine_2"]
                for turbine in turbines:
                    availability = truth[f"{turbine}_available_at"].reindex(predicted.index)
                    availability_times.extend(availability.loc[actual.notna()].dropna().tolist())
                for time, row in predicted.iterrows():
                    records.append({"issue_time": issue, "target_time": time, "model": model,
                                    "entity": entity, "horizon": int(row.horizon), "prediction": row[entity],
                                    "actual": actual.loc[time]})
    rows = pd.DataFrame(records)
    if rows.actual.notna().sum() == 0:
        raise ValueError("No valid hourly targets in validation period")
    metrics = score_rows(rows, config["data"]["target_max"] - config["data"]["target_min"])
    destination = resolve(config, output)
    destination.mkdir(parents=True, exist_ok=True)
    rows.to_csv(destination / "predictions.csv", index=False)
    metrics.to_csv(destination / "metrics.csv", index=False)
    # Complete candidate runs are mandatory, so every model is scored on the same target mask.
    ranking = metrics[(metrics.entity == "farm_proxy") & (metrics.horizon == "all")].sort_values(["mae", "model"])
    if ranking.empty:
        raise ValueError("No paired farm targets available for model selection; inspect turbine coverage")
    winner = ranking.iloc[0]
    selection = {"selected_model": winner.model, "metric": "farm_proxy_mae", "value": float(winner.mae),
                 "known_at": max([issues[-1] + pd.Timedelta(hours=horizon + 1), *availability_times]).isoformat(),
                 "first_issue": issues[0].isoformat(), "last_issue": issues[-1].isoformat(),
                 "n_folds": len(issues), "horizon": horizon, "candidate_models": list(models),
                 "weather_mode": weather_mode, "config": {k: v for k, v in config.items() if not k.startswith("_")},
                 "metrics_sha256": file_hash(destination / "metrics.csv"),
                 "note": "Rolling model selection score, not an independent final test or February contest score."}
    save_json(destination / "selection.json", selection)
    save_json(destination / "folds.json", folds)
    if update_selection:
        save_json(resolve(config, config["run"]["selection_file"]), selection)
    return destination, metrics
