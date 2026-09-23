"""Provenance and interpolation tests; all fabricated fixtures are test-only."""
from pathlib import Path
from hashlib import sha256
import json

import pandas as pd
import pytest

from windforecast.weather import WeatherStore, WeatherError, WeatherUnavailableError, _ranges, _validate_source


@pytest.fixture
def store(tmp_path):
    return WeatherStore({"weather": {"grid": "1p00"}, "turbines": {
        "turbine_1": {"latitude": 43.645150, "longitude": 78.535604}}}, tmp_path)


def fake_step(run, lead, modified="2026-01-31T16:00:00Z"):
    # U changes sign, so interpolating vector components differs from speed.
    values = {"u10": 3 if lead == 6 else -3, "v10": 0, "temperature_k": 273.15 + lead}
    return {"lead": lead, "source": {"object_available_at": modified},
            "points": {"fields": {name: {"points": {"turbine_1": {
                "value": value, "grid_latitude": 44.0, "grid_longitude": 79.0, "distance_km": 54.0}}}
                for name, value in values.items()}}, "manifest_path": "test-only.json", "points_path": "test-only-points.json"}


def test_no_network_in_empty_cache(store, monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **kw: pytest.fail("Cache mode accessed network"))
    with pytest.raises(WeatherUnavailableError, match="No verified cached"):
        store.get(pd.Timestamp("2026-01-31T18:00Z"), pd.date_range("2026-01-31T19:00Z", periods=2, freq="h"), "turbine_1")


def test_issue_buffer_and_vector_interpolation(store, monkeypatch):
    monkeypatch.setattr(store, "_step", lambda run, lead, mode: fake_step(run, lead))
    issue = pd.Timestamp("2026-01-31T18:00Z")
    output = store.get(issue, pd.date_range("2026-01-31T19:00Z", periods=2, freq="h"), "turbine_1")
    assert store.run_for_issue(issue) == pd.Timestamp("2026-01-31T12:00Z")
    assert output["wind_speed"].tolist() == [1.0, 1.0]
    assert output["temperature"].tolist() == pytest.approx([7.0, 8.0])
    assert (output["weather_available_at"] <= issue).all()


def test_old_run_republished_after_issue_is_rejected(store, monkeypatch):
    monkeypatch.setattr(store, "_step", lambda run, lead, mode: fake_step(run, lead, "2026-02-01T00:00Z"))
    with pytest.raises(WeatherError, match="after issue"):
        store.get(pd.Timestamp("2026-01-31T18:00Z"), pd.date_range("2026-01-31T19:00Z", periods=2, freq="h"), "turbine_1")


def test_index_wrong_vintage_fails():
    with pytest.raises(WeatherError, match="different initialisation"):
        _ranges("1:0:d=2026020112:TMP:2 m above ground:6 hour fcst:\n2:100:d=2026020112:RH:2 m above ground:6 hour fcst:",
                pd.Timestamp("2026-01-31T12:00Z"), 6)


def test_naive_timestamp_fails(store):
    with pytest.raises(WeatherError, match="timezone"):
        store.run_for_issue(pd.Timestamp("2026-01-31T18:00"))


def test_disallowed_provider_fails(tmp_path):
    with pytest.raises(WeatherError, match="operational"):
        WeatherStore({"weather": {"provider": "reanalysis"}}, Path(tmp_path))


def test_cached_availability_recomputed_from_all_source_files():
    run = pd.Timestamp("2026-01-31T12:00Z")
    source = {"version": 1, "provider": "noaa_gfs", "grid": "1p00", "run_time": run.isoformat(),
              "lead_hours": 6, "object_available_at": "2026-01-31T16:00Z", "files": {
                  filename: {"last_modified": "2026-01-31T16:00Z"}
                  for filename in ["index.idx", "u10.grib2", "v10.grib2", "temperature_k.grib2"]}}
    _validate_source(source, run, 6, "1p00")
    source["files"]["u10.grib2"]["last_modified"] = "2026-02-01T00:00Z"
    with pytest.raises(WeatherError, match="availability differs"):
        _validate_source(source, run, 6, "1p00")


def test_cached_source_must_include_every_raw_field():
    run = pd.Timestamp("2026-01-31T12:00Z")
    with pytest.raises(WeatherError, match="file set"):
        _validate_source({"version": 1, "provider": "noaa_gfs", "grid": "1p00", "run_time": run.isoformat(),
                          "lead_hours": 6, "files": {}}, run, 6, "1p00")


@pytest.mark.parametrize("corruption", ["availability", "raw_bytes"])
def test_cached_raw_source_validated_before_point_rebuild(store, corruption, monkeypatch):
    run = pd.Timestamp("2026-01-31T12:00Z")
    directory = store.cache / "noaa_gfs" / "1p00" / "20260131T1200Z" / "f006"
    directory.mkdir(parents=True)
    raw = b"test-only fixture, not operational GRIB"
    files = {}
    for filename in ["index.idx", "u10.grib2", "v10.grib2", "temperature_k.grib2"]:
        (directory / filename).write_bytes(raw)
        files[filename] = {"last_modified": "2026-01-31T16:00Z", "sha256": sha256(raw).hexdigest()}
    source = {"version": 1, "provider": "noaa_gfs", "grid": "1p00", "run_time": run.isoformat(),
              "lead_hours": 6, "object_available_at": "2026-01-31T16:00Z", "files": files}
    if corruption == "availability":
        source["files"]["v10.grib2"]["last_modified"] = "2026-02-01T00:00Z"
    else:
        (directory / "v10.grib2").write_bytes(b"corrupted bytes")
    (directory / "source.json").write_text(json.dumps(source), encoding="utf-8")
    monkeypatch.setattr(store, "_decode", lambda *a: pytest.fail("Corrupt source reached decoder"))
    monkeypatch.setattr("requests.get", lambda *a, **kw: pytest.fail("Cache rebuild accessed network"))
    with pytest.raises(WeatherError, match="availability differs|checksum mismatch"):
        store._step(run, 6, "cache")
