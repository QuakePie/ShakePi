from __future__ import annotations

import math

from conftest import write_recording
from flask.testing import FlaskClient

from shakepie_archive.analysis import hodogram, hv_ratio
from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings


def _seed_row(settings: Settings, catalog: Catalog, name: str, **kwargs) -> dict:
    write_recording(settings.archive.data_directory, name, **kwargs)
    catalog.reconcile(settings.archive.data_directory)
    return catalog.records_for_day("2025-01-01")[0]


def test_hodogram_recovers_linear_polarization(
    settings: Settings, catalog: Catalog
) -> None:
    # conftest samples lie on the line east = north + 1 -> azimuth 45 deg, linear
    row = _seed_row(settings, catalog, "line.spb")
    result = hodogram(
        settings.archive.data_directory,
        row,
        start_seconds=0.0,
        window_seconds=60.0,
        max_points=4000,
    )
    assert len(result["points"]) == 4
    assert result["azimuth_deg"] == 45.0
    assert result["linearity"] == 1.0
    assert result["decimation"] == 1
    assert result["window"]["seconds"] == 0.1  # clamped to the 20 ms recording


def test_hodogram_decimates_long_windows(
    settings: Settings, catalog: Catalog
) -> None:
    groups = [(index, 2 * index, 0) for index in range(400)]
    row = _seed_row(
        settings,
        catalog,
        "long.spb",
        samples_per_block=256,
        first_sequences=tuple(block * 256 for block in range(2)),
        samples=groups,
    )
    result = hodogram(
        settings.archive.data_directory,
        row,
        start_seconds=0.0,
        window_seconds=2.0,
        max_points=100,
    )
    assert 0 < len(result["points"]) <= 100
    assert result["decimation"] == 4  # 400 samples over 2 s capped to 100 points


def test_hv_ratio_finds_injected_resonance_peak(
    settings: Settings, catalog: Catalog
) -> None:
    """Horizontal channels carry a 5 Hz tone; the H/V peak must land there."""
    groups = []
    for index in range(4000):  # 20 s at 200 Hz
        moment = index / 200.0
        tone = round(20_000 * math.sin(2 * math.pi * 5.0 * moment))
        alternator = 50 if index % 2 == 0 else -50
        groups.append((tone + alternator // 2, tone - alternator // 2, alternator))
    row = _seed_row(
        settings,
        catalog,
        "noise.spb",
        samples_per_block=256,
        first_sequences=tuple(block * 256 for block in range(16)),
        samples=groups,
    )
    result = hv_ratio(settings.archive.data_directory, row)
    assert result["segments"] >= 5
    assert len(result["frequencies_hz"]) == len(result["h_over_v"]) > 10
    assert all(value > 0 for value in result["h_over_v"])
    assert result["peak_frequency_hz"] is not None
    assert abs(result["peak_frequency_hz"] - 5.0) < 0.7


def test_hodogram_endpoint_rejects_bad_parameters(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    row = _seed_row(settings, catalog, "bad.spb")
    response = client.get(f"/recordings/{int(row['id'])}/hodogram?start=abc")
    assert response.status_code == 400
    assert client.get("/recordings/9999/hodogram").status_code == 404
    assert client.get("/recordings/9999/hv").status_code == 404


def test_analysis_endpoints_return_payloads(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    row = _seed_row(settings, catalog, "plot.spb")
    hodogram_response = client.get(f"/recordings/{int(row['id'])}/hodogram?start=0&seconds=1")
    assert hodogram_response.status_code == 200
    payload = hodogram_response.get_json()
    assert payload["points"] and payload["azimuth_deg"] == 45.0
    hv_response = client.get(f"/recordings/{int(row['id'])}/hv")
    assert hv_response.status_code == 200
    hv_payload = hv_response.get_json()
    assert hv_payload["frequencies_hz"] == [] or hv_payload["peak_frequency_hz"] is not None
