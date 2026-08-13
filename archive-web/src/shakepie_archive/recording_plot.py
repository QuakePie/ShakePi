"""Bounded waveform envelopes and spectrograms for one finalized recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from shakepie_archive.archive_reader import Block, RecordingError, read_recording
from shakepie_archive.config import RecordedPlotConfig
from shakepie_archive.downloads import UnsafeFileError, open_catalog_recording

_FULL_SCALE_COUNTS: Final = 8_388_608.0
_TIMING_UTC_CLOCK_STEP: Final = 1 << 0
_SAMPLE_DTYPE: Final[np.dtype[np.void]] = np.dtype(
    [("north", "<i4"), ("east", "<i4"), ("vertical", "<i4"), ("offset", "<u4")]
)


def _optional_values(values: npt.NDArray[np.float64]) -> list[float | None]:
    return [round(float(value), 3) if np.isfinite(value) else None for value in values]


def analyze_recording(root: Path, row: Any, config: RecordedPlotConfig) -> dict[str, Any]:
    """Validate and summarize one catalog-selected file without loading it into browser memory."""
    opened = open_catalog_recording(root, row)
    opened.handle.close()
    candidate = root / str(row["relative_path"])
    start_ns = int(row["start_ns"])
    end_ns = int(row["end_ns"])
    duration_ns = max(1, end_ns - start_ns)
    # The header rate is nominal; older recordings may have been written at the pipeline's
    # free-running rate. Measuring the effective group rate keeps the spectrogram
    # frequency axis truthful for every archived file.
    nominal_rate_hz = int(row["sample_rate_millihz"]) / 1000.0
    probe = read_recording(candidate)
    measured_rate_hz = (
        probe.sample_count / max(1e-9, (probe.end_ns - probe.start_ns) / 1e9)
        if probe.sample_count > 1
        else nominal_rate_hz
    )
    sample_rate = measured_rate_hz if measured_rate_hz > 0 else nominal_rate_hz
    waveform_bins = min(config.waveform_bins, max(2, int(row["sample_count"])))
    spectrogram_columns = config.spectrogram_columns
    minimum = np.full((waveform_bins, 3), np.inf, dtype=np.float64)
    maximum = np.full((waveform_bins, 3), -np.inf, dtype=np.float64)
    waveform_counts = np.zeros(waveform_bins, dtype=np.uint32)

    fft_size = config.fft_size
    hop = fft_size // 2
    window = np.hanning(fft_size).reshape(-1, 1)
    frequency_max_hz = min(float(config.frequency_max_hz), sample_rate / 2.0)
    frequencies = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
    frequency_mask = frequencies <= frequency_max_hz
    frequencies = frequencies[frequency_mask]
    power_sum = np.zeros((spectrogram_columns, len(frequencies), 3), dtype=np.float64)
    spectrum_counts = np.zeros(spectrogram_columns, dtype=np.uint32)
    pending_values = np.empty((0, 3), dtype=np.float64)
    pending_times = np.empty(0, dtype=np.int64)
    previous_sequence: int | None = None
    previous_timestamp: int | None = None
    maximum_step_ns = max(1, round(3_000_000_000 / sample_rate))

    def process_segment(times: npt.NDArray[np.int64], values: npt.NDArray[np.float64]) -> None:
        nonlocal pending_values, pending_times
        if len(times) == 0:
            return
        if previous_timestamp is not None and times[0] - previous_timestamp > maximum_step_ns:
            pending_values = np.empty((0, 3), dtype=np.float64)
            pending_times = np.empty(0, dtype=np.int64)
        pending_values = np.concatenate((pending_values, values), axis=0)
        pending_times = np.concatenate((pending_times, times))
        while len(pending_times) >= fft_size:
            frame = pending_values[:fft_size]
            centered = frame - np.mean(frame, axis=0, keepdims=True)
            transformed = np.fft.rfft(centered * window, axis=0)[frequency_mask]
            magnitude = np.abs(transformed) * (2.0 / np.sum(window)) / _FULL_SCALE_COUNTS
            power = np.square(magnitude)
            frame_time = int(pending_times[fft_size // 2])
            column = min(
                spectrogram_columns - 1,
                max(0, int((frame_time - start_ns) * spectrogram_columns / duration_ns)),
            )
            power_sum[column] += power
            spectrum_counts[column] += 1
            pending_values = pending_values[hop:]
            pending_times = pending_times[hop:]

    def receive(block: Block) -> None:
        nonlocal pending_values, pending_times, previous_sequence, previous_timestamp
        samples = np.frombuffer(block.payload, dtype=_SAMPLE_DTYPE, count=block.sample_count)
        times = block.utc_start_ns + samples["offset"].astype(np.int64)
        values = np.column_stack(
            (samples["north"], samples["east"], samples["vertical"])
        ).astype(np.float64, copy=False)
        bins = np.clip(
            ((times - start_ns) * waveform_bins // duration_ns).astype(np.int64),
            0,
            waveform_bins - 1,
        )
        for channel_index in range(3):
            np.minimum.at(minimum[:, channel_index], bins, values[:, channel_index])
            np.maximum.at(maximum[:, channel_index], bins, values[:, channel_index])
        np.add.at(waveform_counts, bins, 1)

        discontinuity = (
            previous_sequence is not None and block.first_sample_sequence != previous_sequence + 1
        ) or bool(block.timing_flags & _TIMING_UTC_CLOCK_STEP)
        if discontinuity:
            pending_values = np.empty((0, 3), dtype=np.float64)
            pending_times = np.empty(0, dtype=np.int64)
            previous_timestamp = None
        boundaries = np.flatnonzero(np.diff(times) > maximum_step_ns) + 1
        segment_start = 0
        for boundary in [*boundaries.tolist(), len(times)]:
            process_segment(times[segment_start:boundary], values[segment_start:boundary])
            if boundary != len(times):
                pending_values = np.empty((0, 3), dtype=np.float64)
                pending_times = np.empty(0, dtype=np.int64)
                previous_timestamp = None
            segment_start = boundary
        previous_sequence = block.first_sample_sequence + block.sample_count - 1
        previous_timestamp = int(times[-1])

    try:
        metadata = read_recording(candidate, receive)
    except RecordingError:
        raise
    expected = (
        int(row["device"]),
        int(row["inode"]),
        int(row["size_bytes"]),
        int(row["mtime_ns"]),
    )
    actual = (metadata.device, metadata.inode, metadata.size_bytes, metadata.mtime_ns)
    if actual != expected:
        raise UnsafeFileError("recording changed while it was being analyzed")

    minimum[waveform_counts == 0] = np.nan
    maximum[waveform_counts == 0] = np.nan
    decibels = np.full_like(power_sum, np.nan)
    populated = spectrum_counts > 0
    if np.any(populated):
        averaged = power_sum[populated] / spectrum_counts[populated, None, None]
        decibels[populated] = 10.0 * np.log10(np.maximum(averaged, 1e-24))

    channel_names = ("north", "east", "vertical")
    waveform_times = (
        start_ns + (np.arange(waveform_bins, dtype=np.float64) + 0.5) * duration_ns / waveform_bins
    ) / 1e6
    spectrum_times = (
        start_ns
        + (np.arange(spectrogram_columns, dtype=np.float64) + 0.5)
        * duration_ns
        / spectrogram_columns
    ) / 1e6
    waveform: dict[str, Any] = {"times_ms": waveform_times.tolist()}
    spectra: dict[str, Any] = {
        "times_ms": spectrum_times.tolist(),
        "frequencies_hz": frequencies.round(6).tolist(),
    }
    for channel_index, channel in enumerate(channel_names):
        waveform[channel] = {
            "minimum": _optional_values(minimum[:, channel_index]),
            "maximum": _optional_values(maximum[:, channel_index]),
        }
        spectra[channel] = [
            _optional_values(decibels[column, :, channel_index])
            if spectrum_counts[column]
            else None
            for column in range(spectrogram_columns)
        ]

    return {
        "recording": {
            "id": int(row["id"]),
            "filename": str(row["filename"]),
            "utc_day": str(row["utc_day"]),
            "start_ms": start_ns / 1e6,
            "end_ms": end_ns / 1e6,
            "duration_seconds": duration_ns / 1e9,
            "sample_rate_hz": sample_rate,
            "nominal_rate_hz": nominal_rate_hz,
            "size_bytes": int(row["size_bytes"]),
            "sample_count": int(row["sample_count"]),
            "sequence_gaps": int(row["sequence_gap_count"]),
            "validation_state": str(row["validation_status"]),
        },
        "frequency_max_hz": frequency_max_hz,
        "waveform": waveform,
        "spectrogram": spectra,
    }
