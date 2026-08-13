"""Fault-tolerant recorder, disk, catalog, and worker status aggregation."""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings
from shakepie_archive.constants import NOMINAL_SAMPLE_RATE_HZ

MAX_HEALTH_BYTES = 65_536


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "Unavailable"
    amount = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or suffix == "TiB":
            return f"{amount:.1f} {suffix}"
        amount /= 1024
    return "Unavailable"


def _human_uptime(seconds: float) -> str:
    days, remainder = divmod(int(seconds), 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _human_age(seconds: float) -> str:
    """Freshness of the newest archived sample, in the data-continuity style."""
    if seconds < 90:
        return f"{int(seconds)} s ago"
    if seconds < 5_400:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172_800:
        return f"{seconds / 3_600:.1f} h ago"
    return _human_uptime(seconds) + " ago"


def _system_metrics() -> dict[str, str]:
    """Best-effort host metrics in the spirit of the Pi dashboards; safe when absent."""
    metrics = {
        "cpu_load": "Unavailable",
        "soc_temperature": "Unavailable",
        "uptime": "Unavailable",
        "memory_used": "Unavailable",
    }
    try:
        with open("/proc/loadavg", encoding="ascii") as handle:
            fields = handle.read().split()
        if len(fields) >= 3:
            metrics["cpu_load"] = f"{fields[0]} / {fields[1]} / {fields[2]}"
    except OSError:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="ascii") as handle:
            metrics["soc_temperature"] = f"{int(handle.read().strip()) / 1000:.1f} °C"
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            metrics["uptime"] = _human_uptime(float(handle.read().split()[0]))
    except (OSError, ValueError):
        pass
    try:
        totals: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    totals[key] = int(value.strip().split()[0])
        if totals.get("MemTotal") and "MemAvailable" in totals:
            used = 100 * (totals["MemTotal"] - totals["MemAvailable"]) / totals["MemTotal"]
            metrics["memory_used"] = f"{used:.0f}%"
    except (OSError, ValueError):
        pass
    return metrics


def _timestamp_ns(value: Any) -> str:
    if not isinstance(value, int) or value <= 0:
        return "Unavailable"
    try:
        return datetime.fromtimestamp(value / 1e9, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return "Unavailable"


def _active_partial(directory: Path) -> str:
    try:
        candidates = [
            entry.name for entry in os.scandir(directory) if entry.name.endswith(".partial")
        ]
    except OSError:
        return "Unavailable"
    return max(candidates) if candidates else "None"


def soc_temperature() -> float | None:
    """Current SoC temperature in Celsius; None when unavailable (non-Pi hosts)."""
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="ascii")
        return int(raw.strip()) / 1000.0
    except (OSError, ValueError):
        return None


def read_health(path: Path, stale_after_seconds: int) -> tuple[dict[str, Any], str | None, bool]:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_HEALTH_BYTES:
            raise ValueError("health file is unsafe or too large")
        with path.open("rb") as handle:
            raw = handle.read(MAX_HEALTH_BYTES + 1)
        if len(raw) > MAX_HEALTH_BYTES:
            raise ValueError("health file is too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("health data is not an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return {}, str(error), True
    updated = value.get("updated_realtime_ns")
    stale = (
        not isinstance(updated, int)
        or time.time_ns() - updated > stale_after_seconds * 1_000_000_000
    )
    return value, None, stale


def status_snapshot(
    settings: Settings,
    catalog: Catalog,
    detector: Any | None = None,
) -> dict[str, Any]:
    raw, health_error, stale = read_health(
        settings.health_file, max(45, settings.web.status_refresh_seconds * 3)
    )
    recorder_raw = str(raw.get("state", "unknown"))
    fatal_code = str(raw.get("fatal_code", "none"))
    if health_error:
        recorder_state, recorder_level = "Unavailable", "gray"
    elif recorder_raw == "fatal":
        recorder_state, recorder_level = "Error", "red"
    elif stale:
        recorder_state, recorder_level = "Warning", "yellow"
    elif recorder_raw == "running":
        recorder_state, recorder_level = "Recording", "green"
    elif recorder_raw in {"starting", "stopping"}:
        recorder_state, recorder_level = recorder_raw.title(), "yellow"
    else:
        recorder_state, recorder_level = "Stopped", "gray"

    latest = catalog.latest_recording()
    index = catalog.index_status()
    latest_job = None
    with catalog.reader() as connection:
        latest_job = connection.execute(
            "SELECT state, progress, finished_at FROM dayplot_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    disk_total: int | None
    disk_used: int | None
    disk_free: int | None
    try:
        disk = shutil.disk_usage(settings.archive.mount_point)
        disk_total, disk_used, disk_free = disk.total, disk.used, disk.free
    except OSError:
        disk_total = disk_used = disk_free = None
    raw_rate = raw.get("nominal_group_rate_hz")
    rate_hz = (
        int(raw_rate)
        if isinstance(raw_rate, int) and raw_rate > 0
        else NOMINAL_SAMPLE_RATE_HZ
    )
    rate_millihz = rate_hz * 1000
    daily_bytes = int(86_400 * (rate_millihz / 1000) * 16 * 1.07)
    days_remaining = (
        f"{disk_free / daily_bytes:.1f}" if disk_free is not None and daily_bytes else "Unavailable"
    )
    adc_faults = int(raw.get("adc_faults", 0) or 0) + int(raw.get("adc_timeouts", 0) or 0)
    storage_errors = int(raw.get("storage_errors", 0) or 0)
    missing = int(raw.get("missing_sample_groups", 0) or 0)
    sequence_gaps = int(raw.get("simulated_sequence_gaps", 0) or 0)
    if health_error:
        adc_status, adc_level = "Unavailable", "gray"
        storage_status, storage_level = "Unavailable", "gray"
        timing_status, timing_level = "Unavailable", "gray"
    else:
        adc_status = (
            "Error" if fatal_code == "adc_fault" else ("Warning" if adc_faults else "Healthy")
        )
        adc_level = "red" if fatal_code == "adc_fault" else ("yellow" if adc_faults else "green")
        storage_status = (
            "Error"
            if fatal_code.startswith(("storage_", "hdd_"))
            else ("Warning" if storage_errors else "Ready")
        )
        storage_level = (
            "red"
            if fatal_code.startswith(("storage_", "hdd_"))
            else ("yellow" if storage_errors else "green")
        )
        timing_status = "Warning" if missing or sequence_gaps else "Synced"
        timing_level = "yellow" if missing or sequence_gaps else "green"
    index_level = "gray" if not index["total"] else ("yellow" if index["invalid"] else "green")
    if latest_job is None:
        dayplot_level = "gray"
    elif latest_job["state"] == "failed":
        dayplot_level = "red"
    elif latest_job["state"] in {"queued", "running"}:
        dayplot_level = "yellow"
    else:
        dayplot_level = "green"

    today_start_ns = int(
        datetime.combine(datetime.now(UTC).date(), datetime.min.time(), UTC).timestamp() * 1e9
    )
    events = catalog.event_summary(today_start_ns)
    now_ns = time.time_ns()
    last_sample_age = "Unavailable"
    if latest is not None and latest["end_ns"] is not None:
        last_sample_age = _human_age(max(0.0, (now_ns - int(latest["end_ns"])) / 1e9))
    today_day = datetime.now(UTC).date().isoformat()
    elapsed_today = max(1, now_ns - today_start_ns)
    today_coverage = f"{100 * catalog.day_coverage_ns(today_day) / elapsed_today:.1f} %"
    detector_snapshot: dict[str, Any] = (
        detector.snapshot()
        if detector is not None
        else {"state": "disabled", "error": "", "events_stored": 0}
    )
    if not settings.event_detector.enabled:
        detector_level, detector_state = "gray", "Disabled"
    elif detector_snapshot["state"] == "listening":
        detector_level, detector_state = "green", "Listening"
    elif detector_snapshot["state"] in {"connecting", "starting"}:
        detector_level, detector_state = "yellow", "Connecting"
    else:
        detector_level, detector_state = "red", "Unavailable"
    metrics = _system_metrics()
    return {
        "recorder_state": recorder_state,
        "recorder_level": recorder_level,
        "health_updated": _timestamp_ns(raw.get("updated_realtime_ns")),
        "health_stale": stale,
        "health_error": health_error,
        "station_id": str(raw.get("station_id") or (
            latest["station_id"] if latest is not None else "Unavailable"
        )),
        "sampling_rate": f"{rate_hz:g} Hz",
        "active_recording": _active_partial(settings.archive.data_directory),
        "adc_status": adc_status,
        "adc_level": adc_level,
        "storage_status": storage_status,
        "storage_level": storage_level,
        "timing_status": timing_status,
        "timing_level": timing_level,
        "missing_samples": missing,
        "sequence_gaps": sequence_gaps,
        "disk_queue_depth": int(raw.get("disk_queue_depth", 0) or 0),
        "disk_total": _human_bytes(disk_total),
        "disk_used": _human_bytes(disk_used),
        "disk_available": _human_bytes(disk_free),
        "disk_used_percent": (
            round(disk_used * 100 / disk_total, 1)
            if disk_used is not None and disk_total
            else 0
        ),
        "recording_days_remaining": days_remaining,
        "latest_recording": str(latest["filename"]) if latest is not None else "None indexed",
        "archive_index": (
            f"{index['total']} files · {index['invalid']} invalid"
            + (f" · updated {index['updated']}" if index["updated"] else "")
        ),
        "archive_index_level": index_level,
        "dayplot_generator": (
            f"{str(latest_job['state']).title()} · {latest_job['progress']}"
            if latest_job is not None
            else "Idle"
        ),
        "dayplot_level": dayplot_level,
        "detector_state": detector_state,
        "detector_level": detector_level,
        "detector_error": str(detector_snapshot.get("error") or ""),
        "events_today": events["today"],
        "events_total": events["total"],
        "last_event": (
            _timestamp_ns(events["last_started_ns"])
            if events["last_started_ns"] is not None
            else "None detected"
        ),
        "last_event_peak": (
            f"{events['last_peak']:,} counts" if events["last_peak"] is not None else "—"
        ),
        "latest_dayplot_day": catalog.latest_dayplot_day() or "",
        "last_sample_age": last_sample_age,
        "today_coverage": today_coverage,
        "zero_offset_north": int(raw.get("zero_offset_north", 0) or 0),
        "zero_offset_east": int(raw.get("zero_offset_east", 0) or 0),
        "zero_offset_vertical": int(raw.get("zero_offset_vertical", 0) or 0),
        "calibration_phase": str(raw.get("calibration_phase", "idle")),
        "calibration_outcome": str(raw.get("calibration_outcome", "none")),
        "calibration_progress": int(raw.get("calibration_progress", 0) or 0),
        "cpu_load": metrics["cpu_load"],
        "soc_temperature": metrics["soc_temperature"],
        "uptime": metrics["uptime"],
        "memory_used": metrics["memory_used"],
        "refreshed_at": datetime.now(UTC).strftime("%H:%M:%S UTC"),
    }
