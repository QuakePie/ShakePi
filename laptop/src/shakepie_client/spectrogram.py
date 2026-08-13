"""Bounded incremental spectrogram computation using scipy.signal.ShortTimeFFT."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from scipy.signal import ShortTimeFFT, get_window


@dataclass(frozen=True, slots=True)
class SpectrogramSnapshot:
    frequencies_hz: npt.NDArray[np.float64]
    times_ns: npt.NDArray[np.int64]
    decibels: npt.NDArray[np.float64]  # shape (3, frequencies, times)


class IncrementalSpectrogram:
    def __init__(
        self,
        sample_rate_hz: float,
        window_name: str,
        fft_size: int,
        hop: int,
        history_seconds: float,
    ) -> None:
        if fft_size < 2 or not 0 < hop <= fft_size:
            raise ValueError("invalid FFT size or hop")
        window = get_window(window_name, fft_size, fftbins=True)
        self._transform = ShortTimeFFT(
            window, hop=hop, fs=sample_rate_hz, mfft=fft_size, scale_to="magnitude"
        )
        self._fft_size = fft_size
        self._hop = hop
        self._pending = np.empty((3, 0), dtype=np.float64)
        self._pending_times = np.empty(0, dtype=np.int64)
        self._capacity = max(2, int(np.ceil(history_seconds * sample_rate_hz / hop)))
        self._data = np.empty((3, self._transform.f_pts, self._capacity), dtype=np.float64)
        self._times = np.empty(self._capacity, dtype=np.int64)
        self._start = 0
        self._size = 0

    @property
    def pending_samples(self) -> int:
        return self._pending.shape[1]

    def reset_pending(self) -> None:
        """Prevent an FFT window from spanning a known sample discontinuity."""
        self._pending = np.empty((3, 0), dtype=np.float64)
        self._pending_times = np.empty(0, dtype=np.int64)

    def clear(self) -> None:
        self.reset_pending()
        self._start = 0
        self._size = 0

    def append(self, timestamps_ns: npt.NDArray[np.int64], values: npt.NDArray[np.float64]) -> int:
        if values.shape != (len(timestamps_ns), 3):
            raise ValueError("spectrogram append arrays have incompatible shapes")
        if len(timestamps_ns) == 0:
            return 0
        self._pending = np.concatenate((self._pending, values.T), axis=1)
        self._pending_times = np.concatenate((self._pending_times, timestamps_ns))
        produced = 0
        while self._pending.shape[1] >= self._fft_size:
            frame = self._pending[:, : self._fft_size]
            spectrum = self._transform.stft(
                frame,
                p0=0,
                p1=1,
                k_offset=self._fft_size // 2,
                padding="zeros",
                axis=-1,
            )
            magnitude = np.abs(spectrum[..., 0])
            decibels = 20.0 * np.log10(np.maximum(magnitude, np.finfo(np.float64).tiny))
            self._store(decibels, int(self._pending_times[self._fft_size // 2]))
            self._pending = self._pending[:, self._hop :]
            self._pending_times = self._pending_times[self._hop :]
            produced += 1
        return produced

    def _store(self, decibels: npt.NDArray[np.float64], timestamp_ns: int) -> None:
        if self._size < self._capacity:
            index = (self._start + self._size) % self._capacity
            self._size += 1
        else:
            index = self._start
            self._start = (self._start + 1) % self._capacity
        self._data[:, :, index] = decibels
        self._times[index] = timestamp_ns

    def snapshot(self, minimum_hz: float, maximum_hz: float) -> SpectrogramSnapshot:
        indices = (self._start + np.arange(self._size)) % self._capacity
        frequencies = np.asarray(self._transform.f, dtype=np.float64)
        mask = (frequencies >= minimum_hz) & (frequencies <= maximum_hz)
        return SpectrogramSnapshot(
            frequencies_hz=frequencies[mask].copy(),
            times_ns=self._times[indices].copy(),
            decibels=self._data[:, mask][:, :, indices].copy(),
        )
