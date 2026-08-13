from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest
from conftest import write_recording

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings
from shakepie_archive.dayplot_generator import (
    _configure_matplotlib,
    _dayplot_envelope,
)
from shakepie_archive.dayplot_jobs import cleanup_temporary_files, enough_resources
from shakepie_archive.health import read_health, status_snapshot


def test_matplotlib_cache_uses_worker_writable_directory(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MPLCONFIGDIR", raising=False)

    _configure_matplotlib(settings)

    expected = settings.dayplot.temporary_directory / "matplotlib"
    assert os.environ["MPLCONFIGDIR"] == str(expected)
    assert expected.is_dir()


def test_dayplot_envelope_is_bounded_and_preserves_gaps() -> None:
    data = np.array([3, -4, 8, 5, 100, 200, -7, 9], dtype=np.float32)
    mask = np.array([False, False, False, False, True, True, False, False])

    envelope = _dayplot_envelope(data, mask, requested_bins=4)

    assert envelope.shape == (8,)
    assert envelope[:4].tolist() == [3.0, -4.0, 8.0, 5.0]
    assert np.ma.getmaskarray(envelope)[4:6].all()
    assert envelope[6:].tolist() == [-7.0, 9.0]


def test_job_deduplication_cache_invalidation_and_restart_recovery(
    settings: Settings, catalog: Catalog
) -> None:
    source = write_recording(settings.archive.data_directory, "plot.spb")
    catalog.reconcile(settings.archive.data_directory)
    first, state = catalog.queue_dayplot("2025-01-01")
    duplicate, duplicate_state = catalog.queue_dayplot("2025-01-01")
    assert (duplicate, duplicate_state) == (first, state)
    claimed = catalog.claim_job()
    assert claimed is not None and claimed["state"] == "running"
    assert catalog.recover_running_jobs() == 1
    assert catalog.claim_job()["id"] == first

    catalog.update_job(first, "completed", "done")
    old_fingerprint = catalog.source_fingerprint("2025-01-01")
    with catalog.transaction(immediate=True) as connection:
        for channel in ("north", "east", "vertical"):
            connection.execute(
                "INSERT INTO dayplot_cache VALUES (?, ?, ?, ?, ?)",
                ("2025-01-01", old_fingerprint, channel, f"old-{channel}.png", "now"),
            )
    assert len(catalog.cached_dayplot("2025-01-01")) == 3
    with pytest.raises(FileExistsError):
        catalog.queue_dayplot("2025-01-01")

    content = source.read_bytes()
    source.write_bytes(content)
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1_000_000))
    catalog.reconcile(settings.archive.data_directory)
    assert catalog.source_fingerprint("2025-01-01") != old_fingerprint
    assert catalog.cached_dayplot("2025-01-01") == {}


def test_current_day_keeps_latest_completed_snapshot_when_new_files_arrive(
    settings: Settings, catalog: Catalog
) -> None:
    today = datetime.now(UTC)
    source = write_recording(
        settings.archive.data_directory,
        "current.spb",
        start_ns=int(today.timestamp() * 1_000_000_000),
    )
    utc_day = today.date().isoformat()
    catalog.reconcile(settings.archive.data_directory)
    old_fingerprint = catalog.source_fingerprint(utc_day)
    with catalog.transaction(immediate=True) as connection:
        for channel in ("north", "east", "vertical"):
            connection.execute(
                "INSERT INTO dayplot_cache VALUES (?, ?, ?, ?, ?)",
                (utc_day, old_fingerprint, channel, f"snapshot-{channel}.png", "now"),
            )
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1_000_000))
    catalog.reconcile(settings.archive.data_directory)
    assert catalog.source_fingerprint(utc_day) != old_fingerprint
    assert len(catalog.cached_dayplot(utc_day)) == 3


def test_missing_malformed_and_stale_health_are_safe(settings: Settings, catalog: Catalog) -> None:
    raw, error, stale = read_health(settings.health_file, 45)
    assert raw == {} and error and stale
    settings.health_file.write_text("not json", encoding="utf-8")
    assert read_health(settings.health_file, 45)[1]
    settings.health_file.write_text(
        json.dumps({"state": "running", "updated_realtime_ns": time.time_ns() - 60_000_000_000}),
        encoding="utf-8",
    )
    snapshot = status_snapshot(settings, catalog)
    assert snapshot["recorder_state"] == "Warning"
    assert snapshot["health_stale"] is True
    assert snapshot["sampling_rate"] == "200 Hz"


def test_resource_bounds_and_temporary_cleanup(settings: Settings) -> None:
    settings.dayplot.temporary_directory.mkdir(parents=True)
    temporary = settings.dayplot.temporary_directory / "shakepie-dayplot-abandoned.data"
    temporary.write_bytes(b"temporary")
    unrelated = settings.dayplot.temporary_directory / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    assert cleanup_temporary_files(settings.dayplot.temporary_directory) == 1
    assert not temporary.exists() and unrelated.exists()
    enough, message = enough_resources(settings, 10_000_000)
    assert enough is False
    assert "memory limit" in message


def test_missing_hdd_mount_is_reported(settings: Settings) -> None:
    from dataclasses import replace

    from shakepie_archive.config import ConfigurationError, prepare_filesystem

    missing_archive = replace(
        settings.archive, mount_point=settings.archive.mount_point / "missing"
    )
    with pytest.raises(ConfigurationError, match="mount is unavailable"):
        prepare_filesystem(replace(settings, archive=missing_archive))


def test_failed_job_records_error(settings: Settings, catalog: Catalog) -> None:
    write_recording(settings.archive.data_directory, "failed.spb")
    catalog.reconcile(settings.archive.data_directory)
    job_id, _ = catalog.queue_dayplot("2025-01-01")
    catalog.claim_job()
    catalog.update_job(job_id, "failed", "Resource check failed", "insufficient free space")
    job = catalog.job(job_id)
    assert job is not None
    assert job["state"] == "failed"
    assert "insufficient" in job["error_message"]


def test_short_lived_generator_writes_three_cached_static_images(
    settings: Settings, catalog: Catalog
) -> None:
    low_rate_settings = replace(
        settings,
        dayplot=replace(settings.dayplot, width_pixels=640, minimum_free_gib=1),
    )
    write_recording(
        settings.archive.data_directory,
        "low-rate.spb",
        sample_rate_millihz=1_000,
    )
    catalog.reconcile(settings.archive.data_directory)
    job_id, _ = catalog.queue_dayplot("2025-01-01")
    assert catalog.claim_job() is not None

    # Production runs the generator in a short-lived child. Its hard memory limit
    # must not shrink the pytest process or affect the tests that run afterwards.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pickle, sys; "
            "from shakepie_archive.dayplot_generator import generate_job; "
            "generate_job(pickle.loads(sys.stdin.buffer.read()), int(sys.argv[1]))",
            str(job_id),
        ],
        input=pickle.dumps(low_rate_settings),
        env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    cache = catalog.cached_dayplot("2025-01-01")
    assert set(cache) == {"north", "east", "vertical"}
    assert all(
        (settings.dayplot.output_directory / row["relative_path"]).stat().st_size > 0
        for row in cache.values()
    )
    assert not list(settings.dayplot.temporary_directory.glob("shakepie-dayplot-*"))
