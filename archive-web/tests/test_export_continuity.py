from __future__ import annotations

from conftest import write_recording
from flask.testing import FlaskClient

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings

DAY_START_NS = 1_735_689_600_000_000_000


def test_day_coverage_merges_overlapping_recordings(
    settings: Settings, catalog: Catalog
) -> None:
    write_recording(settings.archive.data_directory, "a.spb", start_ns=DAY_START_NS)
    write_recording(
        settings.archive.data_directory,
        "b.spb",
        start_ns=DAY_START_NS + 3_500_000_000_000,
    )
    catalog.reconcile(settings.archive.data_directory)
    with catalog.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE recordings SET end_ns = ? WHERE filename = 'a.spb'",
            (DAY_START_NS + 3_600_000_000_000,),
        )
        connection.execute(
            "UPDATE recordings SET end_ns = ? WHERE filename = 'b.spb'",
            (DAY_START_NS + 4_000_000_000_000,),
        )
    # 0-3600 s and 3500-4000 s merge to 4000 s of covered time, not 4100 s.
    assert catalog.day_coverage_ns("2025-01-01") == 4_000_000_000_000


def test_status_includes_continuity_metrics(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    write_recording(settings.archive.data_directory, "day.spb")
    catalog.reconcile(settings.archive.data_directory)
    payload = client.get("/api/status").get_json()
    assert "last_sample_age" in payload
    assert "today_coverage" in payload
    assert payload["today_coverage"].endswith("%")


def test_export_recording_streams_csv(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    write_recording(settings.archive.data_directory, "export-me.spb")
    catalog.reconcile(settings.archive.data_directory)
    row = catalog.latest_recording()
    assert row is not None
    response = client.get(f"/recordings/{row['id']}/export")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "export-me.csv" in response.headers["Content-Disposition"]
    lines = response.get_data(as_text=True).splitlines()
    assert lines[0] == "timestamp_utc,north_counts,east_counts,vertical_counts"
    assert len(lines) == 1 + int(row["sample_count"])
    assert lines[1].endswith(",1,2,3")


def test_export_rejects_unknown_recording(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    assert client.get("/recordings/9999/export").status_code == 404
