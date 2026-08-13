"""Single-consumer SQLite dayplot worker."""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings, load_settings, prepare_filesystem
from shakepie_archive.recorder_settings import read_station_id


@contextmanager
def worker_lock(path: Path) -> Iterator[None]:
    """Prevent two worker processes from recovering or claiming the same queue."""
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    handle = path.open("a+b")
    fcntl = None
    try:
        if os.name == "posix":
            fcntl = importlib.import_module("fcntl")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "another dayplot worker already holds the worker lock"
                ) from error
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def cleanup_temporary_files(directory: Path) -> int:
    removed = 0
    for candidate in directory.glob("shakepie-dayplot-*"):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            candidate.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def estimated_working_bytes(sample_rate_millihz: int) -> int:
    samples = (86_400 * sample_rate_millihz + 999) // 1000
    # HDD-backed float32 + mask are copied once into RAM because ObsPy deep-copies
    # its input Stream. Two in-memory channel/mask pairs plus library overhead stay bounded.
    return samples * 10 + 256 * 1024 * 1024


def enough_resources(settings: Settings, sample_rate_millihz: int) -> tuple[bool, str]:
    required_memory = estimated_working_bytes(sample_rate_millihz)
    memory_limit = settings.dayplot.memory_limit_mib * 1024 * 1024
    if required_memory > memory_limit:
        return False, (
            f"estimated {required_memory / 1024**2:.0f} MiB exceeds the "
            f"{settings.dayplot.memory_limit_mib} MiB memory limit"
        )
    free = shutil.disk_usage(settings.dayplot.temporary_directory).free
    configured_minimum = settings.dayplot.minimum_free_gib * 1024**3
    working_disk = ((86_400 * sample_rate_millihz + 999) // 1000) * 5
    required_disk = configured_minimum + working_disk
    if free < required_disk:
        return False, (
            f"only {free / 1024**3:.1f} GiB free; "
            f"at least {required_disk / 1024**3:.1f} GiB is required"
        )
    return True, ""


def _generator_command(settings: Settings, job_id: int) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "shakepie_archive.dayplot_generator",
        "--config",
        str(settings.source_path or "/etc/shakepie/archive-web.conf"),
        "--job-id",
        str(job_id),
    ]
    if os.name == "posix" and settings.dayplot.io_scheduling_class == "idle":
        ionice = shutil.which("ionice")
        if ionice:
            command = [ionice, "-c", "3", *command]
    return command


def process_one(settings: Settings, catalog: Catalog) -> bool:
    job = catalog.claim_job()
    if job is None:
        return False
    records = catalog.records_for_day(str(job["utc_day"]))
    if not records:
        catalog.update_job(
            int(job["id"]), "failed", "No source recordings", "source files disappeared"
        )
        return True
    sample_rate = min(
        settings.dayplot.target_sample_rate_hz * 1000,
        min(int(row["sample_rate_millihz"]) for row in records),
    )
    safe, reason = enough_resources(settings, sample_rate)
    if not safe:
        catalog.update_job(int(job["id"]), "failed", "Resource check failed", reason)
        return True
    environment = os.environ.copy()
    environment["MPLBACKEND"] = "Agg"
    try:
        result = subprocess.run(
            _generator_command(settings, int(job["id"])),
            env=environment,
            capture_output=True,
            text=True,
            timeout=12 * 60 * 60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        catalog.update_job(int(job["id"]), "failed", "Generator did not complete", str(error))
        return True
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "generator failed").strip()[-1000:]
        catalog.update_job(int(job["id"]), "failed", "Generator failed", message)
    else:
        completed = catalog.job(int(job["id"]))
        if completed is None or completed["state"] != "completed":
            catalog.update_job(
                int(job["id"]),
                "failed",
                "Generator exited without publishing images",
                "no completed cache record was written",
            )
    return True


def run_worker(settings: Settings, *, once: bool = False) -> None:
    prepare_filesystem(settings)
    with worker_lock(settings.archive.catalog_path.with_suffix(".worker.lock")):
        catalog = Catalog(settings.archive.catalog_path)
        catalog.initialize()
        cleanup_temporary_files(settings.dayplot.temporary_directory)
        catalog.recover_running_jobs()
        renamed, updated = catalog.migrate_dayplot_names(
            settings.dayplot.output_directory,
            read_station_id(settings.recorder_admin.recorder_conf_path),
        )
        orphans = catalog.cleanup_orphan_dayplots(settings.dayplot.output_directory)
        if renamed or orphans:
            print(
                f"dayplot naming migration: renamed={renamed} catalog_rows={updated} "
                f"orphans_removed={orphans}",
                flush=True,
            )
        next_auto_check = 0.0
        while True:
            if settings.dayplot.auto_generate_days:
                now = time.monotonic()
                if now >= next_auto_check:
                    queued = catalog.auto_queue_completed_dayplots()
                    if queued:
                        print(f"auto-queued dayplots for {', '.join(queued)}", flush=True)
                    next_auto_check = now + settings.dayplot.auto_check_seconds
            processed = process_one(settings, catalog)
            if once:
                return
            if not processed:
                time.sleep(settings.dayplot.idle_sleep_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the single ShakePie dayplot worker")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    run_worker(load_settings(arguments.config), once=arguments.once)


if __name__ == "__main__":
    main()
