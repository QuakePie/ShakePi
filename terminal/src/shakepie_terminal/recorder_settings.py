"""Read, validate, and apply recorder.conf from the terminal, mirroring the C++ rules.

The validation is the same contract the recorder enforces at startup
(``pi/src/config.cpp``): a proposed value that fails here would be rejected by the
recorder too, so settings are checked before anything touches the device. The file
is backed up on the Pi before every apply, and the recorder restarts only after a
successful write.
"""

from __future__ import annotations

import re
import time

from shakepie_terminal.connection import ShakePieSession

RECORDER_CONF = "/etc/shakepie/recorder.conf"
RECORDER_DATA_RATES: tuple[str, ...] = (
    "30000", "15000", "7500", "3750", "2000", "1000",
    "500", "100", "60", "50", "30", "25", "15", "10", "5", "2.5",
)
STATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,31}$")

EDITABLE_KEYS: tuple[str, ...] = (
    "adc.data_rate_sps",
    "acquisition.nominal_group_rate_hz",
    "acquisition.samples_per_block",
    "acquisition.max_consecutive_adc_errors",
    "storage.rotation_minutes",
    "storage.station_id",
)


class SettingsError(RuntimeError):
    """Raised when the remote configuration cannot be read, validated, or applied."""


def parse_recorder_conf(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and value.strip():
            values[key.strip()] = value.strip()
    return values


def validate_values(current: dict[str, str], updates: dict[str, str]) -> list[str]:
    merged = {**current, **updates}
    errors: list[str] = []
    if merged.get("adc.data_rate_sps") not in RECORDER_DATA_RATES:
        errors.append("adc.data_rate_sps must be one of: " + ", ".join(RECORDER_DATA_RATES))
    for key, minimum, maximum in (
        ("acquisition.nominal_group_rate_hz", 1, 1_000_000),
        ("acquisition.samples_per_block", 1, 256),
        ("acquisition.max_consecutive_adc_errors", 1, 1_000_000),
        ("storage.rotation_minutes", 1, 1440),
    ):
        try:
            number = int(merged.get(key, ""))
        except ValueError:
            errors.append(f"{key} must be a whole number")
            continue
        if not minimum <= number <= maximum:
            errors.append(f"{key} must be between {minimum} and {maximum}")
    if not STATION_ID_PATTERN.match(merged.get("storage.station_id", "")):
        errors.append("storage.station_id may contain only letters, digits, '-' and '_' (1-31)")
    try:
        group_rate = int(merged.get("acquisition.nominal_group_rate_hz", "0"))
        data_rate = float(merged.get("adc.data_rate_sps", "0"))
    except ValueError:
        return errors
    if group_rate <= 0 or data_rate <= 0:
        errors.append("the ADC conversion rate and group rate must be positive")
    elif group_rate * 6 >= data_rate:
        errors.append(
            f"adc.data_rate_sps must exceed six times the group rate "
            f"({group_rate} Hz groups need more than {group_rate * 6} SPS)"
        )
    return errors


def render_recorder_conf(original_text: str, updates: dict[str, str]) -> str:
    """Replace edited keys in place; comments and unrelated lines are preserved."""
    replaced: set[str] = set()
    output: list[str] = []
    for raw_line in original_text.splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                replaced.add(key)
                continue
        output.append(raw_line)
    for key, value in updates.items():
        if key not in replaced:
            output.append(f"{key}={value}")
    return "\n".join(output) + "\n"


def fetch(session: ShakePieSession) -> tuple[str, dict[str, str]]:
    result = session.run(f"cat {RECORDER_CONF}")
    if not result.ok():
        raise SettingsError(f"cannot read {RECORDER_CONF}: {result.stderr.strip()}")
    return result.stdout, parse_recorder_conf(result.stdout)


CALIBRATE_REQUEST = "/var/lib/shakepie/calibrate.request"
ZERO_OFFSETS_STATE = "/var/lib/shakepie/zero_offsets.conf"


def request_zero_calibration(session: ShakePieSession, mode: str) -> None:
    """Write a calibration request consumed by the recorder within a second.

    ``mode`` is ``calibrate`` (measure and recenter) or ``clear`` (record raw again).
    The recorder removes the file when it picks the request up.
    """
    if mode not in {"calibrate", "clear"}:
        raise SettingsError(f"unknown calibration mode: {mode}")
    # mode is one of the two validated literals, so the shell form is safe.
    result = session.run(f"printf '%s\\n' {mode} > {CALIBRATE_REQUEST}")
    if not result.ok():
        result = session.run_sudo(
            f"tee {CALIBRATE_REQUEST} > /dev/null", stdin_text=mode + "\n"
        )
    if not result.ok():
        raise SettingsError(f"cannot write the calibration request: {result.stderr.strip()}")


def read_zero_offsets(session: ShakePieSession) -> dict[str, int]:
    """Return the persisted offsets, or an empty dict when none were calibrated yet."""
    result = session.run(f"cat {ZERO_OFFSETS_STATE} 2>/dev/null")
    if not result.ok():
        return {}
    offsets: dict[str, int] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"north", "east", "vertical"}:
            try:
                offsets[key.strip()] = int(value.strip())
            except ValueError:
                continue
    return offsets


def apply(session: ShakePieSession, updates: dict[str, str]) -> str:
    """Validate, back up, write, and restart the recorder. Returns a summary line."""
    if not updates:
        raise SettingsError("no changes were proposed")
    original_text, current = fetch(session)
    errors = validate_values(current, updates)
    if errors:
        raise SettingsError("validation failed: " + "; ".join(errors))
    new_text = render_recorder_conf(original_text, updates)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    backup = session.run_sudo(f"cp {RECORDER_CONF} {RECORDER_CONF}.bak.{stamp}")
    if not backup.ok():
        raise SettingsError(f"backup failed: {backup.stderr.strip()}")
    write = session.run_sudo(f"tee {RECORDER_CONF} > /dev/null", stdin_text=new_text)
    if not write.ok():
        raise SettingsError(f"write failed: {write.stderr.strip()}")
    restart = session.run_sudo("systemctl restart seisberry-recorder.service")
    if not restart.ok():
        raise SettingsError(
            f"the configuration was written but the restart failed: {restart.stderr.strip()}"
        )
    return (
        f"updated {', '.join(sorted(updates))}; backup {RECORDER_CONF}.bak.{stamp}; "
        "recorder restarted"
    )
