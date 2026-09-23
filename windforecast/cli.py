from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

from .common import daily_issues, read_config, resolve, save_json, targets_for, utc


def parser():
    p = argparse.ArgumentParser(description="Auditable hourly wind farm forecasts")
    p.add_argument("--config", default="config.example.json")
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-data", help="Copy supplied original CSVs without modifying them")
    init.add_argument("--turbine-1", required=True)
    init.add_argument("--turbine-2", required=True)
    commands.add_parser("audit", help="Audit raw measurements and hourly coverage")
    for name in ("forecast", "watch"):
        run = commands.add_parser(name)
        issue_help = "ISO time; naive times use configured timezone"
        if name == "watch":
            issue_help += "; 'now' advances to the current UTC hour on each poll"
        run.add_argument("--issue", required=True, help=issue_help)
        run.add_argument("--horizon", type=int, default=48)
        run.add_argument("--model", choices=["auto", "persistence", "extra_trees", "extra_trees_weather"], default="auto")
        run.add_argument("--weather", choices=["off", "cache", "network"], default="cache")
        if name == "watch":
            run.add_argument("--interval-seconds", type=float, default=60)
            run.add_argument("--iterations", type=int, default=0, help="0 runs until interrupted")
    replay = commands.add_parser("replay")
    replay.add_argument("--start", default="2026-01-31")
    replay.add_argument("--end", default="2026-02-28")
    replay.add_argument("--horizon", type=int, default=48)
    replay.add_argument("--model", choices=["auto", "persistence", "extra_trees", "extra_trees_weather"], default="auto")
    replay.add_argument("--weather", choices=["off", "cache", "network"], default="cache")
    replay.add_argument("--continue-on-error", action="store_true")
    ev = commands.add_parser("evaluate")
    ev.add_argument("--start", required=True)
    ev.add_argument("--end", required=True)
    ev.add_argument("--models", nargs="+", choices=["persistence", "extra_trees", "extra_trees_weather"], default=["persistence", "extra_trees"])
    ev.add_argument("--weather", choices=["off", "cache", "network"], default="off")
    ev.add_argument("--horizon", type=int, default=48)
    ev.add_argument("--every-days", type=int, default=1)
    ev.add_argument("--output", default="reports/validation")
    ev.add_argument("--no-select", action="store_true", help="Report a held-out period without changing selected model")
    incoming = commands.add_parser("ingest")
    incoming.add_argument("--turbine", choices=["turbine_1", "turbine_2"], required=True)
    incoming.add_argument("--file", required=True)
    incoming.add_argument("--received-at", help="Optional documented arrival time; default is now")
    incoming.add_argument("--recalculate-issue")
    incoming.add_argument("--weather", choices=["off", "cache", "network"], default="cache")
    weather = commands.add_parser("cache-weather")
    weather.add_argument("--start", required=True)
    weather.add_argument("--end", required=True)
    weather.add_argument("--horizon", type=int, default=48)
    verify = commands.add_parser("verify-run", help="Refit from a saved input snapshot without network")
    verify.add_argument("directory")
    return p


def watch_issue(value):
    """Resolve a moving clock only at a poll, preserving explicit replay issues."""
    if value.strip().lower() == "now":
        return pd.Timestamp(datetime.now(timezone.utc)).floor("h")
    return value


def execute(args):
    config = read_config(args.config)
    from .data import audit_data, ingest_observations, load_observations
    from .pipeline import log_event, run_forecast
    if args.command == "init-data":
        for turbine, source in (("turbine_1", args.turbine_1), ("turbine_2", args.turbine_2)):
            target = Path(config["inputs"][turbine])
            if target.exists():
                if target.read_bytes() != Path(source).read_bytes():
                    raise ValueError(f"Refusing to overwrite different input: {target}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        print("Original CSVs copied; run audit next.")
    elif args.command == "audit":
        report = audit_data(load_observations(config), config)
        destination = resolve(config, "reports/data_audit.json")
        save_json(destination, report)
        print(f"Data audit: {destination}")
    elif args.command == "forecast":
        print(run_forecast(config, args.issue, args.horizon, args.model, args.weather))
    elif args.command == "watch":
        if args.interval_seconds < 1 or args.iterations < 0:
            raise ValueError("Watch interval must be >=1 second and iterations >=0")
        n = 0
        while args.iterations == 0 or n < args.iterations:
            try:
                issue = watch_issue(args.issue)
                print(run_forecast(config, issue, args.horizon, args.model, args.weather), flush=True)
            except Exception as exc:
                print(f"Watch run failed: {exc}", file=sys.stderr, flush=True)
            n += 1
            if args.iterations == 0 or n < args.iterations:
                time.sleep(args.interval_seconds)
    elif args.command == "replay":
        records = []
        for issue in daily_issues(args.start, args.end, config):
            try:
                directory = run_forecast(config, issue, args.horizon, args.model, args.weather)
                records.append({"issue": issue.isoformat(), "status": "ok", "directory": str(directory)})
                print(f"{issue.isoformat()} {directory}", flush=True)
            except Exception as exc:
                records.append({"issue": issue.isoformat(), "status": "failed", "error": str(exc)})
                if not args.continue_on_error:
                    save_json(resolve(config, config["run"]["output_dir"]) / "replay.json", records)
                    raise
        save_json(resolve(config, config["run"]["output_dir"]) / "replay.json", records)
        if any(r["status"] != "ok" for r in records):
            return 2
    elif args.command == "evaluate":
        from .evaluation import evaluate
        directory, metrics = evaluate(config, args.start, args.end, args.models, args.weather, args.horizon,
                                      args.every_days, args.output, not args.no_select)
        print(metrics[metrics.horizon == "all"].to_string(index=False))
        print(f"Validation report: {directory}")
    elif args.command == "ingest":
        result = ingest_observations(config, args.turbine, args.file, received_at=args.received_at)
        log_event(resolve(config, config["run"]["output_dir"]), "ingested", turbine=args.turbine, result=result)
        print(json.dumps(result, default=str, ensure_ascii=False))
        if args.recalculate_issue:
            print(run_forecast(config, args.recalculate_issue, weather_mode=args.weather))
    elif args.command == "cache-weather":
        from .weather import WeatherStore
        store = WeatherStore(config, Path(config["_root"]))
        for issue in daily_issues(args.start, args.end, config):
            for turbine in ("turbine_1", "turbine_2"):
                store.get(issue, targets_for(issue, args.horizon), turbine, mode="network")
            print(f"Cached {issue.isoformat()}", flush=True)
    elif args.command == "verify-run":
        from .verification import verify_run
        print(json.dumps(verify_run(args.directory), ensure_ascii=False))
    return 0


def main(argv=None):
    try:
        return execute(parser().parse_args(argv))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR [{type(exc).__name__}]: {exc}", file=sys.stderr)
        return 2
