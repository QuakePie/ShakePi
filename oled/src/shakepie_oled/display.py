"""ShakePie OLED status display.

A small systemd service that renders the recorder's status on a 128x32 SSD1306 OLED
(I2C, address 0x3C) using the agreed four-line layout:

    REC * 200.0 Hz   21:37
    DISK 78% - ~124d left
    HEALTH OK 46.7C
    EVT 2 today - GAPS 0

Design rules:
- Line 1 is the heartbeat: the dot blinks once per health-file refresh, proving the whole
  pipeline (ADC -> storage -> telemetry) is alive. State flips to STOP / FAULT <code>.
- Disk shows percent used plus the estimated days until full, from the recorder's actual
  daily volume. Above 90% the line becomes "DISK n% FULL SOON".
- The health line rolls up recorder/ADC/storage/timing into one word and names the
  problem when it is not OK; a stale health file says HEALTH STALE.
- An optional GPS summary (gpsd on localhost:2947) appears when a receiver is attached;
  without one the probe is a cheap failed connect and the line is unchanged.
- Anti burn-in: the display dims automatically between night hours (local time).
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import socket
import sqlite3
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HEALTH_STALE_SECONDS = 45
DISK_ALARM_PERCENT = 90
BYTES_PER_GROUP = 16
STORAGE_OVERHEAD = 1.07
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 7
NIGHT_CONTRAST = 20
DAY_CONTRAST = 255


# ---------------------------------------------------------------------------
# data gathering (pure-ish helpers; unit tested)


def read_health(path: Path) -> dict[str, Any] | None:
    """Read the recorder health JSON; None when missing or unparsable."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def is_stale(health: dict[str, Any] | None) -> bool:
    if health is None:
        return True
    updated = health.get("updated_realtime_ns")
    if not isinstance(updated, int):
        return True
    now_ns = time.time_ns()
    return now_ns - updated > HEALTH_STALE_SECONDS * 1_000_000_000


def soc_temperature() -> float | None:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="ascii")
        return int(raw.strip()) / 1000.0
    except (OSError, ValueError):
        return None


def read_cpu_times() -> tuple[int, int] | None:
    """(idle+io wait, total) jiffies from /proc/stat; None when unavailable."""
    try:
        fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]
        values = [int(value) for value in fields]
    except (OSError, ValueError, IndexError):
        return None
    if len(values) < 4:
        return None
    return (values[3] + (values[4] if len(values) > 4 else 0), sum(values))


class CpuUsage:
    """CPU utilization percent measured across successive one-second samples."""

    def __init__(self) -> None:
        self._last: tuple[int, int] | None = None

    def percent(self) -> int | None:
        current = read_cpu_times()
        if current is None:
            return None
        previous = self._last
        self._last = current
        if previous is None:
            return None  # first sample only sets the baseline
        d_idle = current[0] - previous[0]
        d_total = current[1] - previous[1]
        if d_total <= 0:
            return None
        return round(100 * (d_total - d_idle) / d_total)


def events_today(database: Path, now: datetime) -> int | None:
    """Count events recorded since the start of the UTC day (read-only, best effort)."""
    day_start_ns = int(
        (now - timedelta(hours=now.hour, minutes=now.minute, seconds=now.second,
                         microseconds=now.microsecond)).timestamp() * 1e9
    )
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM events WHERE started_ns >= ?", (day_start_ns,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return int(row[0]) if row is not None else None


def disk_percent_free_and_days(
    data_directory: Path, group_rate_hz: int
) -> tuple[int, int, float]:
    """Return (percent used, free bytes, estimated days until full); -1s when unavailable."""
    try:
        usage = shutil.disk_usage(data_directory)
    except OSError:
        return (-1, -1, -1.0)
    daily_bytes = max(1.0, group_rate_hz * BYTES_PER_GROUP * STORAGE_OVERHEAD * 86_400)
    return (round(usage.used * 100 / usage.total), int(usage.free), usage.free / daily_bytes)


def read_configured_rate(conf_path: Path) -> int | None:
    """The group rate the recorder is actually configured for (health.json omits it)."""
    try:
        for raw_line in conf_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line.startswith("acquisition.nominal_group_rate_hz="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return None


def format_days(days: float) -> str:
    if days >= 365 * 3:
        return "3y+"
    return f"~{int(days)}d"


def gps_summary(state: dict[str, Any]) -> str | None:
    """Probe gpsd (localhost:2947) at most every 5 s; a short fix summary or None.

    Reserved for the planned GNSS receiver: with gpsd running and a fix available this
    returns a marker such as "GPS 3D"; without gpsd the probe is a cheap failed connect.
    """
    now = time.monotonic()
    if now - state.get("checked_at", -1e12) < 5.0:
        return state.get("last")
    state["checked_at"] = now
    state["last"] = None
    try:
        with socket.create_connection(("127.0.0.1", 2947), timeout=0.4) as sock:
            sock.settimeout(0.8)
            sock.sendall(b'?WATCH={"enable":true}\n')
            payload = sock.recv(4096).decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in payload.splitlines():
        if not line.startswith('{"class":"TPV"'):
            continue
        try:
            report = json.loads(line)
        except ValueError:
            continue
        if report.get("mode", 0) >= 2:
            marker = "GPS 3D" if report.get("mode") == 3 else "GPS 2D"
            state["last"] = marker
            break
    summary: str | None = state["last"]
    return summary


def health_compact(health: dict[str, Any] | None, stale: bool) -> str:
    """Short health token for the small panel: OK / ADC / STOR / G12 / STALE / F:code."""
    if stale or health is None:
        return "STALE"
    fatal = str(health.get("fatal_code", "none"))
    if fatal != "none":
        return f"F:{fatal[:8]}"
    if health.get("state") != "running":
        return "STOP"
    storage_errors = int(health.get("storage_errors", 0) or 0)
    if storage_errors:
        return "STOR"
    if int(health.get("adc_faults", 0) or 0) + int(health.get("adc_timeouts", 0) or 0):
        return "ADC"
    gaps = int(health.get("missing_sample_groups", 0) or 0) + int(
        health.get("simulated_sequence_gaps", 0) or 0
    )
    if gaps:
        return f"G{gaps}"
    return "OK"


def build_lines(
    health: dict[str, Any] | None,
    *,
    rate_hz: float,
    stale: bool,
    disk_percent: int,
    days_left: float,
    temperature: float | None,
    events: int | None,
    cpu_percent: int | None,
    gps: str | None,
    now: datetime,
) -> list[str]:
    """Compose the three display lines for the current instant.

    Three bold lines read far better on the small panel than four thin ones; the health
    and event details share the last line in compact form (E = events today, G = gaps).
    The blinking heartbeat is drawn separately at a fixed position so no text moves.
    """
    state = str(health.get("state", "stopped")) if health else "stopped"
    clock = now.strftime("%H:%M")
    if state == "running":
        line1 = f"REC {rate_hz:.0f}Hz {clock}"
    elif state == "fatal":
        line1 = f"FAULT {clock}"
    else:
        line1 = f"STOP {clock}"

    if disk_percent < 0:
        line2 = "DISK --"  # data directory unavailable (simulate mode, unmounted disk)
    elif disk_percent >= DISK_ALARM_PERCENT:
        line2 = f"DISK {disk_percent}% FULL SOON"
    else:
        line2 = f"DISK {disk_percent}% {format_days(days_left)}"

    events_text = "-" if events is None else str(events)
    line3 = f"{health_compact(health, stale)} {events_text}E"
    if temperature is not None:
        line3 += f" {temperature:.0f}C"
    if cpu_percent is not None:
        # Extreme values are rare; when the line would overflow the panel the CPU
        # figure is the part that yields.
        if len(line3) + 6 <= 21:
            line3 += f" CPU{cpu_percent}"
        else:
            line3 += f" {cpu_percent}%"
    if gps and len(line3) + 1 + len(gps) <= 21:
        line3 += f" {gps}"
    return [line1, line2, line3]


# ---------------------------------------------------------------------------
# rendering + main loop


_FONT: Any = None
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
HEARTBEAT_BOX = (120, 1, 126, 7)  # fixed position: right edge of the first line


def _font(size: int) -> Any:
    """A bold TTF font: thin bitmap fonts are unreadable on the small panel."""
    global _FONT
    if _FONT is None:
        from PIL import ImageFont

        for candidate in FONT_CANDIDATES:
            if Path(candidate).is_file():
                _FONT = ImageFont.truetype(candidate, size)
                break
        else:
            _FONT = ImageFont.load_default(size=size)
    return _FONT


HEARTBEAT_BOX = (120, 1, 126, 7)  # fixed position: right edge of the first line


def render(device: Any, lines: list[str], size: int, blink_on: bool) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("1", (device.width, device.height))
    draw = ImageDraw.Draw(image)
    font = _font(size)
    pitch = size + 1
    for index, line in enumerate(lines):
        draw.text((0, index * pitch), line, fill=255, font=font)
    if blink_on:
        # The heartbeat lives at a fixed position; toggling it never moves any text.
        draw.ellipse(HEARTBEAT_BOX, fill=255)
    device.display(image)


def open_device(address: int) -> Any:
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306

    return ssd1306(i2c(port=1, address=address))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shakepie-oled",
        description="ShakePie OLED status display (SSD1306 128x32, I2C 0x3C).",
    )
    parser.add_argument("--health-file", type=Path, default=Path("/run/shakepie/health.json"))
    parser.add_argument(
        "--events-db", type=Path, default=Path("/mnt/shakepie-data/index/archive.sqlite")
    )
    parser.add_argument("--data-dir", type=Path, default=Path("/mnt/shakepie-data"))
    parser.add_argument(
        "--recorder-conf", type=Path, default=Path("/etc/shakepie/recorder.conf")
    )
    parser.add_argument("--i2c-address", type=lambda v: int(v, 0), default=0x3C)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--font-size", type=int, default=10, help="font size in px (3 lines)")
    parser.add_argument("--png-out", type=Path, help="render one frame to a PNG and exit")
    parser.add_argument(
        "--simulate", action="store_true", help="print the display lines to stdout instead"
    )
    parser.add_argument("--frames", type=int, default=0, help="exit after N frames (simulate)")
    arguments = parser.parse_args(argv)

    stopping = False

    def handle_signal(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    device: Any = None
    gps_state: dict[str, Any] = {}
    cpu_usage = CpuUsage()
    frames = 0
    last_contrast = -1
    configured_rate: int | None = None
    rate_checked_at = -1e12
    while not stopping:
        health = read_health(arguments.health_file)
        stale = is_stale(health)
        # health.json does not carry the group rate; read it from the recorder config
        # (refreshed occasionally so a Settings change is picked up without a restart).
        if time.monotonic() - rate_checked_at >= 60.0:
            configured_rate = read_configured_rate(arguments.recorder_conf)
            rate_checked_at = time.monotonic()
        reported = health.get("nominal_group_rate_hz") if health else None
        rate_hz = int(reported) if isinstance(reported, int) and reported > 0 else (
            configured_rate if configured_rate else 200
        )
        percent, _free, days = disk_percent_free_and_days(arguments.data_dir, rate_hz)
        blink_on = int(time.time()) % 2 == 0
        lines = build_lines(
            health,
            rate_hz=float(rate_hz),
            stale=stale,
            disk_percent=percent,
            days_left=days,
            temperature=soc_temperature(),
            cpu_percent=cpu_usage.percent(),
            events=events_today(arguments.events_db, datetime.now(UTC)),
            gps=gps_summary(gps_state),
            now=datetime.now(),
        )
        if arguments.png_out:
            from PIL import Image, ImageDraw

            image = Image.new("1", (128, 32))
            draw = ImageDraw.Draw(image)
            font = _font(arguments.font_size)
            pitch = arguments.font_size + 1
            for index, line in enumerate(lines):
                draw.text((0, index * pitch), line, fill=255, font=font)
            if blink_on:
                draw.ellipse(HEARTBEAT_BOX, fill=255)
            image.save(arguments.png_out)
            print(f"frame written to {arguments.png_out}")
            return 0
        if arguments.simulate:
            for line in lines:
                print(line)
            print("-" * 21)
        else:
            if device is None:
                try:
                    device = open_device(arguments.i2c_address)
                except Exception as error:
                    print(
                        f"waiting for the OLED on I2C (0x{arguments.i2c_address:x}): {error}",
                        flush=True,
                    )
                    time.sleep(30.0)
                    continue
            hour = datetime.now().hour
            wanted = (
                NIGHT_CONTRAST
                if hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR
                else DAY_CONTRAST
            )
            if wanted != last_contrast:
                device.contrast(wanted)
                last_contrast = wanted
            render(device, lines, arguments.font_size, blink_on)
        frames += 1
        if arguments.frames and frames >= arguments.frames:
            break
        slept = 0.0
        while slept < arguments.interval and not stopping:
            time.sleep(0.1)
            slept += 0.1

    if device is not None:
        with suppress(Exception):
            device.hide()
    return 0


if __name__ == "__main__":
    sys.exit(main())
