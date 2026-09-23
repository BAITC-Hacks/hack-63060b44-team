import copy
import json

import numpy as np
import pandas as pd
import pytest

from windforecast.models import (ForecastModelError, _power_features, forecast,
                                 forecast_targets, observations_asof, validate_weather)


@pytest.fixture
def hourly():
    index = pd.date_range("2025-01-01", periods=55 * 24, freq="h", tz="UTC")
    signal = 0.45 + 0.2 * np.sin(np.arange(len(index)) / 17) + 0.12 * np.cos(np.arange(len(index)) / 6)
    return pd.DataFrame({"turbine_1": signal, "turbine_2": signal * 0.8 + 0.05}, index=index)


@pytest.fixture
def config():
    return {"data": {"timezone": "+05:00", "power_bounds": [0, 1], "turbine_weights": [1, 3]},
            "model": {"train_days": 14, "min_train_days": 5, "n_estimators": 8,
                      "min_samples_leaf": 2, "seed": 42, "n_jobs": 1}}


def make_weather(issue, horizon=48):
    targets = forecast_targets(issue, horizon)
    return {turbine: pd.DataFrame({
        "wind_speed": np.arange(horizon) / 10 + 4,
        "temperature": 10,
        "weather_run_time": issue - pd.Timedelta(hours=6),
        "weather_available_at": issue - pd.Timedelta(hours=2),
        "weather_source": "test-explicit-vintage",
    }, index=targets) for turbine in ("turbine_1", "turbine_2")}


def test_persistence_only_uses_completed_hours_and_weighted_farm(hourly, config):
    issue = hourly.index[100]
    predictions, info, _ = forecast(hourly, issue, 48, config, "persistence")
    assert len(predictions) == 48
    assert predictions.index[0] == issue + pd.Timedelta(hours=1)
    assert predictions.index[-1] == issue + pd.Timedelta(hours=48)
    assert (predictions.turbine_1 == hourly.turbine_1.iloc[99]).all()
    expected = 0.25 * predictions.turbine_1 + 0.75 * predictions.turbine_2
    np.testing.assert_allclose(predictions.farm_proxy, expected)
    assert info["observation_age_hours"]["turbine_1"] == 1
    assert info["prediction_checks"]["clipped"] is False


@pytest.mark.parametrize("model_name", ["persistence", "extra_trees"])
def test_current_future_values_cannot_change_prediction_or_training(hourly, config, model_name):
    issue = hourly.index[40 * 24]
    first, original_info, _ = forecast(hourly, issue, 48, config, model_name)
    changed = hourly.copy()
    # Includes the not-yet-completed issue hour, and deliberately impossible
    # later values: neither validation nor imputation may inspect them.
    changed.loc[changed.index >= issue, "turbine_1"] = 99999
    changed.loc[changed.index >= issue, "turbine_2"] = -99999
    second, changed_info, _ = forecast(changed, issue, 48, config, model_name)
    pd.testing.assert_frame_equal(first, second)
    assert original_info == changed_info


def test_direct_training_respects_issue_and_label_cutoffs(hourly, config):
    issue = hourly.index[40 * 24]
    hourly.attrs["coverage"] = pd.DataFrame({"counts": [5, 6]})
    predictions, info, artifact = forecast(hourly, issue, 48, config)
    assert pd.Timestamp(info["training"]["issue_end"]) == issue - pd.Timedelta(days=1)
    assert pd.Timestamp(info["training"]["label_end"]) + pd.Timedelta(hours=1) <= issue
    assert info["training"]["per_turbine"]["turbine_1"]["issue_days"] == 14
    assert len(predictions) == 48
    assert set(artifact["models"]) == {"turbine_1", "turbine_2"}
    json.dumps(info)  # persisted manifest must be JSON-serializable
    assert "coverage" in hourly.attrs  # caller metadata remains intact


def test_each_synthetic_issue_excludes_later_measurements(hourly):
    actual_issue = hourly.index[40 * 24]
    synthetic_issue = actual_issue - pd.Timedelta(days=7)
    known_at_training = observations_asof(hourly, actual_issue)
    targets = forecast_targets(synthetic_issue, 48)
    expected = _power_features(known_at_training, synthetic_issue, targets, "+05:00")
    mutated = known_at_training.copy()
    mutated.loc[mutated.index >= synthetic_issue, ["turbine_1", "turbine_2", "farm_proxy"]] = 0.99
    actual = _power_features(mutated, synthetic_issue, targets, "+05:00")
    pd.testing.assert_frame_equal(expected, actual)
    # A target-relative 24-hour lag is unavailable on the second day.
    assert actual.loc[targets[24], "turbine_1_target_lag_24"] != actual.loc[targets[24], "turbine_1_target_lag_24"]


def test_late_backfill_is_hidden_at_synthetic_training_issue(hourly):
    actual_issue = hourly.index[40 * 24]
    synthetic_issue = actual_issue - pd.Timedelta(days=1)
    delivered = hourly.copy()
    for turbine in ("turbine_1", "turbine_2"):
        delivered[f"{turbine}_available_at"] = delivered.index + pd.Timedelta(hours=1)
    delayed_hours = (delivered.index >= synthetic_issue - pd.Timedelta(days=3)) & (delivered.index < synthetic_issue)
    delivered.loc[delayed_hours, "turbine_1_available_at"] = actual_issue
    known = observations_asof(delivered, actual_issue)
    targets = forecast_targets(synthetic_issue, 48)
    expected = _power_features(known, synthetic_issue, targets, "+05:00")
    delivered.loc[delayed_hours, "turbine_1"] = 0.99
    changed = observations_asof(delivered, actual_issue)
    actual = _power_features(changed, synthetic_issue, targets, "+05:00")
    pd.testing.assert_frame_equal(expected, actual)
    assert actual.iloc[0]["turbine_1_age_hours"] == 73
    assert actual.iloc[0]["turbine_1_coverage_24"] == 0
    # At the actual issue the late measurements are available again.
    assert observations_asof(delivered, actual_issue).turbine_1.loc[delayed_hours[:len(known)]].notna().all()


def test_missing_targets_are_excluded_instead_of_imputed(hourly, config):
    issue = hourly.index[40 * 24]
    _, full_info, _ = forecast(hourly, issue, 12, config)
    missing = hourly.copy()
    missing.loc[missing.index[30 * 24:32 * 24], "turbine_1"] = np.nan
    _, missing_info, _ = forecast(missing, issue, 12, config)
    assert missing_info["training"]["per_turbine"]["turbine_1"]["rows"] < full_info["training"]["per_turbine"]["turbine_1"]["rows"]
    assert missing_info["training"]["per_turbine"]["turbine_2"]["rows"] == full_info["training"]["per_turbine"]["turbine_2"]["rows"]


@pytest.mark.parametrize("column", ["weather_run_time", "weather_available_at"])
def test_rejects_weather_released_after_issue(column):
    issue = pd.Timestamp("2025-02-01 18:00:00Z")
    weather = make_weather(issue)
    weather["turbine_1"][column] = issue + pd.Timedelta(seconds=1)
    with pytest.raises(ForecastModelError, match="later than forecast issue"):
        validate_weather(weather, issue, forecast_targets(issue, 48))


def test_weather_requires_explicit_timezones_and_complete_coverage():
    issue = pd.Timestamp("2025-02-01 18:00:00Z")
    weather = make_weather(issue)
    weather["turbine_1"]["weather_run_time"] = "2025-02-01 12:00:00"
    with pytest.raises(ForecastModelError, match="timezone-aware"):
        validate_weather(weather, issue, forecast_targets(issue, 48))
    weather = make_weather(issue)
    weather["turbine_2"] = weather["turbine_2"].iloc[:-1]
    with pytest.raises(ForecastModelError, match="every forecast target"):
        validate_weather(weather, issue, forecast_targets(issue, 48))


def test_weather_training_requires_exact_daily_vintages(hourly, config):
    issue = hourly.index[40 * 24]
    current = make_weather(issue, 12)
    with pytest.raises(ForecastModelError, match="no leakage-free"):
        forecast(hourly, issue, 12, config, "extra_trees_weather", current)
    history = {issue - pd.Timedelta(days=day): make_weather(issue - pd.Timedelta(days=day), 12)
               for day in range(1, 15)}
    predictions, info, _ = forecast(hourly, issue, 12, config, "extra_trees_weather", current, history)
    assert len(predictions) == 12
    assert info["feature_mode"] == "archive_weather"
    assert len(info["training"]["weather_issues"]) == 14
    assert "turbine_1_forecast_wind_speed" in info["feature_columns"]
    invalid = copy.deepcopy(history)
    invalid[issue - pd.Timedelta(days=7)]["turbine_2"]["weather_available_at"] = issue
    with pytest.raises(ForecastModelError, match="later than forecast issue"):
        forecast(hourly, issue, 12, config, "extra_trees_weather", current, invalid)


def test_insufficient_history_fails_without_silent_fallback(hourly, config):
    with pytest.raises(ForecastModelError, match="training issue days"):
        forecast(hourly, hourly.index[3 * 24], 48, config)


def test_issue_and_horizon_validation():
    with pytest.raises(ForecastModelError, match="timezone-aware"):
        forecast_targets("2025-01-01", 48)
    with pytest.raises(ForecastModelError, match="full hour"):
        forecast_targets("2025-01-01 00:30:00Z", 48)
    with pytest.raises(ForecastModelError, match="between 1 and 48"):
        forecast_targets("2025-01-01 00:00:00Z", 49)
