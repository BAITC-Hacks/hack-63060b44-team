from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import pandas as pd

from windforecast.cli import execute, parser


class WatchTests(unittest.TestCase):
    def test_now_rechecks_utc_clock_each_poll_and_floors_to_hour(self):
        args = parser().parse_args(["watch", "--issue", "now", "--iterations", "3", "--interval-seconds", "2", "--weather", "off"])
        times = [datetime(2026, 1, 31, 18, 59, 59, tzinfo=timezone.utc),
                 datetime(2026, 1, 31, 19, 0, 1, tzinfo=timezone.utc),
                 datetime(2026, 1, 31, 19, 10, 0, tzinfo=timezone.utc)]
        with patch("windforecast.cli.read_config", return_value={}), patch("windforecast.cli.datetime") as clock, \
             patch("windforecast.pipeline.run_forecast", return_value="saved-run") as run, \
             patch("windforecast.cli.time.sleep") as sleep, patch("builtins.print"):
            clock.now.side_effect = times
            self.assertEqual(execute(args), 0)
        expected = [pd.Timestamp("2026-01-31T18:00Z"), pd.Timestamp("2026-01-31T19:00Z"), pd.Timestamp("2026-01-31T19:00Z")]
        self.assertEqual([call.args[1] for call in run.call_args_list], expected)
        self.assertEqual(clock.now.call_count, 3)
        self.assertTrue(all(call.args == (timezone.utc,) for call in clock.now.call_args_list))
        self.assertEqual([call.args for call in sleep.call_args_list], [(2.,), (2.,)])

    def test_fixed_issue_stays_fixed_across_polls(self):
        issue = "2026-01-31T23:00:00+05:00"
        args = parser().parse_args(["watch", "--issue", issue, "--iterations", "2", "--weather", "off"])
        with patch("windforecast.cli.read_config", return_value={}), patch("windforecast.cli.datetime") as clock, \
             patch("windforecast.pipeline.run_forecast", return_value="saved-run") as run, \
             patch("windforecast.cli.time.sleep"), patch("builtins.print"):
            self.assertEqual(execute(args), 0)
        self.assertEqual([call.args[1] for call in run.call_args_list], [issue, issue])
        clock.now.assert_not_called()


if __name__ == "__main__":
    unittest.main()
