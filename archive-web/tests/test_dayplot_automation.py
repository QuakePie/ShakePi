from __future__ import annotations

from collections.abc import Callable
from datetime import date

from conftest import write_recording
from flask.testing import FlaskClient

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings
from shakepie_archive.naming import canonical_dayplot_name

DAY_START_NS = 1_735_689_600_000_000_000  # 2025-01-01 00:00:00 UTC
DAY_NS = 86_400_000_000_000


def _catalog_completed_day(settings: Settings, catalog: Catalog) -> None:
    """Index one recording and stretch its indexed span to cover all of 2025-01-01."""
    write_recording(settings.archive.data_directory, "day.spb")
    catalog.reconcile(settings.archive.data_directory)
    with catalog.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE recordings SET start_ns = ?, end_ns = ?, sequence_gap_count = 0 "
            "WHERE filename = 'day.spb'",
            (DAY_START_NS, DAY_START_NS + DAY_NS - 1),
        )


def test_available_day_counts_lists_days_with_recordings(
    settings: Settings, catalog: Catalog
) -> None:
    write_recording(settings.archive.data_directory, "a.spb", start_ns=DAY_START_NS)
    write_recording(
        settings.archive.data_directory, "b.spb", start_ns=DAY_START_NS + 3_600_000_000_000
    )
    write_recording(
        settings.archive.data_directory, "c.spb", start_ns=DAY_START_NS + DAY_NS
    )
    catalog.reconcile(settings.archive.data_directory)

    counts = catalog.available_day_counts()
    assert counts == [
        {"date": "2025-01-01", "recordings": 2},
        {"date": "2025-01-02", "recordings": 1},
    ]


def test_auto_queue_dayplot_for_completed_day_only(
    settings: Settings, catalog: Catalog
) -> None:
    _catalog_completed_day(settings, catalog)
    write_recording(settings.archive.data_directory, "gap.spb", start_ns=DAY_START_NS + DAY_NS)
    catalog.reconcile(settings.archive.data_directory)

    # 2025-01-01 is complete; 2025-01-02 (a single short file) is not.
    queued = catalog.auto_queue_completed_dayplots(today=date(2025, 1, 3))
    assert queued == ["2025-01-01"]
    # The queued job is visible and a second pass does not queue it twice.
    job = catalog.latest_job("2025-01-01")
    assert job is not None and job["state"] == "queued"
    assert catalog.auto_queue_completed_dayplots(today=date(2025, 1, 3)) == []


def test_auto_queue_skips_days_with_cached_dayplots(
    settings: Settings, catalog: Catalog
) -> None:
    _catalog_completed_day(settings, catalog)
    catalog.auto_queue_completed_dayplots(today=date(2025, 1, 3))
    job = catalog.latest_job("2025-01-01")
    assert job is not None
    with catalog.transaction(immediate=True) as connection:
        connection.execute("UPDATE dayplot_jobs SET state = 'running' WHERE id = ?", (job["id"],))
        for channel in ("north", "east", "vertical"):
            connection.execute(
                "INSERT INTO dayplot_cache "
                "(utc_day, source_fingerprint, channel, relative_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "2025-01-01",
                    str(job["source_fingerprint"]),
                    channel,
                    f"2025-01-01/x-{channel}.png",
                    "2025-01-02T00:00:00+00:00",
                ),
            )
    assert catalog.auto_queue_completed_dayplots(today=date(2025, 1, 3)) == []


def test_migrate_dayplot_names_and_orphan_cleanup(settings: Settings, catalog: Catalog) -> None:
    _catalog_completed_day(settings, catalog)
    catalog.auto_queue_completed_dayplots(today=date(2025, 1, 3))
    job = catalog.latest_job("2025-01-01")
    assert job is not None
    day_directory = settings.dayplot.output_directory / "2025-01-01"
    day_directory.mkdir(parents=True, exist_ok=True)
    fingerprinted = day_directory / f"{job['source_fingerprint'][:16]}-north.png"
    fingerprinted.write_bytes(b"png")
    previous_convention = day_directory / "TEST_20250101_east_dayplot.png"
    previous_convention.write_bytes(b"png")
    orphan = day_directory / f"{'a' * 16}-vertical.png"
    orphan.write_bytes(b"png")
    with catalog.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE dayplot_jobs SET state = 'running' WHERE id = ?", (job["id"],)
        )
        for channel, path in (
            ("north", fingerprinted),
            ("east", previous_convention),
        ):
            connection.execute(
                "INSERT INTO dayplot_cache "
                "(utc_day, source_fingerprint, channel, relative_path, created_at) "
                "VALUES (?, ?, ?, ?, '2025-01-02T00:00:00+00:00')",
                (
                    "2025-01-01",
                    str(job["source_fingerprint"]),
                    channel,
                    str(path.relative_to(settings.dayplot.output_directory)),
                ),
            )

    renamed, updated = catalog.migrate_dayplot_names(
        settings.dayplot.output_directory, "Caspian"
    )
    assert (renamed, updated) == (2, 2)
    first = day_directory / canonical_dayplot_name("Caspian", "2025-01-01", "north", "png")
    second = day_directory / canonical_dayplot_name("Caspian", "2025-01-01", "east", "png")
    assert first.is_file() and second.is_file()
    assert not fingerprinted.exists() and not previous_convention.exists()
    with catalog.reader() as connection:
        paths = {
            str(row["channel"]): str(row["relative_path"])
            for row in connection.execute(
                "SELECT channel, relative_path FROM dayplot_cache"
            )
        }
    assert paths["north"] == str(first.relative_to(settings.dayplot.output_directory))
    assert paths["east"] == str(second.relative_to(settings.dayplot.output_directory))

    removed = catalog.cleanup_orphan_dayplots(settings.dayplot.output_directory)
    assert removed == 1
    assert not orphan.exists()


def test_queue_all_dayplots_forces_every_indexed_day(
    settings: Settings, catalog: Catalog
) -> None:
    _catalog_completed_day(settings, catalog)
    write_recording(settings.archive.data_directory, "gap.spb", start_ns=DAY_START_NS + DAY_NS)
    catalog.reconcile(settings.archive.data_directory)

    assert catalog.queue_all_dayplots() == ["2025-01-01", "2025-01-02"]
    for day in ("2025-01-01", "2025-01-02"):
        job = catalog.latest_job(day)
        assert job is not None and job["state"] == "queued"


def test_regenerate_all_route_queues_every_day(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
    catalog: Catalog,
) -> None:
    _catalog_completed_day(settings, catalog)
    csrf = authenticate()
    response = client.post(
        "/dayplot/regenerate-all",
        data={"csrf_token": csrf},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Queued regeneration for 1 day" in response.data
    job = catalog.latest_job("2025-01-01")
    assert job is not None and job["state"] == "queued"


def test_regenerate_all_route_reports_missing_sources(
    client: FlaskClient, authenticate: Callable[[], str]
) -> None:
    csrf = authenticate()
    response = client.post(
        "/dayplot/regenerate-all",
        data={"csrf_token": csrf},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"No indexed recordings" in response.data
