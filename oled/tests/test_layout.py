from __future__ import annotations

import math

from shakepie_oled.display import (
    CpuUsage,
    build_lines,
    disk_percent_free_and_days,
    format_days,
    health_compact,
    is_stale,
)


def running_health(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "state": "running",
        "fatal_code": "none",
        "nominal_group_rate_hz": 200,
        "storage_errors": 0,
        "adc_faults": 0,
        "adc_timeouts": 0,
        "missing_sample_groups": 0,
        "simulated_sequence_gaps": 0,
        "updated_realtime_ns": 1,
    }
    base.update(overrides)
    return base


class FakeTime:
    """Freeze time.time_ns via monkeypatched module constant."""

    def __init__(self, now_ns: int) -> None:
        self.now_ns = now_ns


def test_is_stale_detects_old_updates(monkeypatch) -> None:
    now_ms = 1_000_000_000_000_000_000  # ns
    monkeypatch.setattr("shakepie_oled.display.time.time_ns", lambda: now_ms)
    fresh = running_health(updated_realtime_ns=now_ms - 5_000_000_000)
    old = running_health(updated_realtime_ns=now_ms - 120_000_000_000)
    assert not is_stale(fresh)
    assert is_stale(old)
    assert is_stale(None)


def test_format_days_buckets_long_horizons() -> None:
    assert format_days(124.0) == "~124d"
    assert format_days(1500.0) == "3y+"


def test_disk_percent_free_and_days(tmp_path) -> None:
    # disk_percent_free_and_days uses shutil on a real directory; fake via monkeypatch
    import shutil

    class FakeUsage:
        total = 1_000_000_000
        used = 780_000_000
        free = 220_000_000

    original = shutil.disk_usage
    shutil.disk_usage = lambda _path: FakeUsage  # type: ignore[assignment]
    try:
        percent, free, days = disk_percent_free_and_days(tmp_path, 200)
    finally:
        shutil.disk_usage = original
    assert percent == 78
    assert free == 220_000_000
    # bytes per day = 200 * 16 * 1.07 * 86400 ~= 296 MB -> 220e9 / 296e6 ~= 743 days
    expected = 220_000_000 / (200 * 16 * 1.07 * 86_400)
    assert math.isclose(days, expected, rel_tol=1e-6)


def test_health_compact_tokens() -> None:
    assert health_compact(running_health(), stale=False) == "OK"
    assert health_compact(running_health(fatal_code="storage_write"), stale=False) == (
        "F:storage_"
    )
    assert health_compact(running_health(state="stopped"), stale=False) == "STOP"
    assert health_compact(running_health(storage_errors=2), stale=False) == "STOR"
    assert health_compact(running_health(adc_faults=1), stale=False) == "ADC"
    assert health_compact(running_health(missing_sample_groups=5), stale=False) == "G5"
    assert health_compact(None, stale=True) == "STALE"


def test_build_lines_normal_running() -> None:
    from datetime import datetime

    lines = build_lines(
        running_health(),
        rate_hz=200.0,
        stale=False,
        disk_percent=78,
        days_left=124.0,
        temperature=46.7,
        events=2,
        cpu_percent=35,
        gps=None,
        now=datetime(2026, 9, 16, 21, 37),
    )
    assert lines[0] == "REC 200Hz 21:37"
    assert lines[1] == "DISK 78% ~124d"
    assert lines[2] == "OK 2E 47C CPU35"
    assert all(len(line) <= 21 for line in lines)


def test_build_lines_alarm_overrides() -> None:
    from datetime import datetime

    lines = build_lines(
        running_health(state="fatal", fatal_code="storage_write"),
        rate_hz=200.0,
        stale=False,
        disk_percent=91,
        days_left=12.0,
        temperature=81.0,
        events=None,
        cpu_percent=None,
        gps=None,
        now=datetime(2026, 9, 16, 8, 5),
    )
    assert lines[0] == "FAULT 08:05"
    assert lines[1] == "DISK 91% FULL SOON"
    assert lines[2].startswith("F:storage_")
    assert "81C" in lines[2]


def test_build_lines_gps_appends_when_space_allows() -> None:
    from datetime import datetime

    lines = build_lines(
        running_health(),
        rate_hz=200.0,
        stale=False,
        disk_percent=50,
        days_left=200.0,
        temperature=40.0,
        events=0,
        cpu_percent=None,
        gps="GPS 3D",
        now=datetime(2026, 9, 16, 12, 0),
    )
    assert "GPS 3D" in lines[2]


def test_cpu_usage_percent_from_two_samples(monkeypatch) -> None:
    import types

    samples = iter(
        [
            "cpu  100 0 50 850 0 0 0 0 0 0",
            "cpu  150 0 90 1760 0 0 0 0 0 0",  # +90 busy, +910 idle -> 9% busy
            "cpu  350 0 210 1780 0 0 0 0 0 0",  # +320 busy, +20 idle -> 94% busy
        ]
    )
    monkeypatch.setattr(
        "shakepie_oled.display.Path",
        lambda *_a, **_k: types.SimpleNamespace(
            read_text=lambda **_k: next(samples)
        ),
    )
    usage = CpuUsage()
    assert usage.percent() is None  # first read sets the baseline
    assert usage.percent() == 9
    assert usage.percent() == 94
