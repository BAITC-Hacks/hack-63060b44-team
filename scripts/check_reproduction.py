"""Check saved first/last replay runs and a repeated unchanged invocation."""
import argparse
import json
from pathlib import Path

from windforecast.common import file_hash, read_config, save_json
from windforecast.pipeline import run_forecast
from windforecast.verification import verify_run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/reproducibility.json",
                        help="Write this check separately when preserving a historical report")
    args = parser.parse_args(argv)
    config = read_config("config.example.json")
    records = json.loads(Path("artifacts/replay.json").read_text(encoding="utf-8"))
    first, last = records[0], records[-1]
    results = [verify_run(first["directory"]), verify_run(last["directory"])]
    directory = Path(first["directory"])
    before = file_hash(directory / "manifest.json")
    repeated = run_forecast(config, first["issue"], 48, "auto", "cache")
    after = file_hash(repeated / "manifest.json")
    assert repeated == directory and before == after
    result = {"refits": results, "idempotence": {"same_directory": True, "manifest_unchanged": True,
              "run_id": directory.name}, "test_suite": {"status": "not_run_by_this_script",
              "command": "python -m pytest -q"}}
    save_json(args.output, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
