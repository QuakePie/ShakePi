from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from pytest import MonkeyPatch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from shakepie_client.app import MainWindow
from shakepie_client.config import ClientConfig
from shakepie_client.processing import ProcessedSnapshot
from shakepie_client.spectrogram import SpectrogramSnapshot


def test_window_starts_and_stops_workers_cleanly(tmp_path: Path) -> None:
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = ClientConfig(
        port=1,
        socket_timeout_seconds=0.05,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.02,
        fft_size=64,
        overlap=32,
    )
    window = MainWindow(config, tmp_path / "client.json", None)
    application.processEvents()
    window.close()
    application.processEvents()
    assert not window._receiver.is_alive()
    assert not window._processor.is_alive()
    assert (tmp_path / "client.json").exists()


def test_spectrogram_epoch_bounds_are_reported_to_viewbox(tmp_path: Path) -> None:
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = ClientConfig(
        port=1,
        socket_timeout_seconds=0.05,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.02,
        fft_size=64,
        overlap=32,
    )
    window = MainWindow(config, tmp_path / "client.json", None)
    epoch_ns = 1_700_000_000_000_000_000
    times = np.asarray([epoch_ns, epoch_ns + 100_000_000], dtype=np.int64)
    frequencies = np.asarray([0.0, 50.0, 100.0], dtype=np.float64)
    snapshot = ProcessedSnapshot(
        timestamps_ns=times,
        waveform=np.zeros((2, 3), dtype=np.float64),
        unit="counts",
        spectrogram=SpectrogramSnapshot(
            frequencies_hz=frequencies,
            times_ns=times,
            decibels=np.full((3, 3, 2), -60.0, dtype=np.float64),
        ),
    )
    window._processed.put_nowait(snapshot)
    window._refresh()
    application.processEvents()
    x_minimum, x_maximum = window._spectrogram_plots[0].viewRange()[0]
    epoch_seconds = epoch_ns / 1_000_000_000.0
    assert x_minimum < epoch_seconds < x_maximum
    window.close()
    application.processEvents()
    assert not window._receiver.is_alive()
    assert not window._processor.is_alive()


def test_spectrogram_refresh_does_not_scan_viewbox_children(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = ClientConfig(
        port=1,
        socket_timeout_seconds=0.05,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.02,
        fft_size=64,
        overlap=32,
    )
    window = MainWindow(config, tmp_path / "client.json", None)
    epoch_ns = 1_700_000_000_000_000_000
    times = np.asarray([epoch_ns, epoch_ns + 100_000_000], dtype=np.int64)
    frequencies = np.asarray([0.0, 50.0, 100.0], dtype=np.float64)
    snapshot = ProcessedSnapshot(
        timestamps_ns=times,
        waveform=np.zeros((2, 3), dtype=np.float64),
        unit="counts",
        spectrogram=SpectrogramSnapshot(
            frequencies_hz=frequencies,
            times_ns=times,
            decibels=np.full((3, 3, 2), -60.0, dtype=np.float64),
        ),
    )

    def fail_update_auto_range() -> None:
        raise AssertionError("live refresh must not call ViewBox.updateAutoRange")

    for plot in window._spectrogram_plots:
        monkeypatch.setattr(plot.vb, "updateAutoRange", fail_update_auto_range)
    window._processed.put_nowait(snapshot)
    window._refresh()
    application.processEvents()
    assert window._images[0].image is not None
    window.close()
    application.processEvents()
