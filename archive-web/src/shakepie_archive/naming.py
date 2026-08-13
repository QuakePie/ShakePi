"""Canonical file naming for generated daily plots.

Daily plots must be organized and uploaded to a website without renaming, so every
generated image gets one stable, human-readable name per day and channel:

    <YYYYMMDD>-<STATION>-CH<n>.<ext>   e.g. 20260914-Caspian-CH3.png

``CH1``/``CH2``/``CH3`` are the north/east/vertical channels and ``<STATION>`` is the
recorder's configured station ID (``storage.station_id``, editable on the web settings
page). Regeneration overwrites the same name atomically, and the dayplot worker
migrates older fingerprint-named and previous-convention files to these names on startup.
"""

from __future__ import annotations

import re

_SAFE_STATION = re.compile(r"[^A-Za-z0-9_-]")
_CHANNEL_NUMBERS = {"north": 1, "east": 2, "vertical": 3}


def canonical_dayplot_name(
    station_id: str | None, utc_day: str, channel: str, extension: str
) -> str:
    """Return the stable filename for one dayplot image."""
    station = _SAFE_STATION.sub("", station_id or "SHAKEPIE") or "SHAKEPIE"
    compact_day = utc_day.replace("-", "")
    number = _CHANNEL_NUMBERS.get(channel)
    label = (
        f"CH{number}"
        if number is not None
        else (_SAFE_STATION.sub("", channel).upper() or "CH0")
    )
    clean_extension = _SAFE_STATION.sub("", extension.lower()) or "png"
    return f"{compact_day}-{station}-{label}.{clean_extension}"


_FINGERPRINT_NAME = re.compile(r"^[0-9a-f]{16}-(north|east|vertical)\.(png|jpg)$")
_PREVIOUS_CANONICAL_NAME = re.compile(
    r"^[A-Za-z0-9_-]+_[0-9]{8}_(north|east|vertical)_dayplot\.(png|jpg)$"
)


def legacy_fingerprint_name(relative_path: str) -> bool:
    """True when a cached file still uses the old ``<fingerprint>-<channel>.<ext>`` name."""
    leaf = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    return _FINGERPRINT_NAME.match(leaf) is not None


def legacy_dayplot_name(relative_path: str) -> bool:
    """True for any cached file whose name predates the current convention."""
    leaf = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    return (
        _FINGERPRINT_NAME.match(leaf) is not None
        or _PREVIOUS_CANONICAL_NAME.match(leaf) is not None
    )
