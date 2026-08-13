"""Recorder filename conventions and remote-path constants.

The recorder writes finalized files named ``STATION_YYYYMMDDTHHMMSSZ.spb`` (with
optional ``_u<nanoseconds>`` collision suffixes and ``.recovered.spb`` marks).
Live in-progress files end in ``.partial.spb`` and must never be downloaded or
deleted by tooling.
"""

from __future__ import annotations

from dataclasses import dataclass

RECORDING_DIR = "/mnt/shakepie-data/recordings"
DAYPLOT_DIR = "/mnt/shakepie-data/dayplots"
HEALTH_FILE = "/run/shakepie/health.json"
ARCHIVE_WEB_DIR = "/opt/shakepie/archive-web"
ARCHIVE_WEB_CONF = "/etc/shakepie/archive-web.conf"

_PARTIAL_SUFFIXES = (".partial.spb", ".partial.empty.spb")


def is_finalized_recording_name(name: str) -> bool:
    """True for finalized recordings; never true for the recorder's live partial files."""
    return name.endswith(".spb") and not name.endswith(_PARTIAL_SUFFIXES)


def day_of_file_name(name: str) -> str | None:
    """UTC day from a finalized recorder filename; partial files never map to a day."""
    if name.endswith((".partial.spb", ".partial.empty.spb", ".partial")):
        return None
    stem = name
    for suffix in (".recovered.spb", ".spb"):
        if stem.endswith(suffix):
            stem = stem.removesuffix(suffix)
            break
    parts = stem.split("_")
    stamp = parts[-1] if parts else ""
    if stamp.startswith("u") and stamp[1:].isdigit() and len(parts) >= 2:
        stamp = parts[-2]  # collision-renamed file: ..._YYYYMMDDTHHMMSSZ_u<nanoseconds>
    if (
        len(stamp) != 16
        or stamp[8] != "T"
        or not stamp[:8].isdigit()
        or not stamp[9:15].isdigit()
        or stamp[15] != "Z"
    ):
        return None
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"


@dataclass(slots=True)
class RemoteFile:
    name: str
    size: int

    @property
    def day(self) -> str | None:
        return day_of_file_name(self.name)


def human_bytes(value: int) -> str:
    amount = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or suffix == "TiB":
            return f"{amount:.1f} {suffix}"
        amount /= 1024
    return f"{value} B"
