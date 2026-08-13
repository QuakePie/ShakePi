"""Short-lived, one-channel-at-a-time ObsPy dayplot generator process."""

from __future__ import annotations

import argparse
import gc
import importlib
import math
import os
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from shakepie_archive.archive_reader import Block, read_recording
from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings, load_settings, prepare_filesystem
from shakepie_archive.naming import canonical_dayplot_name
from shakepie_archive.recorder_settings import read_station_id

CHANNELS = {"north": 0, "east": 1, "vertical": 2}


def _configure_matplotlib(settings: Settings) -> None:
    """Keep Matplotlib state in the worker's explicitly writable data directory."""
    config_directory = settings.dayplot.temporary_directory / "matplotlib"
    config_directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.environ["MPLBACKEND"] = "Agg"
    os.environ["MPLCONFIGDIR"] = str(config_directory)


def _dayplot_envelope(data: Any, mask: Any, requested_bins: int) -> Any:
    """Reduce a full-day mmap to a bounded, gap-aware min/max display envelope."""
    import numpy as np

    sample_count = int(data.shape[0])
    bin_count = max(1, min(requested_bins, sample_count))
    envelope = np.full(bin_count * 2, np.nan, dtype=np.float32)
    envelope_mask = np.ones(bin_count * 2, dtype=np.bool_)
    for index in range(bin_count):
        start = index * sample_count // bin_count
        stop = (index + 1) * sample_count // bin_count
        valid = ~np.asarray(mask[start:stop], dtype=np.bool_)
        if not valid.any():
            continue
        values = np.asarray(data[start:stop], dtype=np.float32)[valid]
        minimum_index = int(np.argmin(values))
        maximum_index = int(np.argmax(values))
        low = float(values[minimum_index])
        high = float(values[maximum_index])
        if minimum_index <= maximum_index:
            envelope[index * 2 : index * 2 + 2] = (low, high)
        else:
            envelope[index * 2 : index * 2 + 2] = (high, low)
        envelope_mask[index * 2 : index * 2 + 2] = False
    return np.ma.MaskedArray(envelope, mask=envelope_mask, copy=False)


def _close_memmap(value: Any) -> None:
    """Close NumPy's mmap handle without exposing its private member to type checking."""
    memory_map: Any = getattr(value, "_mmap", None)
    if memory_map is not None and not memory_map.closed:
        memory_map.close()


def _apply_memory_limit(limit_mib: int) -> None:
    if os.name != "posix":
        return
    resource: Any = importlib.import_module("resource")
    limit = limit_mib * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _day_start_ns(utc_day: str) -> int:
    selected = date.fromisoformat(utc_day)
    return int(datetime.combine(selected, datetime.min.time(), UTC).timestamp() * 1e9)


def _generate_channel(
    settings: Settings,
    catalog: Catalog,
    job: Any,
    records: list[Any],
    channel: str,
    station: str,
) -> str:
    os.environ["MPLBACKEND"] = "Agg"
    import numpy as np
    from obspy import Stream, Trace, UTCDateTime

    channel_index = CHANNELS[channel]
    source_sample_rate = min(int(row["sample_rate_millihz"]) for row in records) / 1000.0
    plot_sample_rate = min(source_sample_rate, float(settings.dayplot.target_sample_rate_hz))
    sample_total = math.ceil(86_400 * plot_sample_rate)
    start_ns = _day_start_ns(str(job["utc_day"]))
    prefix = f"shakepie-dayplot-{job['id']}-{channel}"
    data_path = settings.dayplot.temporary_directory / f"{prefix}.data"
    mask_path = settings.dayplot.temporary_directory / f"{prefix}.mask"
    output_subdirectory = settings.dayplot.output_directory / str(job["utc_day"])
    output_subdirectory.mkdir(mode=0o750, parents=True, exist_ok=True)
    # Canonical, website-ready name; regeneration overwrites the same file atomically.
    output_name = canonical_dayplot_name(
        station, str(job["utc_day"]), channel, settings.dayplot.output_format
    )
    final_output = output_subdirectory / output_name
    temporary_output = (
        settings.dayplot.temporary_directory / f"{prefix}.{settings.dayplot.output_format}"
    )
    data = np.memmap(data_path, dtype=np.float32, mode="w+", shape=(sample_total,))
    mask = np.memmap(mask_path, dtype=np.bool_, mode="w+", shape=(sample_total,))
    mask[:] = True

    sample_dtype = np.dtype(
        [("north", "<i4"), ("east", "<i4"), ("vertical", "<i4"), ("offset", "<u4")]
    )
    field = ("north", "east", "vertical")[channel_index]

    def receive(block: Block) -> None:
        samples = np.frombuffer(block.payload, dtype=sample_dtype, count=block.sample_count)
        absolute = block.utc_start_ns + samples["offset"].astype(np.int64)
        indices = np.rint((absolute - start_ns) * plot_sample_rate / 1_000_000_000).astype(
            np.int64
        )
        valid = (indices >= 0) & (indices < sample_total)
        selected_indices = indices[valid]
        data[selected_indices] = samples[field][valid]
        mask[selected_indices] = False

    try:
        for row in records:
            candidate = settings.archive.data_directory / str(row["relative_path"])
            metadata = read_recording(candidate, receive)
            identity = (metadata.device, metadata.inode, metadata.size_bytes, metadata.mtime_ns)
            expected = (
                int(row["device"]),
                int(row["inode"]),
                int(row["size_bytes"]),
                int(row["mtime_ns"]),
            )
            if identity != expected:
                raise RuntimeError(f"source changed while reading {row['filename']}")
        data.flush()
        mask.flush()
        # ObsPy deep-copies the Stream before drawing. Pass it a screen-resolution
        # min/max envelope instead of a second full-day array so full days stay bounded.
        interval_count = math.ceil(24 * 60 / settings.dayplot.interval_minutes)
        masked = _dayplot_envelope(
            data,
            mask,
            requested_bins=interval_count * settings.dayplot.width_pixels,
        )
        _close_memmap(data)
        _close_memmap(mask)
        with suppress(FileNotFoundError):
            data_path.unlink()
        with suppress(FileNotFoundError):
            mask_path.unlink()
        trace = Trace(data=masked)
        trace.stats.starttime = UTCDateTime(start_ns / 1e9)
        trace.stats.sampling_rate = len(masked) / 86_400
        trace.stats.channel = {"north": "N", "east": "E", "vertical": "Z"}[channel]
        Stream([trace]).plot(
            type="dayplot",
            interval=settings.dayplot.interval_minutes,
            outfile=str(temporary_output),
            size=(settings.dayplot.width_pixels, 1200),
            title=f"ShakePie {job['utc_day']} UTC · {channel.title()}",
        )
        os.replace(temporary_output, final_output)
        return str(final_output.relative_to(settings.dayplot.output_directory))
    finally:
        _close_memmap(data)
        _close_memmap(mask)
        gc.collect()
        for candidate in (data_path, mask_path, temporary_output):
            with suppress(FileNotFoundError):
                candidate.unlink()


def generate_job(settings: Settings, job_id: int) -> None:
    prepare_filesystem(settings)
    _configure_matplotlib(settings)
    _apply_memory_limit(settings.dayplot.memory_limit_mib)
    catalog = Catalog(settings.archive.catalog_path)
    catalog.initialize()
    job = catalog.job(job_id)
    if job is None or job["state"] != "running":
        raise RuntimeError("dayplot job is not in Running state")
    utc_day = str(job["utc_day"])
    selected_day = date.fromisoformat(utc_day)
    if catalog.source_fingerprint(utc_day) != job["source_fingerprint"]:
        raise RuntimeError("source recordings changed before generation started")
    records = catalog.records_for_day(utc_day)
    # The filename station follows the web-editable recorder settings, falling back to
    # the station recorded in the files themselves when the settings cannot be read.
    station = (
        read_station_id(settings.recorder_admin.recorder_conf_path)
        or next((str(row["station_id"]) for row in records if row["station_id"]), "SHAKEPIE")
    )
    paths: dict[str, str] = {}
    for channel in CHANNELS:
        catalog.update_job(job_id, "running", f"Generating {channel.title()}")
        paths[channel] = _generate_channel(settings, catalog, job, records, channel, station)
    if (
        selected_day < datetime.now(UTC).date()
        and catalog.source_fingerprint(utc_day) != job["source_fingerprint"]
    ):
        raise RuntimeError("source recordings changed during generation")
    catalog.complete_dayplot(job_id, paths)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one queued ShakePie dayplot")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job-id", type=int, required=True)
    arguments = parser.parse_args()
    settings = load_settings(arguments.config)
    try:
        generate_job(settings, arguments.job_id)
    except BaseException as error:
        Catalog(settings.archive.catalog_path).update_job(
            arguments.job_id, "failed", "Generation failed", str(error)
        )
        raise


if __name__ == "__main__":
    main()
