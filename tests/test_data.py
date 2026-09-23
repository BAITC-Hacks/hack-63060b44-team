from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from windforecast.data import (
    audit_data, hourly_targets, ingest_observations, load_observations,
    read_observations, utc_timestamp,
)


def measurements(index, values=None):
    frame = pd.DataFrame({"power": values if values is not None else 0.5,
                          "wind_speed": 7.0, "temperature": 20.0}, index=index)
    frame["available_at"] = frame.index + pd.Timedelta(minutes=10)
    frame.index.name = "timestamp"
    return frame


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.index = pd.date_range("2026-01-01T00:00:00Z", periods=12, freq="10min")
        self.config = {"data": {"timezone": "+05:00", "min_hourly_samples": 5}}

    def test_coverage_and_farm_require_both_turbines(self):
        one = measurements(self.index.delete([0, 6, 7]))
        two = measurements(self.index, np.zeros(len(self.index)))
        result = hourly_targets({"turbine_1": one, "turbine_2": two}, self.config)
        self.assertEqual(result.loc[self.index[0], "turbine_1"], 0.5)
        self.assertEqual(result.loc[self.index[0], "farm_proxy"], 0.25)
        self.assertTrue(np.isnan(result.loc[self.index[6], "turbine_1"]))
        self.assertTrue(np.isnan(result.loc[self.index[6], "farm_proxy"]))
        self.assertEqual(result.loc[self.index[6], "turbine_2"], 0.0)

    def test_as_of_excludes_unfinished_hour_and_late_observations(self):
        frame = measurements(self.index)
        observations = {"turbine_1": frame.copy(), "turbine_2": frame.copy()}
        result = hourly_targets(observations, self.config, "2026-01-01T00:50:00Z")
        self.assertEqual(len(result), 0)
        observations["turbine_1"].loc[self.index[:2], "available_at"] = pd.Timestamp("2026-01-02T00:00Z")
        result = hourly_targets(observations, self.config, "2026-01-01T01:00:00Z")
        self.assertEqual(len(result), 1)
        self.assertTrue(np.isnan(result.iloc[0]["turbine_1"]))
        self.assertEqual(result.iloc[0]["turbine_2"], 0.5)

    def test_invalid_power_does_not_become_zero_or_valid_coverage(self):
        frame = measurements(self.index, [0.5, 0.5, 0.5, 0.5, -0.1, 1.1] * 2)
        original = frame.copy(deep=True)
        result = hourly_targets({"turbine_1": frame, "turbine_2": frame}, self.config)
        self.assertTrue(result.isna().all().all())
        pd.testing.assert_frame_equal(frame, original)

    def test_csv_timezone_sorting_and_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.csv"
            raw = pd.DataFrame({"ID": [2, 1], "Статистическое время": ["2026-01-01 00:10:00", "2026-01-01 00:00:00"],
                                "Средняя скорость ветра(m/s)": [7, 8], "Нормализованная активная мощность": [.2, .4],
                                "Средняя температура окружающей среды(°C)": [15, 16]})
            raw.to_csv(source, index=False)
            parsed = read_observations(source, "+05:00")
            self.assertEqual(parsed.index[0], pd.Timestamp("2025-12-31T19:00:00Z"))
            self.assertEqual(parsed.iloc[0]["power"], .4)
            self.assertEqual(parsed.iloc[0]["available_at"], pd.Timestamp("2025-12-31T19:10:00Z"))
            raw.loc[1, "Статистическое время"] = raw.loc[0, "Статистическое время"]
            raw.to_csv(source, index=False)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                read_observations(source)

    def test_ingest_immutable_idempotent_and_causal(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            one, two, update = (folder / name for name in ("one.csv", "two.csv", "update.csv"))
            measurements(self.index[:6]).to_csv(one)
            measurements(self.index).to_csv(two)
            measurements(self.index[6:]).to_csv(update)
            config = {"inputs": {"turbine_1": str(one), "turbine_2": str(two)},
                      "data": {"supplemental_dir": str(folder / "incoming")}}
            original_bytes = one.read_bytes()
            received = "2026-01-02T00:00:00Z"
            result = ingest_observations(config, "turbine_1", update, received)
            self.assertEqual(result["new_timestamps"], 6)
            self.assertEqual(one.read_bytes(), original_bytes)
            self.assertEqual(ingest_observations(config, "turbine_1", update, received)["status"], "unchanged")
            observations = load_observations(config)
            earlier = hourly_targets(observations, config, "2026-01-01T02:00:00Z")
            self.assertTrue(pd.isna(earlier.loc[self.index[6], "turbine_1"]))
            later = hourly_targets(observations, config, received)
            self.assertEqual(later.loc[self.index[6], "turbine_1"], .5)
            self.assertEqual(later.loc[self.index[6], "turbine_1_available_at"], pd.Timestamp(received))
            self.assertEqual(later.loc[self.index[6], "turbine_2_available_at"], pd.Timestamp("2026-01-01T02:00:00Z"))

    def test_ingest_rejects_conflicting_values_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            original, update = folder / "source.csv", folder / "update.csv"
            measurements(self.index).to_csv(original)
            measurements(self.index, np.ones(len(self.index))).to_csv(update)
            config = {"inputs": {"turbine_1": str(original), "turbine_2": str(original)},
                      "data": {"supplemental_dir": str(folder / "incoming")}}
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                ingest_observations(config, "turbine_1", update)
            self.assertFalse((folder / "incoming").exists())

    def test_audit_aligns_by_time_not_id(self):
        one = measurements(self.index.delete([0, 1]))
        two = measurements(self.index.delete([10, 11]))
        one["id"] = np.arange(len(one))
        two["id"] = np.arange(len(two))
        report = audit_data({"turbine_1": one, "turbine_2": two}, self.config)
        self.assertEqual(report["alignment"]["shared_timestamps"], 8)
        self.assertEqual(report["alignment"]["only_turbine_1"], 2)
        self.assertEqual(report["alignment"]["only_turbine_2"], 2)

    def test_naive_issue_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "explicit timezone"):
            utc_timestamp("2026-01-01 01:00")


if __name__ == "__main__":
    unittest.main()
