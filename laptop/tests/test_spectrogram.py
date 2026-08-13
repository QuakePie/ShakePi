from __future__ import annotations

import numpy as np

from shakepie_client.spectrogram import IncrementalSpectrogram


def test_incremental_spectrogram_has_expected_peak_and_bound() -> None:
    sample_rate = 256.0
    transform = IncrementalSpectrogram(sample_rate, "hann", 128, 32, 1.0)
    sample_count = 1024
    times = (
        np.arange(sample_count, dtype=np.int64) * int(1e9 / sample_rate)
    ) + 1_700_000_000_000_000_000
    sine = np.sin(2 * np.pi * 32.0 * np.arange(sample_count) / sample_rate)
    values = np.column_stack((sine, sine * 0.5, sine * 2.0))
    produced = 0
    for start in range(0, sample_count, 17):
        produced += transform.append(times[start : start + 17], values[start : start + 17])
        assert transform.pending_samples < 128
    snapshot = transform.snapshot(0.0, 100.0)
    assert produced > 1
    assert snapshot.decibels.shape[0] == 3
    assert snapshot.decibels.shape[2] <= 8  # one second at a 32-sample hop
    peak = snapshot.frequencies_hz[np.argmax(snapshot.decibels[0, :, -1])]
    assert abs(peak - 32.0) <= 2.0


def test_pending_window_can_be_reset_at_a_sample_gap() -> None:
    transform = IncrementalSpectrogram(256.0, "hann", 64, 32, 1.0)
    times = np.arange(80, dtype=np.int64) * 1_000_000
    values = np.zeros((80, 3), dtype=np.float64)
    assert transform.append(times, values) == 1
    assert transform.pending_samples == 48
    transform.reset_pending()
    assert transform.pending_samples == 0
    assert transform.append(times[:32], values[:32]) == 0
