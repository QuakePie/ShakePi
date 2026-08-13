"""Safe reading, validation, and application of recorder settings from the web interface.

The editable rules mirror the recorder's own C++ validation (``pi/src/config.cpp``) so a
value accepted here is guaranteed to be accepted by the recorder. Application is atomic:
the current file is backed up, the new file is written through a temporary file in the
same directory, and services are restarted over D-Bus with a polkit rule that allows the
``shakepie-web`` account.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

RECORDER_DATA_RATES: tuple[str, ...] = (
    "30000", "15000", "7500", "3750", "2000", "1000", "500", "100",
    "60", "50", "30", "25", "15", "10", "5", "2.5",
)
STATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,31}$")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One editable recorder setting and the bounds the recorder itself enforces."""

    key: str
    label: str
    kind: str  # "enum" | "int" | "text"
    minimum: int = 0
    maximum: int = 0
    choices: tuple[str, ...] = ()
    pattern: str = ""
    help: str = ""

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "adc.data_rate_sps",
        "ADC conversion rate (SPS)",
        "enum",
        choices=RECORDER_DATA_RATES,
        help="Native ADS1256 conversion rate; must exceed six times the group rate.",
    ),
    FieldSpec(
        "acquisition.nominal_group_rate_hz",
        "Sample-group rate (Hz)",
        "int",
        minimum=1,
        maximum=1000000,
        help="Output rate of N/E/Z sample groups.",
    ),
    FieldSpec(
        "acquisition.samples_per_block",
        "Sample groups per block",
        "int",
        minimum=1,
        maximum=256,
    ),
    FieldSpec(
        "acquisition.max_consecutive_adc_errors",
        "Max consecutive ADC errors",
        "int",
        minimum=1,
        maximum=1000000,
        help="Acquisition stops after this many consecutive failed ADC reads.",
    ),
    FieldSpec(
        "storage.rotation_minutes",
        "File rotation (minutes)",
        "int",
        minimum=1,
        maximum=1440,
        help="One recording file per rotation interval.",
    ),
    FieldSpec(
        "storage.station_id",
        "Station ID",
        "text",
        pattern=STATION_ID_PATTERN.pattern,
        help="Letters, digits, '-' and '_' (1-31 characters); used in filenames.",
    ),
)

RESTART_COMMANDS: tuple[str, ...] = (
    "restart seisberry-recorder.service",
    "restart shakepie-dayplot-worker.service",
    "restart shakepie-archive-web.service",
)


def read_station_id(path: Path) -> str | None:
    """Return the recorder's configured ``storage.station_id``; None when unavailable.

    Dayplot filenames carry this station name, so plots follow the value edited on the
    web settings page rather than a name baked into individual recordings.
    """
    try:
        values = parse_recorder_conf(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    station = values.get("storage.station_id", "")
    return station if STATION_ID_PATTERN.match(station) else None


def parse_recorder_conf(text: str) -> dict[str, str]:
    """Parse key=value lines, ignoring comments, without rejecting unknown keys."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            values[key] = value
    return values


def validate_values(current: dict[str, str], updates: dict[str, str]) -> list[str]:
    """Validate proposed values against the recorder's own configuration rules."""
    merged = dict(current)
    merged.update(updates)
    errors: list[str] = []
    for spec in FIELDS:
        value = updates.get(spec.key, current.get(spec.key, ""))
        if not value:
            errors.append(f"{spec.key} is required")
            continue
        if spec.kind == "enum":
            if value not in spec.choices:
                errors.append(f"{spec.key} must be one of: {', '.join(spec.choices)}")
        elif spec.kind == "int":
            try:
                number = int(value)
            except ValueError:
                errors.append(f"{spec.key} must be a whole number")
                continue
            if not spec.minimum <= number <= spec.maximum:
                errors.append(f"{spec.key} must be between {spec.minimum} and {spec.maximum}")
        elif spec.kind == "text" and spec.pattern and not re.match(spec.pattern, value):
            errors.append(f"{spec.key} has an unsupported value")
    try:
        group_rate = int(merged.get("acquisition.nominal_group_rate_hz", "0"))
        data_rate = float(merged.get("adc.data_rate_sps", "0"))
    except ValueError:
        return errors
    if group_rate <= 0 or data_rate <= 0:
        errors.append("the ADC conversion rate and group rate must be positive")
    elif group_rate * 6 >= data_rate:
        errors.append(
            "adc.data_rate_sps must exceed six times the group rate because every "
            f"group needs three conversions plus one discarded settling conversion "
            f"per channel switch ({group_rate} Hz groups need more than "
            f"{group_rate * 6} SPS)"
        )
    return errors


def render_recorder_conf(original_text: str, updates: dict[str, str]) -> str:
    """Return the configuration text with edited keys replaced in place.

    Comments and unrelated lines are preserved byte-for-byte; new keys are appended.
    """
    updated_keys: set[str] = set()
    output_lines: list[str] = []
    for raw_line in original_text.splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        output_lines.append(raw_line)
    for key, value in updates.items():
        if key not in updated_keys:
            output_lines.append(f"{key}={value}")
    return "\n".join(output_lines) + "\n"


def update_live_rates(original_text: str, group_rate_hz: int) -> str:
    """Point the archive web configuration's view rates at the new group rate."""
    updates = {
        "sample_rate_hz": str(group_rate_hz),
        "target_sample_rate_hz": str(group_rate_hz),
    }
    updated_keys: set[str] = set()
    output_lines: list[str] = []
    for raw_line in original_text.splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output_lines.append(f"{key} = {updates[key]}")
                updated_keys.add(key)
                continue
        output_lines.append(raw_line)
    return "\n".join(output_lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    directory = path.parent
    descriptor, temporary_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.")
    try:
        os.chmod(temporary_name, 0o640)
        # newline="" keeps the text byte-exact on every platform, matching the recorder's
        # own plain LF configuration files.
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary_name)
        raise


def backup_file(path: Path) -> Path | None:
    """Copy the current file to a timestamped sibling; None when it does not exist yet."""
    if not path.is_file():
        return None
    backup = path.with_name(
        f"{path.name}.bak.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    )
    backup.write_bytes(path.read_bytes())
    return backup


def restart_services(
    services: tuple[str, ...],
    timeout_seconds: int = 90,
) -> tuple[list[str], list[str]]:
    """Restart systemd services over the D-Bus API; returns (succeeded, failed).

    systemctl talks to PID 1, so no setuid is involved and the web service can keep
    ``NoNewPrivileges=true``. A polkit rule must allow the ``shakepie-web`` account to
    manage exactly the three service units below.
    """
    succeeded: list[str] = []
    failed: list[str] = []
    for service in services:
        command = ["/usr/bin/systemctl", "restart", service]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            failed.append(f"{service}: {error}")
            continue
        if completed.returncode == 0:
            succeeded.append(service)
        else:
            detail = (completed.stderr or completed.stdout or "").strip()
            failed.append(f"{service}: {detail or f'exit {completed.returncode}'}")
    return succeeded, failed
