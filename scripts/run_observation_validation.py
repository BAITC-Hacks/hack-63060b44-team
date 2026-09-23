"""Local validation runner; does not modify production model selection."""
import copy
import json
from pathlib import Path

from windforecast.common import read_config, save_json
from windforecast.evaluation import evaluate
from windforecast.verification import verify_run

config = read_config("config.example.json")
config["run"]["output_dir"] = "artifacts/observation_validation"
summaries = []
for minimum, output in [(5, "reports/validation_observations"), (6, "reports/validation_coverage6")]:
    current = copy.deepcopy(config)
    current["data"]["min_hourly_samples"] = minimum
    if minimum == 6:
        current["run"]["output_dir"] = "artifacts/coverage6_validation"
    print(f"Starting 13 weekly folds, {minimum}/6 coverage", flush=True)
    destination, metrics = evaluate(
        current, start="2025-11-01", end="2026-01-24",
        models=["persistence", "extra_trees"], weather_mode="off",
        every_days=7, output=output, update_selection=False,
    )
    overall = metrics.loc[metrics.horizon == "all"]
    print(overall.to_string(index=False), flush=True)
    folds = json.loads((destination / "folds.json").read_text(encoding="utf-8"))
    selected = next(row for row in folds if row["model"] == "extra_trees")
    verification = verify_run(selected["run"])
    print(verification, flush=True)
    summaries.append({"min_hourly_samples": minimum, "metrics": overall.to_dict("records"),
                      "verification": verification, "fold_count": len(folds) // 2})
    save_json("reports/observation_validation_summary.json", summaries)
