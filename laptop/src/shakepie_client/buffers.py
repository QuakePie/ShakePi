"""Fixed-memory rolling buffers and plot-oriented decimation."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt


class RollingWaveformBuffer:
    def __init__(self, capacity: int) -> None:
        if capacity < 2:
            raise ValueError("capacity must be at least two")
        self._capacity = capacity
        self._timestamps = np.empty(capacity, dtype=np.int64)
        self._monotonic = np.empty(capacity, dtype=np.int64)
        self._counts = np.empty((capacity, 3), dtype=np.int32)
        self._start = 0
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def clear(self) -> None:
        self._start = 0
        self._size = 0

    def append(
        self,
        timestamps_ns: npt.NDArray[np.int64],
        counts: npt.NDArray[np.int32],
        monotonic_ns: npt.NDArray[np.int64] | None = None,
    ) -> None:
        if timestamps_ns.ndim != 1 or counts.shape != (len(timestamps_ns), 3):
            raise ValueError("waveform append arrays have incompatible shapes")
        if monotonic_ns is None:
            monotonic_ns = timestamps_ns
        if monotonic_ns.ndim != 1 or len(monotonic_ns) != len(timestamps_ns):
            raise ValueError("waveform monotonic timestamps have an incompatible shape")
        if len(timestamps_ns) == 0:
            return
        if len(timestamps_ns) >= self._capacity:
            self._timestamps[:] = timestamps_ns[-self._capacity :]
            self._monotonic[:] = monotonic_ns[-self._capacity :]
            self._counts[:] = counts[-self._capacity :]
            self._start = 0
            self._size = self._capacity
            return
        write = (self._start + self._size) % self._capacity
        overflow = max(0, self._size + len(timestamps_ns) - self._capacity)
        if overflow:
            self._start = (self._start + overflow) % self._capacity
            self._size -= overflow
            write = (self._start + self._size) % self._capacity
        first = min(len(timestamps_ns), self._capacity - write)
        self._timestamps[write : write + first] = timestamps_ns[:first]
        self._monotonic[write : write + first] = monotonic_ns[:first]
        self._counts[write : write + first] = counts[:first]
        remaining = len(timestamps_ns) - first
        if remaining:
            self._timestamps[:remaining] = timestamps_ns[first:]
            self._monotonic[:remaining] = monotonic_ns[first:]
            self._counts[:remaining] = counts[first:]
        self._size += len(timestamps_ns)

    def snapshot(
        self, visible_seconds: float | None = None
    ) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int32]]:
        if self._size == 0:
            return np.empty(0, dtype=np.int64), np.empty((0, 3), dtype=np.int32)
        indices = (self._start + np.arange(self._size)) % self._capacity
        timestamps = self._timestamps[indices].copy()
        monotonic = self._monotonic[indices]
        counts = self._counts[indices].copy()
        if visible_seconds is not None:
            cutoff = monotonic[-1] - int(visible_seconds * 1_000_000_000)
            first = int(np.searchsorted(monotonic, cutoff, side="left"))
            timestamps = timestamps[first:]
            counts = counts[first:]
        return timestamps, counts


def decimate_minmax(
    x: npt.NDArray[np.float64], y: npt.NDArray[np.float64], maximum_points: int
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Preserve spikes while limiting plotted points to about the pixel width."""
    if len(x) != len(y):
        raise ValueError("x and y lengths differ")
    if maximum_points < 4 or len(x) <= maximum_points:
        return x, y
    buckets = max(1, maximum_points // 2)
    width = int(np.ceil(len(x) / buckets))
    padded = buckets * width
    x_pad = np.pad(x, (0, padded - len(x)), mode="edge").reshape(buckets, width)
    y_pad = np.pad(y, (0, padded - len(y)), mode="edge").reshape(buckets, width)
    minima = np.argmin(y_pad, axis=1)
    maxima = np.argmax(y_pad, axis=1)
    rows = np.arange(buckets)
    order = np.stack((minima, maxima), axis=1)
    order.sort(axis=1)
    return x_pad[rows[:, None], order].ravel(), y_pad[rows[:, None], order].ravel()
