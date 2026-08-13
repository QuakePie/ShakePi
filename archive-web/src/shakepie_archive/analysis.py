"""Post-hoc polarization analyses for finalized recordings.

Both helpers stream a validated recording once with bounded memory:

* ``hodogram`` — a downsampled North-vs-East particle-motion (hodogram) view of
  a time window, plus the principal polarization azimuth and linearity from a
  2x2 covariance eigen-decomposition. For a P-wave arrival the major axis points
  along the propagation direction, which doubles as a single-station
  back-azimuth hint.
* ``hv_ratio`` — the Nakamura horizontal-to-vertical ambient-noise spectral
  ratio from Welch averages of the three components; the peak of the ratio is
  the site's resonance frequency.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from shakepie_archive.archive_reader import Block, read_recording
from shakepie_archive.downloads import UnsafeFileError, open_catalog_recording

_SAMPLE_DTYPE = np.dtype(
    [("north", "<i4"), ("east", "<i4"), ("vertical", "<i4"), ("offset", "<u4")]
)
_TIMING_UTC_CLOCK_STEP = 1 << 0
MAX_HODOGRAM_SECONDS = 300.0
MAX_HODOGRAM_POINTS = 20_000
HV_LOG_BINS = 48
# Standard site-response band: resonance peaks below 0.2 Hz or above 20 Hz are
# usually sensor artifacts for a short-period geophone, not ground truth.
HV_PEAK_BAND_HZ = (0.2, 20.0)


def _verified_read(root: Path, row: Any, on_block: Any) -> Any:
    """Read one catalog-selected recording and re-verify its identity afterwards."""
    opened = open_catalog_recording(root, row)
    opened.handle.close()
    candidate = root / str(row["relative_path"])
    metadata = read_recording(candidate, on_block)
    if (
        metadata.device,
        metadata.inode,
        metadata.size_bytes,
        metadata.mtime_ns,
    ) != (
        int(row["device"]),
        int(row["inode"]),
        int(row["size_bytes"]),
        int(row["mtime_ns"]),
    ):
        raise UnsafeFileError("recording changed while it was being analyzed")
    return metadata


def hodogram(
    root: Path,
    row: Any,
    *,
    start_seconds: float,
    window_seconds: float,
    max_points: int,
) -> dict[str, Any]:
    """Downsampled N/E particle motion for one window, with polarization metrics."""
    start_ns = int(row["start_ns"])
    end_ns = int(row["end_ns"])
    duration_seconds = max(0.0, (end_ns - start_ns) / 1e9)
    window = max(0.1, min(window_seconds, MAX_HODOGRAM_SECONDS, duration_seconds))
    offset = max(0.0, min(start_seconds, max(0.0, duration_seconds - 0.1)))
    window_start_ns = start_ns + int(offset * 1e9)
    window_end_ns = min(window_start_ns + int(window * 1e9), end_ns)
    points_cap = max(10, min(max_points, MAX_HODOGRAM_POINTS))

    nominal_rate_hz = int(row["sample_rate_millihz"] or 200_000) / 1000.0
    stride = max(1, math.ceil(window * nominal_rate_hz / points_cap))
    picked: list[tuple[int, int, int]] = []
    seen = 0

    def on_block(block: Block) -> None:
        nonlocal seen
        samples = np.frombuffer(block.payload, dtype=_SAMPLE_DTYPE, count=block.sample_count)
        times = block.utc_start_ns + samples["offset"].astype(np.int64)
        mask = (times >= window_start_ns) & (times < window_end_ns)
        if not mask.any():
            return
        north = samples["north"][mask]
        east = samples["east"][mask]
        kept_times = times[mask]
        for index in range(len(north)):
            if seen % stride == 0:
                picked.append((int(north[index]), int(east[index]), int(kept_times[index])))
            seen += 1

    _verified_read(root, row, on_block)

    azimuth_deg = 0.0
    linearity = 0.0
    if len(picked) >= 2:
        values = np.array([(item[0], item[1]) for item in picked], dtype=np.float64)
        centered = values - values.mean(axis=0, keepdims=True)
        covariance = centered.T @ centered / len(centered)
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        major = eigenvectors[:, -1]
        if abs(major[0]) > 0 or abs(major[1]) > 0:
            azimuth_deg = math.degrees(math.atan2(major[1], major[0])) % 180.0
        largest, smallest = float(eigenvalues[-1]), float(eigenvalues[0])
        if largest > 0:
            linearity = 1.0 - smallest / largest

    return {
        "window": {
            "start_ms": window_start_ns / 1e6,
            "end_ms": window_end_ns / 1e6,
            "seconds": round(window, 3),
        },
        "points": [[item[0], item[1], item[2] / 1e6] for item in picked],
        "decimation": stride,
        "azimuth_deg": round(azimuth_deg, 1),
        "linearity": round(linearity, 3),
    }


def hv_ratio(root: Path, row: Any) -> dict[str, Any]:
    """H/V ambient-noise spectral ratio (Nakamura) from overlap-free Welch averages."""
    estimate_hz = int(row["sample_rate_millihz"] or 200_000) / 1000.0
    sample_count = max(2, int(row["sample_count"]))
    # Pick a power-of-two frame targeting roughly four averages on short files,
    # capped at 2048 samples (~10 s at 200 Hz) for honest low-frequency bins.
    exponent = min(11, max(6, int(math.log2(max(64, sample_count // 4)))))
    nperseg = 1 << exponent
    maximum_step_ns = max(1, round(3_000_000_000 / estimate_hz))
    window = np.hanning(nperseg).reshape(-1, 1)
    power = np.zeros((nperseg // 2 + 1, 3), dtype=np.float64)
    frames = 0
    pending = np.empty((0, 3), dtype=np.float64)
    previous_sequence: int | None = None

    def feed(values: npt.NDArray[np.float64]) -> None:
        nonlocal pending, frames, power
        if len(values) == 0:
            return
        pending = np.concatenate((pending, values), axis=0)
        while len(pending) >= nperseg:
            frame = pending[:nperseg]
            centered = frame - frame.mean(axis=0, keepdims=True)
            spectrum = np.fft.rfft(centered * window, axis=0)
            power += np.abs(spectrum) ** 2
            frames += 1
            pending = pending[nperseg:]

    def on_block(block: Block) -> None:
        nonlocal pending, previous_sequence
        samples = np.frombuffer(block.payload, dtype=_SAMPLE_DTYPE, count=block.sample_count)
        times = block.utc_start_ns + samples["offset"].astype(np.int64)
        values = np.column_stack(
            (samples["north"], samples["east"], samples["vertical"])
        ).astype(np.float64, copy=False)
        discontinuity = (
            previous_sequence is not None
            and block.first_sample_sequence != previous_sequence + 1
        ) or bool(block.timing_flags & _TIMING_UTC_CLOCK_STEP)
        previous_sequence = block.first_sample_sequence + block.sample_count - 1
        if discontinuity:
            pending = np.empty((0, 3), dtype=np.float64)
        boundaries = np.flatnonzero(np.diff(times) > maximum_step_ns) + 1
        segment_start = 0
        for boundary in [*boundaries.tolist(), len(times)]:
            if segment_start != 0:
                pending = np.empty((0, 3), dtype=np.float64)
            feed(values[segment_start:boundary])
            segment_start = boundary

    metadata = _verified_read(root, row, on_block)
    measured_hz = (
        metadata.sample_count / max(1e-9, (metadata.end_ns - metadata.start_ns) / 1e9)
        if metadata.sample_count > 1
        else estimate_hz
    )
    if frames == 0 or measured_hz <= 0:
        return {
            "frequencies_hz": [],
            "h_over_v": [],
            "peak_frequency_hz": None,
            "nperseg": nperseg,
            "segments": frames,
            "sample_rate_hz": round(measured_hz, 3),
        }

    frequencies = np.fft.rfftfreq(nperseg, d=1.0 / measured_hz)
    mean_power = power / frames
    horizontal = (mean_power[:, 0] + mean_power[:, 1]) / 2.0
    # Floor the denominator relative to the loudest bin so a silent vertical
    # channel cannot overflow the ratio; anything above ~1e6 is meaningless.
    vertical = np.maximum(mean_power[:, 2], float(mean_power.max()) * 1e-12)
    ratio = np.sqrt(horizontal / vertical)

    # Rebin into logarithmically spaced bands (standard H/V presentation).
    usable = (frequencies > 0) & np.isfinite(ratio)
    bin_freqs: list[float] = []
    bin_ratios: list[float] = []
    if usable.any():
        low = max(0.05, float(frequencies[usable].min()))
        high = float(frequencies[usable].max())
        edges = np.geomspace(low, high, HV_LOG_BINS + 1)
        indices = np.searchsorted(edges, frequencies[usable], side="right") - 1
        indices = np.clip(indices, 0, HV_LOG_BINS - 1)
        horizontal_sum = np.bincount(indices, weights=horizontal[usable], minlength=HV_LOG_BINS)
        vertical_sum = np.bincount(indices, weights=vertical[usable], minlength=HV_LOG_BINS)
        counts = np.bincount(indices, minlength=HV_LOG_BINS)
        for index in range(HV_LOG_BINS):
            if counts[index] == 0:
                continue
            center = math.sqrt(edges[index] * edges[index + 1])
            value = math.sqrt(horizontal_sum[index] / vertical_sum[index])
            bin_freqs.append(round(center, 6))
            bin_ratios.append(round(value, 6))

    band_low, band_high = HV_PEAK_BAND_HZ
    peak: float | None = None
    best = 0.0
    for frequency, value in zip(bin_freqs, bin_ratios, strict=True):
        if band_low <= frequency <= band_high and value > best:
            best = value
            peak = frequency

    return {
        "frequencies_hz": bin_freqs,
        "h_over_v": bin_ratios,
        "peak_frequency_hz": peak,
        "nperseg": nperseg,
        "segments": frames,
        "sample_rate_hz": round(measured_hz, 3),
    }
