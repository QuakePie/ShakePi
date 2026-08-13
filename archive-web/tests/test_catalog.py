from __future__ import annotations

from conftest import write_recording

from shakepie_archive.catalog import SCHEMA_VERSION, Catalog
from shakepie_archive.config import Settings


def test_discovery_by_utc_day_ordering_pagination_and_partial_ignore(
    settings: Settings, catalog: Catalog
) -> None:
    day_start = 1_735_689_600_000_000_000
    for index in range(28, -1, -1):
        write_recording(
            settings.archive.data_directory,
            f"recording-{index:02d}.spb",
            start_ns=day_start + index * 10_000_000_000,
        )
    write_recording(settings.archive.data_directory, "active.partial")

    result = catalog.reconcile(settings.archive.data_directory)
    page = catalog.recordings_for_day("2025-01-01", 1, per_page=25)

    assert result == {"indexed": 29, "invalid": 0, "skipped": 0, "missing": 0}
    assert page.total == 29
    assert page.pages == 2
    assert [row["filename"] for row in page.items[:2]] == ["recording-00.spb", "recording-01.spb"]
    assert all(row["filename"] != "active.partial" for row in page.items)


def test_changed_file_is_revalidated_and_invalid_crc_is_cataloged(
    settings: Settings, catalog: Catalog
) -> None:
    path = write_recording(settings.archive.data_directory, "changed.spb")
    assert catalog.reconcile(settings.archive.data_directory)["indexed"] == 1
    assert catalog.reconcile(settings.archive.data_directory)["skipped"] == 1
    content = bytearray(path.read_bytes())
    content[-1] ^= 1
    path.write_bytes(content)

    assert catalog.reconcile(settings.archive.data_directory)["invalid"] == 1
    assert catalog.index_status()["invalid"] == 1


def test_schema_initialization_and_catalog_rebuild(settings: Settings) -> None:
    write_recording(settings.archive.data_directory, "rebuild.spb")
    first = Catalog(settings.archive.catalog_path)
    first.initialize()
    assert first.schema_version() == SCHEMA_VERSION
    assert first.reconcile(settings.archive.data_directory)["indexed"] == 1
    settings.archive.catalog_path.unlink()

    rebuilt = Catalog(settings.archive.catalog_path)
    rebuilt.initialize()
    assert rebuilt.reconcile(settings.archive.data_directory)["indexed"] == 1
    assert rebuilt.recordings_for_day("2025-01-01", 1).total == 1


def test_missing_finalized_file_is_marked_missing(settings: Settings, catalog: Catalog) -> None:
    path = write_recording(settings.archive.data_directory, "missing.spb")
    catalog.reconcile(settings.archive.data_directory)
    path.unlink()
    assert catalog.reconcile(settings.archive.data_directory)["missing"] == 1
    assert catalog.recordings_for_day("2025-01-01", 1).total == 0
