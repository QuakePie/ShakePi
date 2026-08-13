"""Command-line miniSEED export for finalized recordings.

The SSH console (shakepie-terminal) runs this module on the recorder to convert
``.spb`` recordings before downloading them, so no raw-format logic is duplicated
outside the archive package:

    .venv/bin/python -m shakepie_archive.miniseed_export \
        --file /mnt/shakepie-data/recordings/STATION_20260917T000000Z.spb \
        --out /tmp/shakepie-miniseed [--channel all|north|east|vertical]

``--day YYYY-MM-DD`` converts every finalized recording of one UTC day (matched
from the recorder's own filenames, so a stale catalog does not matter). Each
input is CRC-validated while it is read; unreadable files are reported on stderr
and counted as failures (exit 1), but do not stop the remaining exports.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from shakepie_archive.archive_reader import RecordingError, finalized_name, read_recording
from shakepie_archive.miniseed import SeriesCollector, encode_all

_DAY_PATTERN = re.compile(r"_(\d{4})(\d{2})(\d{2})T\d{6}Z(?:_u\d+)?\.spb$")

_CHANNELS = {
    "all": ("north", "east", "vertical"),
    "north": ("north",),
    "east": ("east",),
    "vertical": ("vertical",),
}


def _day_from_name(name: str) -> str | None:
    match = _DAY_PATTERN.search(name)
    if match is None:
        return None
    year, month, day = match.groups()
    return f"{year}-{month}-{day}"


def export_file(path: Path, out_dir: Path, channel_param: str) -> Path:
    """Convert one finalized recording; returns the written ``.miniseed`` path."""
    collector = SeriesCollector(_CHANNELS[channel_param])
    metadata = read_recording(path, collector.add_block)
    station = metadata.station_id.upper()[:5] or "SHAKE"
    out_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    suffix = "" if channel_param == "all" else f"_{channel_param}"
    output = out_dir / f"{path.stem}{suffix}.miniseed"
    output.write_bytes(encode_all(collector.finish(), station))
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shakepie_archive.miniseed_export",
        description="Convert finalized ShakePie recordings to miniSEED 2.4.",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="a finalized .spb recording (repeatable)",
    )
    parser.add_argument("--day", help="convert every finalized recording of one UTC day")
    parser.add_argument(
        "--recordings-dir",
        default="/mnt/shakepie-data/recordings",
        help="recording directory for --day scanning",
    )
    parser.add_argument("--out", required=True, help="directory for the .miniseed files")
    parser.add_argument(
        "--channel",
        choices=tuple(_CHANNELS),
        default="all",
        help="export one component or all three (default)",
    )
    arguments = parser.parse_args(argv)

    inputs: list[Path] = [Path(item) for item in arguments.file]
    if arguments.day:
        recordings = Path(arguments.recordings_dir)
        matched = sorted(
            entry
            for entry in recordings.iterdir()
            if finalized_name(entry.name) and _day_from_name(entry.name) == arguments.day
        )
        if not matched:
            print(f"no finalized recordings found for {arguments.day}", file=sys.stderr)
            return 1
        inputs.extend(matched)
    if not inputs:
        parser.error("nothing to export: pass --file or --day")

    out_dir = Path(arguments.out)
    failed = 0
    for path in inputs:
        try:
            output = export_file(path, out_dir, arguments.channel)
        except (RecordingError, OSError) as error:
            failed += 1
            print(f"FAILED {path.name}: {error}", file=sys.stderr)
            continue
        print(f"wrote {output}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
