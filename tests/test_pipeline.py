from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from windforecast.common import targets_for
from windforecast.data import ingest_observations
from windforecast.evaluation import evaluate
from windforecast.pipeline import run_forecast, select_model, validate_forecast
from windforecast.verification import read_snapshot, verify_run


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.issue = pd.Timestamp("2026-01-03T00:00:00Z")
        self.targets = targets_for(self.issue, 3)
        self.config = {
            "_root": str(self.folder),
            "inputs": {},
            "data": {"timezone": "+05:00", "min_hourly_samples": 5,
                     "target_min": 0., "target_max": 1., "supplemental_dir": str(self.folder / "incoming")},
            "run": {"output_dir": "artifacts", "selection_file": "selection.json", "issue_hour_local": 23},
            "model": {}, "weather": {},
        }
        for turbine, value in (("turbine_1", .4), ("turbine_2", .6)):
            path = self.folder / f"{turbine}.csv"
            frame = pd.DataFrame({"power": value, "wind_speed": 7., "temperature": 20.},
                                 index=pd.date_range("2026-01-01T00:00Z", periods=288, freq="10min", name="timestamp"))
            frame.to_csv(path)
            self.config["inputs"][turbine] = str(path)

    def tearDown(self):
        self.temporary.cleanup()

    def prediction(self):
        return pd.DataFrame({"turbine_1": .4, "turbine_2": .6, "farm_proxy": .5}, index=self.targets)

    def test_changed_input_recomputes_and_unchanged_input_is_idempotent(self):
        # Freeze code identity while other development can run in the shared repo.
        with patch("windforecast.pipeline.source_version", return_value="test-version"):
            first = run_forecast(self.config, self.issue, 3, "persistence", "off")
            artifact = (first / "forecast.csv").read_bytes()
            again = run_forecast(self.config, self.issue, 3, "persistence", "off")
            self.assertEqual(first, again)
            self.assertEqual((again / "forecast.csv").read_bytes(), artifact)
            source = Path(self.config["inputs"]["turbine_1"])
            rows = pd.read_csv(source)
            rows.loc[len(rows) - 1, "power"] = .8
            rows.to_csv(source, index=False)
            changed = run_forecast(self.config, self.issue, 3, "persistence", "off")
            self.assertNotEqual(first, changed)
            self.assertNotEqual((changed / "forecast.csv").read_bytes(), artifact)
            self.assertEqual((first / "forecast.csv").read_bytes(), artifact)
        events = [json.loads(line)["event"] for line in (self.folder / "artifacts" / "events.jsonl").read_text().splitlines()]
        self.assertIn("unchanged_inputs", events)

    def test_invalid_forecasts_raise_without_clipping(self):
        cases = []
        outside = self.prediction()
        outside.loc[self.targets[0], "turbine_1"] = 1.01
        cases.append((outside, "outside"))
        missing_value = self.prediction()
        missing_value.loc[self.targets[0], "turbine_1"] = np.nan
        cases.append((missing_value, "non-finite"))
        cases.append((self.prediction().iloc[1:], "hours"))
        wrong_farm = self.prediction()
        wrong_farm.loc[self.targets[0], "farm_proxy"] = .2
        cases.append((wrong_farm, "Farm proxy"))
        for frame, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_forecast(frame, self.issue, self.targets, self.config)

    def test_weather_vintage_must_be_available_by_issue(self):
        weather = pd.DataFrame({"wind_speed": 7., "temperature": 20.,
                                "weather_run_time": self.issue - pd.Timedelta(hours=6),
                                "weather_available_at": self.issue}, index=self.targets)
        for column in ("weather_run_time", "weather_available_at"):
            future = weather.copy()
            future.loc[self.targets[0], column] = self.issue + pd.Timedelta(seconds=1)
            with self.subTest(column=column), self.assertRaisesRegex(ValueError, "later"):
                validate_forecast(self.prediction(), self.issue, self.targets, self.config,
                                  {"turbine_1": future, "turbine_2": weather})

    def test_snapshot_preserves_availability_timestamps(self):
        path = self.folder / "hourly.csv"
        frame = pd.DataFrame({"turbine_1": [.5, np.nan],
                              "turbine_1_available_at": [self.issue, pd.NaT]}, index=self.targets[:2])
        frame.to_csv(path)
        parsed = read_snapshot(path)
        self.assertEqual(parsed.iloc[0]["turbine_1_available_at"], self.issue)
        self.assertTrue(pd.isna(parsed.iloc[1]["turbine_1_available_at"]))
        self.assertEqual(str(parsed["turbine_1_available_at"].dt.tz), "UTC")

    def test_february_without_actuals_cannot_be_evaluated(self):
        with self.assertRaisesRegex(ValueError, "February accuracy"):
            evaluate(self.config, "2026-02-01", "2026-02-02", ["persistence"], horizon=48)

    def test_model_selection_waits_for_validation_labels(self):
        selection_path = self.folder / "selection.json"
        selection_path.write_text(json.dumps({"known_at": (self.issue + pd.Timedelta(hours=1)).isoformat(),
                                              "selected_model": "extra_trees"}), encoding="utf-8")
        selected, details = select_model(self.config, self.issue, "auto", "off")
        self.assertEqual(selected, "persistence")
        self.assertIn("baseline", details["method"])

    def test_model_selection_requires_compatible_settings(self):
        selection_path = self.folder / "selection.json"
        for section, key, changed_value in (("data", "min_hourly_samples", 6), ("model", "seed", 7),
                                            ("weather", "publication_delay_hours", 12)):
            past_config = deepcopy(self.config)
            past_config[section][key] = changed_value
            selection_path.write_text(json.dumps({"known_at": self.issue.isoformat(), "selected_model": "extra_trees",
                                                  "config": past_config}), encoding="utf-8")
            with self.subTest(section=section):
                name, details = select_model(self.config, self.issue, "auto", "off")
                self.assertEqual(name, "persistence")
                self.assertEqual(details["method"], "incompatible_validation")
                self.assertIn(section, details["changed_settings"])

    def test_model_selection_ignores_storage_paths(self):
        past_config = deepcopy(self.config)
        past_config["_root"] = "/different/root"
        past_config["inputs"] = {"turbine_1": "/moved/one.csv", "turbine_2": "/moved/two.csv"}
        past_config["run"]["output_dir"] = "/moved/output"
        past_config["data"]["supplemental_dir"] = "/moved/incoming"
        past_config["weather"]["cache_dir"] = "/moved/weather"
        (self.folder / "selection.json").write_text(json.dumps({"known_at": self.issue.isoformat(),
            "selected_model": "extra_trees", "config": past_config}), encoding="utf-8")
        name, details = select_model(self.config, self.issue, "auto", "off")
        self.assertEqual(name, "extra_trees")
        self.assertEqual(details["method"], "past_rolling_validation")

    def test_model_selection_rejects_missing_saved_configuration(self):
        (self.folder / "selection.json").write_text(json.dumps({"known_at": self.issue.isoformat(),
            "selected_model": "extra_trees"}), encoding="utf-8")
        name, details = select_model(self.config, self.issue, "auto", "off")
        self.assertEqual(name, "persistence")
        self.assertEqual(details["method"], "incompatible_validation")

    def test_model_selection_rejects_different_weather_coordinates(self):
        past_config = deepcopy(self.config)
        past_config["turbines"] = {"turbine_1": {"latitude": 43., "longitude": 78.}}
        (self.folder / "selection.json").write_text(json.dumps({"known_at": self.issue.isoformat(),
            "selected_model": "extra_trees", "config": past_config}), encoding="utf-8")
        name, details = select_model(self.config, self.issue, "auto", "off")
        self.assertEqual(name, "persistence")
        self.assertIn("coordinates", details["changed_settings"])

    def test_validation_selection_known_at_includes_late_label_receipt(self):
        source = Path(self.config["inputs"]["turbine_1"])
        rows = pd.read_csv(source)
        times = pd.to_datetime(rows.timestamp, utc=True)
        late = (times >= pd.Timestamp("2026-01-01T19:00Z")) & (times < pd.Timestamp("2026-01-01T22:00Z"))
        update = self.folder / "late-labels.csv"
        rows.loc[late].to_csv(update, index=False)
        rows.loc[~late].to_csv(source, index=False)
        received = pd.Timestamp("2026-01-05T00:00Z")
        ingest_observations(self.config, "turbine_1", update, received)
        evaluate(self.config, "2026-01-01", "2026-01-01", ["persistence"], horizon=3)
        selection = json.loads((self.folder / "selection.json").read_text())
        self.assertEqual(pd.Timestamp(selection["known_at"]), received)

    def test_failed_check_releases_lock_and_does_not_save_success(self):
        invalid = self.prediction()
        invalid.loc[self.targets[0], "farm_proxy"] = 2.
        with patch("windforecast.pipeline.forecast", return_value=(invalid, {}, {})):
            with self.assertRaisesRegex(ValueError, "outside"):
                run_forecast(self.config, self.issue, 3, "persistence", "off")
        root = self.folder / "artifacts"
        self.assertFalse((root / ".run.lock").exists())
        self.assertEqual(list(root.glob("runs/*/manifest.json")), [])
        self.assertEqual(json.loads((root / "events.jsonl").read_text().splitlines()[-1])["event"], "failed")

    def test_artifact_tampering_fails_verification(self):
        directory = run_forecast(self.config, self.issue, 3, "persistence", "off")
        with (directory / "forecast.csv").open("a") as out:
            out.write("changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify_run(directory)


if __name__ == "__main__":
    unittest.main()
