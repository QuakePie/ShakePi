from __future__ import annotations

import numpy as np

from shakepie_client.buffers import RollingWaveformBuffer, decimate_minmax


def test_rolling_buffer_wrap_and_time_window() -> None:
    buffer = RollingWaveformBuffer(5)
    buffer.append(np.arange(3, dtype=np.int64), np.arange(9, dtype=np.int32).reshape(3, 3))
    buffer.append(np.arange(3, 8, dtype=np.int64), np.arange(15, 30, dtype=np.int32).reshape(5, 3))
    timestamps, counts = buffer.snapshot()
    np.testing.assert_array_equal(timestamps, [3, 4, 5, 6, 7])
    np.testing.assert_array_equal(counts, np.arange(15, 30, dtype=np.int32).reshape(5, 3))


def test_decimator_preserves_impulse() -> None:
    x = np.arange(10_000, dtype=np.float64)
    y = np.zeros_like(x)
    y[4321] = 100.0
    reduced_x, reduced_y = decimate_minmax(x, y, 500)
    assert len(reduced_x) <= 500
    assert reduced_y.max() == 100.0
    assert 4321.0 in reduced_x


def test_visible_window_uses_monotonic_time_across_utc_step() -> None:
    buffer = RollingWaveformBuffer(5)
    utc = np.array([100, 101, 50, 51, 52], dtype=np.int64)
    monotonic = np.array([0, 1, 2, 3, 4], dtype=np.int64) * 1_000_000_000
    counts = np.arange(15, dtype=np.int32).reshape(5, 3)
    buffer.append(utc, counts, monotonic)
    timestamps, visible = buffer.snapshot(2.0)
    np.testing.assert_array_equal(timestamps, [50, 51, 52])
    np.testing.assert_array_equal(visible, counts[-3:])
