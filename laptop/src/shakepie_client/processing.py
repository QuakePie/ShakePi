"""Bounded background waveform/calibration/STFT pipeline."""

from __future__ import annotations

import queue
import threading
from contextlib import suppress
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from .buffers import RollingWaveformBuffer
from .calibration import calibrate
from .config import ClientConfig
from .protocol import TIMING_UTC_CLOCK_STEP, Block
from .spectrogram import IncrementalSpectrogram, SpectrogramSnapshot

ADC_FULL_SCALE_COUNTS = 8_388_608.0


@dataclass(frozen=True, slots=True)
class ProcessedSnapshot:
    timestamps_ns: npt.NDArray[np.int64]
    waveform: npt.NDArray[np.float64]
    unit: str
    spectrogram: SpectrogramSnapshot


class ProcessingWorker(threading.Thread):
    def __init__(
        self,
        config: ClientConfig,
        input_queue: queue.Queue[Block],
        output_queue: queue.Queue[ProcessedSnapshot],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="shakepie-processing", daemon=False)
        self._config = config
        self._input = input_queue
        self._output = output_queue
        self._stop_event = stop_event
        self._settings_lock = threading.Lock()
        self._units = config.units
        self._visible_seconds = config.visible_seconds
        self._previous_sample_sequence: int | None = None
        capacity = int(np.ceil(config.waveform_capacity_seconds * config.sample_rate_hz))
        self._waveform = RollingWaveformBuffer(capacity)
        self._spectrogram = self._new_spectrogram()

    def _new_spectrogram(self) -> IncrementalSpectrogram:
        return IncrementalSpectrogram(
            self._config.sample_rate_hz,
            self._config.window,
            self._config.fft_size,
            self._config.hop,
            self._config.spectrogram_history_seconds,
        )

    def set_units(self, units: str) -> None:
        if units not in {"counts", "volts", "velocity", "displacement", "acceleration"}:
            raise ValueError("unsupported calibration mode")
        with self._settings_lock:
            if units != self._units:
                self._units = units
                # Spectral magnitudes cannot mix calibration units. Start a
                # fresh bounded history while retaining raw waveform history.
                self._spectrogram = self._new_spectrogram()

    def set_visible_seconds(self, seconds: float) -> None:
        if not 0.5 <= seconds <= self._config.waveform_capacity_seconds:
            raise ValueError("visible time is outside waveform buffer capacity")
        with self._settings_lock:
            self._visible_seconds = seconds

    def run(self) -> None:
        while not self._stop_event.is_set() or not self._input.empty():
            try:
                block = self._input.get(timeout=0.1)
            except queue.Empty:
                continue
            raw = np.column_stack(
                (block.samples["north"], block.samples["east"], block.samples["vertical"])
            ).astype(np.int32, copy=False)
            offsets = np.asarray(block.samples["offset_ns"], dtype=np.int64)
            timestamps = offsets + np.int64(block.header.utc_start_ns)
            monotonic = offsets + np.int64(block.header.monotonic_start_ns)
            expected = (
                None
                if self._previous_sample_sequence is None
                else self._previous_sample_sequence + 1
            )
            discontinuity = (
                expected is not None and block.header.first_sample_sequence != expected
            )
            utc_step = bool(block.header.timing_flags & TIMING_UTC_CLOCK_STEP)
            if discontinuity or utc_step:
                self._waveform.clear()
            self._waveform.append(timestamps, raw, monotonic)
            with self._settings_lock:
                if discontinuity or utc_step:
                    self._spectrogram.clear()
                calibrated = calibrate(
                    raw,
                    self._units,
                    self._config.reference_voltage_volts,
                    self._config.pga_gain,
                    self._config.channels,
                )
                spectral_values = calibrated.values
                if self._units == "counts":
                    # Express raw-count spectra as dBFS so the default
                    # -120..-20 dB color range remains useful across gains.
                    spectral_values = spectral_values / ADC_FULL_SCALE_COUNTS
                self._spectrogram.append(timestamps, spectral_values)
                visible_times, visible_raw = self._waveform.snapshot(self._visible_seconds)
                visible = calibrate(
                    visible_raw,
                    self._units,
                    self._config.reference_voltage_volts,
                    self._config.pga_gain,
                    self._config.channels,
                )
                snapshot = ProcessedSnapshot(
                    timestamps_ns=visible_times,
                    waveform=visible.values,
                    unit=visible.unit,
                    spectrogram=self._spectrogram.snapshot(
                        self._config.frequency_min_hz, self._config.frequency_max_hz
                    ),
                )
            self._previous_sample_sequence = (
                block.header.first_sample_sequence + block.header.sample_count - 1
            )
            try:
                self._output.put_nowait(snapshot)
            except queue.Full:
                with suppress(queue.Empty):
                    self._output.get_nowait()
                self._output.put_nowait(snapshot)
