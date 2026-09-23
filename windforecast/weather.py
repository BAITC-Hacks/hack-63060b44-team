"""Issue-time-safe retrieval of operational NOAA GFS forecasts.

The archive is selected by model initialisation, never by target date alone.
Every GRIB is checked against its requested run and forecast lead. S3 object
Last-Modified dates provide a conservative publication upper bound: objects
rewritten after an issue are rejected even if their nominal run is older.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import math
from pathlib import Path
import time
from threading import Lock
from typing import Any
import uuid

import numpy as np
import pandas as pd
import requests


GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
FIELDS = {
    "temperature_k": ("TMP", "2 m above ground", "2t", "K"),
    "u10": ("UGRD", "10 m above ground", "10u", "m s**-1"),
    "v10": ("VGRD", "10 m above ground", "10v", "m s**-1"),
}
CACHE_VERSION = 1
_ECCODES_LOCK = Lock()  # Native definition loading is not thread-safe on all builds.


class WeatherError(RuntimeError):
    """Weather cannot be used without compromising provenance or completeness."""


class WeatherUnavailableError(WeatherError):
    """The requested operational forecast or its verified cache is unavailable."""


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise WeatherError("Weather timestamps must explicitly include a timezone.")
    return stamp.tz_convert("UTC")


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def _write_json(path: Path, value: dict) -> None:
    _atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode())


def _ranges(index_text: str, run: pd.Timestamp, lead: int) -> dict:
    lines = [line.split(":") for line in index_text.splitlines() if line.strip()]
    result = {}
    for position, fields in enumerate(lines):
        if len(fields) < 6:
            raise WeatherError("Malformed NOAA GRIB index.")
        for name, (parameter, level, _, _) in FIELDS.items():
            if fields[3:5] != [parameter, level]:
                continue
            if name in result:
                raise WeatherError(f"Duplicate weather field: {name}")
            if fields[2] != "d=" + run.strftime("%Y%m%d%H"):
                raise WeatherError("NOAA index contains a different initialisation time.")
            expected = "anl" if lead == 0 else f"{lead} hour fcst"
            if fields[5] != expected:
                raise WeatherError(f"Unexpected lead in NOAA index: {fields[5]!r}")
            if position + 1 == len(lines):
                raise WeatherError("Cannot bound required field at end of GRIB index.")
            start, end = int(fields[1]), int(lines[position + 1][1]) - 1
            if start < 0 or end < start:
                raise WeatherError("Invalid GRIB byte range.")
            result[name] = {"start": start, "end": end, "index_entry": ":".join(fields)}
    if set(result) != set(FIELDS):
        raise WeatherUnavailableError("NOAA archive lacks required 10 m wind / 2 m temperature fields.")
    return result


def _validate_source(source: dict, run: pd.Timestamp, lead: int, grid: str) -> None:
    """Do not trust a cached aggregate availability instead of field metadata."""
    expected_files = {"index.idx"} | {name + ".grib2" for name in FIELDS}
    if (source.get("version") != CACHE_VERSION or source.get("provider") != "noaa_gfs"
            or source.get("run_time") != run.isoformat() or source.get("lead_hours") != lead
            or source.get("grid") != grid or set(source.get("files", {})) != expected_files):
        raise WeatherError("Invalid weather cache identity or source file set.")
    modified = [_utc(item["last_modified"]) for item in source["files"].values()]
    if min(modified) < run:
        raise WeatherError("Weather publication metadata precedes its model initialisation.")
    if max(modified) != _utc(source["object_available_at"]):
        raise WeatherError("Cached weather availability differs from source object Last-Modified values.")


class WeatherStore:
    """Persistent exact-vintage archive, with zero-network cache mode.

    ``config`` is the full project configuration (``weather`` and ``turbines``
    mappings). Coordinates are mandatory. Default 1 degree fields are native
    three-hour forecasts. U/V components and temperature are interpolated only
    between adjacent valid times in the *same already-published model run*.
    """

    def __init__(self, config: dict, root: Path):
        self.config = config
        self.options = config.get("weather", {})
        if self.options.get("provider", "noaa_gfs") != "noaa_gfs":
            raise WeatherError("Only verified operational noaa_gfs weather is supported.")
        self.root = Path(root).resolve()
        self.cache = self.root / self.options.get("cache_dir", "data/weather")
        self.grid = self.options.get("grid", "1p00")
        if self.grid not in {"0p25", "0p50", "1p00"}:
            raise WeatherError("GFS grid must be 0p25, 0p50 or 1p00.")
        self.step_hours = 1 if self.grid == "0p25" else 3
        self.delay = float(self.options.get("publication_delay_hours", 6))
        if not math.isfinite(self.delay) or self.delay < 0:
            raise WeatherError("publication_delay_hours must be finite and nonnegative.")
        self.workers = max(1, min(8, int(self.options.get("max_workers", 4))))
        self.retries = max(1, min(5, int(self.options.get("retries", 3))))
        self.timeout = float(self.options.get("timeout_seconds", 45))
        self.coords = {}
        for name, turbine in config.get("turbines", {}).items():
            lat, lon = float(turbine["latitude"]), float(turbine["longitude"])
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                raise WeatherError(f"Invalid coordinates for {name}.")
            self.coords[name] = {"latitude": lat, "longitude": lon}
        if not self.coords:
            raise WeatherError("No turbine coordinates configured; refusing guessed coordinates.")
        self.coords_id = _digest(self.coords)[:16]

    def run_for_issue(self, issue: pd.Timestamp) -> pd.Timestamp:
        """Latest 6-hour model cycle after the configured publication buffer."""
        return (_utc(issue) - pd.Timedelta(hours=self.delay)).floor("6h")

    def _request(self, url: str, headers: dict | None = None) -> requests.Response:
        error = None
        for attempt in range(self.retries):
            try:
                response = requests.get(url, headers=headers, timeout=self.timeout)
                if response.status_code == 404:
                    raise WeatherUnavailableError(f"Archive object not found: {url}")
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                elif response.status_code >= 400:
                    raise WeatherError(f"Weather server HTTP {response.status_code}: {url}")
                return response
            except (requests.RequestException, OSError) as exc:
                error = exc
                if attempt + 1 < self.retries:
                    time.sleep(min(2 ** attempt, 4))
        raise WeatherUnavailableError(f"Weather request failed after {self.retries} attempts: {url}: {error}")

    @staticmethod
    def _response_meta(response: requests.Response) -> dict:
        modified = response.headers.get("Last-Modified")
        if not modified:
            raise WeatherError("NOAA response has no Last-Modified; cannot prove issue-time availability.")
        return {
            "url": response.url,
            "last_modified": _utc(modified).isoformat(),
            "etag": response.headers.get("ETag"),
            "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "sha256": sha256(response.content).hexdigest(),
            "size_bytes": len(response.content),
            "http_status": response.status_code,
        }

    def _decode(self, raw: bytes, name: str, run: pd.Timestamp, lead: int) -> dict:
        try:
            import eccodes
        except (ImportError, RuntimeError) as exc:
            raise WeatherError("Reading raw GFS GRIB requires ecCodes: install project dependencies.") from exc
        handle = None
        try:
            handle = eccodes.codes_new_from_message(raw)
            get = lambda key: eccodes.codes_get(handle, key)
            valid = run + pd.Timedelta(hours=lead)
            expected = {"dataDate": int(run.strftime("%Y%m%d")), "dataTime": run.hour * 100,
                        "validityDate": int(valid.strftime("%Y%m%d")), "validityTime": valid.hour * 100,
                        "shortName": FIELDS[name][2], "units": FIELDS[name][3]}
            actual = {key: get(key) for key in expected}
            if actual != expected or int(get("endStep")) != lead:
                raise WeatherError(f"GRIB forecast vintage / field mismatch: {actual}; expected {expected}")
            if get("stepType") != "instant" or int(get("edition")) != 2:
                raise WeatherError("Only instantaneous GRIB2 weather fields are supported.")
            points = {}
            for turbine, coords in self.coords.items():
                nearest = eccodes.codes_grib_find_nearest(handle, coords["latitude"], coords["longitude"])[0]
                value = float(nearest["value"])
                if not math.isfinite(value) or abs(value) > 1e6:
                    raise WeatherError("Non-finite or missing weather value in GRIB.")
                points[turbine] = {"value": value, "grid_latitude": float(nearest["lat"]),
                                   "grid_longitude": float(nearest["lon"]), "distance_km": float(nearest["distance"])}
            return {"grib_keys": actual, "points": points}
        except WeatherError:
            raise
        except Exception as exc:
            raise WeatherError(f"Cannot decode or validate {name} from archived GFS GRIB: {exc}") from exc
        finally:
            if handle is not None:
                eccodes.codes_release(handle)

    def _step(self, run: pd.Timestamp, lead: int, mode: str) -> dict:
        directory = self.cache / "noaa_gfs" / self.grid / run.strftime("%Y%m%dT%H%MZ") / f"f{lead:03d}"
        point_file = directory / f"points-{self.coords_id}.json"
        raw_meta_file = directory / "source.json"
        if raw_meta_file.exists():
            source = json.loads(raw_meta_file.read_text(encoding="utf-8"))
            _validate_source(source, run, lead, self.grid)
            for filename, metadata in source["files"].items():
                path = directory / filename
                if not path.is_file() or sha256(path.read_bytes()).hexdigest() != metadata["sha256"]:
                    raise WeatherError(f"Weather cache checksum mismatch or missing file: {path}")
        else:
            if mode == "cache":
                raise WeatherUnavailableError(f"No verified cached weather: {raw_meta_file}. Run network mode to populate it.")
            stem = f"gfs.t{run.hour:02d}z.pgrb2.{self.grid}.f{lead:03d}"
            url = f"{GFS_BASE}/gfs.{run:%Y%m%d}/{run.hour:02d}/atmos/{stem}"
            response = self._request(url + ".idx")
            index_meta = self._response_meta(response)
            ranges = _ranges(response.text, run, lead)
            _atomic_bytes(directory / "index.idx", response.content)
            source = {"version": CACHE_VERSION, "provider": "noaa_gfs", "grid": self.grid,
                      "run_time": run.isoformat(), "lead_hours": lead, "files": {"index.idx": index_meta}}
            for name, entry in ranges.items():
                range_header = f"bytes={entry['start']}-{entry['end']}"
                response = self._request(url, {"Range": range_header})
                expected_range = f"bytes {entry['start']}-{entry['end']}/"
                if response.status_code != 206 or not response.headers.get("Content-Range", "").startswith(expected_range) or len(response.content) != entry["end"] - entry["start"] + 1:
                    raise WeatherError("Server ignored or returned an incorrect GRIB byte range.")
                metadata = self._response_meta(response)
                metadata.update(entry)
                metadata["request_headers"] = {"Range": range_header}
                filename = name + ".grib2"
                _atomic_bytes(directory / filename, response.content)
                source["files"][filename] = metadata
            source["object_available_at"] = max(item["last_modified"] for item in source["files"].values())
            _validate_source(source, run, lead, self.grid)
            _write_json(raw_meta_file, source)
        if point_file.exists():
            points = json.loads(point_file.read_text(encoding="utf-8"))
            checksum = points.pop("checksum", None)
            if checksum != _digest(points) or points.get("source_digest") != _digest(source) or points.get("coordinates") != self.coords:
                raise WeatherError(f"Invalid extracted weather cache checksum: {point_file}")
        else:
            points = {"coordinates": self.coords, "source_digest": _digest(source), "fields": {}}
            with _ECCODES_LOCK:
                for name in FIELDS:
                    points["fields"][name] = self._decode((directory / (name + ".grib2")).read_bytes(), name, run, lead)
            saved = dict(points, checksum=_digest(points))
            _write_json(point_file, saved)
        return {"lead": lead, "source": source, "points": points, "manifest_path": str(raw_meta_file),
                "points_path": str(point_file)}

    def get(self, issue: pd.Timestamp, targets: pd.DatetimeIndex, turbine: str, mode: str = "cache") -> pd.DataFrame:
        """Return a complete verified forecast; never fall back to observations.

        Cache and network modes select the same deterministic run. Network mode
        only downloads missing objects, preserving idempotent replay. Failure of
        that run is explicit: no silent switch to a newer run or reanalysis.
        """
        issue = _utc(issue)
        if mode not in {"cache", "network"}:
            raise WeatherError("Weather mode must be 'cache' or 'network'.")
        targets = pd.DatetimeIndex(targets)
        if targets.tz is None:
            raise WeatherError("Weather target timestamps must include a timezone.")
        targets = targets.tz_convert("UTC")
        if turbine not in self.coords:
            raise WeatherError(f"Unknown turbine: {turbine}")
        if len(targets) == 0 or targets.has_duplicates or not targets.is_monotonic_increasing or (targets <= issue).any():
            raise WeatherError("Weather targets must be unique, increasing and after issue time.")
        run = self.run_for_issue(issue)
        offsets = np.asarray((targets - run) / pd.Timedelta(hours=1), dtype=float)
        if not np.isfinite(offsets).all() or (offsets < 0).any() or (offsets > 120).any():
            raise WeatherError("This provider supports forecast hours 0 through 120 only.")
        leads = sorted({int(value) for value in np.floor(offsets / self.step_hours) * self.step_hours} |
                       {int(value) for value in np.ceil(offsets / self.step_hours) * self.step_hours})
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            steps = list(executor.map(lambda lead: self._step(run, lead, mode), leads))
        available = max([run + pd.Timedelta(hours=self.delay)] +
                        [_utc(step["source"]["object_available_at"]) for step in steps])
        if available > issue:
            raise WeatherError(f"Archived weather became available {available.isoformat()}, after issue {issue.isoformat()}; rejecting possible hindsight.")
        field_values = {}
        for name in FIELDS:
            values = [step["points"]["fields"][name]["points"][turbine]["value"] for step in steps]
            field_values[name] = np.interp(offsets, leads, values)
        frame = pd.DataFrame({
            "wind_speed": np.hypot(field_values["u10"], field_values["v10"]),
            "temperature": field_values["temperature_k"] - 273.15,
            "weather_run_time": run,
            "weather_available_at": available,
            "weather_source": f"NOAA_GFS_operational_{self.grid}_nearest_{self.step_hours}h_linear_uv",
        }, index=targets.rename("target_time"))
        if not np.isfinite(frame[["wind_speed", "temperature"]].to_numpy()).all():
            raise WeatherError("Non-finite weather after alignment.")
        if (frame["wind_speed"] > 150).any() or not frame["temperature"].between(-100, 70).all():
            raise WeatherError("Weather outside physically plausible bounds; refusing silent corrections.")
        provenance = [{"lead_hours": step["lead"], "source_digest": _digest(step["source"]),
                       "manifest_path": step["manifest_path"], "points_path": step["points_path"]} for step in steps]
        first_point = steps[0]["points"]["fields"]["u10"]["points"][turbine]
        frame.attrs["weather_manifest"] = {
            "provider": "noaa_gfs", "grid": self.grid, "run_time": run.isoformat(),
            "available_at": available.isoformat(), "issue_time": issue.isoformat(),
            "coordinates": self.coords[turbine], "nearest_grid_point": first_point,
            "native_step_hours": self.step_hours, "interpolation": "linear U/V and temperature within one forecast run",
            "publication_delay_hours": self.delay, "files": provenance,
            "fingerprint": _digest({"coordinates": self.coords[turbine], "files": [
                {"lead_hours": item["lead_hours"], "source_digest": item["source_digest"]} for item in provenance]}),
        }
        return frame
