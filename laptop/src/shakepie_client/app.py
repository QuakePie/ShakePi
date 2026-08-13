"""PySide6/PyQtGraph native ShakePie monitor."""

from __future__ import annotations

import argparse
import queue
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .buffers import decimate_minmax
from .config import ClientConfig, load_config, save_config
from .processing import ProcessedSnapshot, ProcessingWorker
from .protocol import Block
from .receiver import Receiver

STATUS_FLAG_NAMES = (
    "ADC timeout",
    "ADC fault",
    "simulated",
    "clipping",
    "storage pressure",
)
TIMING_FLAG_NAMES = ("UTC step", "acquisition late", "timestamp saturated")


def describe_flags(value: int, names: tuple[str, ...]) -> str:
    active = [name for bit, name in enumerate(names) if value & (1 << bit)]
    known_mask = (1 << len(names)) - 1
    if value & ~known_mask:
        active.append(f"unknown=0x{value & ~known_mask:x}")
    return ", ".join(active) if active else "ok"


class TimeAxis(pg.AxisItem):  # type: ignore[misc]
    def __init__(self, *, local_time: bool, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.local_time = local_time

    def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:
        del scale
        zone = None if self.local_time else UTC
        if spacing < 1:
            return [
                datetime.fromtimestamp(value, tz=zone)
                .strftime("%H:%M:%S.%f")
                .rstrip("0.")
                for value in values
            ]
        return [datetime.fromtimestamp(value, tz=zone).strftime("%H:%M:%S") for value in values]


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
        self, config: ClientConfig, config_path: Path | None, capture: Path | None
    ) -> None:
        super().__init__()
        self.setWindowTitle("ShakePie live monitor — live view is not the authoritative archive")
        self.resize(1500, 950)
        self._config = config
        self._config_path = config_path
        self._paused = False
        self._stop = threading.Event()
        self._blocks: queue.Queue[Block] = queue.Queue(maxsize=config.queue_blocks)
        self._processed: queue.Queue[ProcessedSnapshot] = queue.Queue(maxsize=2)
        self._receiver = Receiver(
            config.host,
            config.port,
            self._blocks,
            self._stop,
            timeout_seconds=config.socket_timeout_seconds,
            reconnect_initial_seconds=config.reconnect_initial_seconds,
            reconnect_max_seconds=config.reconnect_max_seconds,
            capture_path=capture,
        )
        self._processor = ProcessingWorker(config, self._blocks, self._processed, self._stop)
        self._axes: list[TimeAxis] = []
        self._wave_plots: list[pg.PlotItem] = []
        self._wave_curves: list[pg.PlotDataItem] = []
        self._spectrogram_plots: list[pg.PlotItem] = []
        self._images: list[pg.ImageItem] = []
        self._build_ui()
        self._receiver.start()
        self._processor.start()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(40)  # 25 frames/s; reception and FFT continue while paused.

    def _build_ui(self) -> None:
        toolbar = self.addToolBar("Display")
        toolbar.setMovable(False)
        self._pause_action = QtGui.QAction("Pause display", self, checkable=True)
        self._pause_action.toggled.connect(self._set_paused)
        toolbar.addAction(self._pause_action)
        toolbar.addSeparator()
        toolbar.addWidget(QtWidgets.QLabel("Visible seconds: "))
        visible = QtWidgets.QDoubleSpinBox()
        visible.setRange(0.5, self._config.waveform_capacity_seconds)
        visible.setValue(self._config.visible_seconds)
        visible.setDecimals(1)
        visible.valueChanged.connect(self._set_visible_seconds)
        toolbar.addWidget(visible)
        toolbar.addSeparator()
        toolbar.addWidget(QtWidgets.QLabel("Units: "))
        units = QtWidgets.QComboBox()
        units.addItems(["counts", "volts", "velocity", "displacement", "acceleration"])
        units.setCurrentText(self._config.units)
        units.currentTextChanged.connect(self._set_units)
        toolbar.addWidget(units)
        toolbar.addSeparator()
        self._time_zone = QtWidgets.QComboBox()
        self._time_zone.addItems(["UTC", "Local time"])
        self._time_zone.setCurrentIndex(1 if self._config.local_time else 0)
        self._time_zone.currentIndexChanged.connect(self._set_time_zone)
        toolbar.addWidget(self._time_zone)
        toolbar.addSeparator()
        warning = QtWidgets.QLabel("LIVE VIEW ONLY — HDD files on the Pi are authoritative")
        warning.setStyleSheet("color: #ffb347; font-weight: bold; padding-left: 12px")
        toolbar.addWidget(warning)

        pg.setConfigOptions(antialias=False, imageAxisOrder="row-major")
        graphics = pg.GraphicsLayoutWidget()
        graphics.setBackground("#11151c")
        self.setCentralWidget(graphics)
        colors = ["#5ec8e5", "#f7b955", "#df6c75"]
        for row, (channel, color) in enumerate(zip(self._config.channels, colors, strict=True)):
            axis = TimeAxis(local_time=self._config.local_time, orientation="bottom")
            waveform = graphics.addPlot(row=row, col=0, axisItems={"bottom": axis})
            waveform.setTitle(f"{channel.label} waveform")
            waveform.showGrid(x=True, y=True, alpha=0.2)
            waveform.setLabel("left", self._config.units)
            waveform.setMouseEnabled(x=True, y=True)
            curve = waveform.plot(pen=pg.mkPen(color, width=1))
            self._axes.append(axis)
            self._wave_plots.append(waveform)
            self._wave_curves.append(curve)

            spectrogram_axis = TimeAxis(
                local_time=self._config.local_time, orientation="bottom"
            )
            spectrogram = graphics.addPlot(
                row=row, col=1, axisItems={"bottom": spectrogram_axis}
            )
            spectrogram.setTitle(f"{channel.label} spectrogram")
            spectrogram.setLabel("left", "Frequency", units="Hz")
            spectrogram.setLabel(
                "bottom", "Local time" if self._config.local_time else "UTC"
            )
            image = pg.ImageItem()
            image.setLookupTable(pg.colormap.get(self._config.color_map).getLookupTable())
            spectrogram.addItem(image)
            self._spectrogram_plots.append(spectrogram)
            self._images.append(image)
            self._axes.append(spectrogram_axis)
        for plot in self._wave_plots[1:]:
            plot.setXLink(self._wave_plots[0])
        for plot in self._spectrogram_plots[1:]:
            plot.setXLink(self._spectrogram_plots[0])
        self.statusBar().showMessage("Starting receiver…")

    @QtCore.Slot(bool)
    def _set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._pause_action.setText("Resume display" if paused else "Pause display")

    @QtCore.Slot(float)
    def _set_visible_seconds(self, value: float) -> None:
        self._config.visible_seconds = value
        self._processor.set_visible_seconds(value)

    @QtCore.Slot(str)
    def _set_units(self, value: str) -> None:
        self._config.units = value
        self._processor.set_units(value)
        for plot in self._wave_plots:
            plot.setLabel("left", value)

    @QtCore.Slot(int)
    def _set_time_zone(self, index: int) -> None:
        self._config.local_time = index == 1
        for axis in self._axes:
            axis.local_time = self._config.local_time
            axis.picture = None
            axis.update()
        for plot in self._spectrogram_plots:
            plot.setLabel("bottom", "Local time" if self._config.local_time else "UTC")

    @QtCore.Slot()
    def _refresh(self) -> None:
        status = self._receiver.status()
        pi_health = describe_flags(status.status_flags, STATUS_FLAG_NAMES)
        timing_health = describe_flags(status.timing_flags, TIMING_FLAG_NAMES)
        self.statusBar().showMessage(
            f"{status.state}  latency {status.latency_ms:.1f} ms  "
            f"rate {status.effective_rate_hz:.1f} Hz  block gaps {status.block_gaps}  "
            f"sample gaps {status.sample_gaps}  recorder restarts {status.recorder_restarts}  "
            f"CRC {status.crc_errors}  "
            f"client queue drops {status.queue_drops}  Pi: {pi_health}  "
            f"timing: {timing_health}"
        )
        if self._paused:
            return
        latest: ProcessedSnapshot | None = None
        while True:
            try:
                latest = self._processed.get_nowait()
            except queue.Empty:
                break
        if latest is None or len(latest.timestamps_ns) == 0:
            return
        x = latest.timestamps_ns.astype(np.float64) / 1_000_000_000.0
        for index, (plot, curve) in enumerate(
            zip(self._wave_plots, self._wave_curves, strict=True)
        ):
            maximum = max(200, int(plot.vb.width() * 2))
            reduced_x, reduced_y = decimate_minmax(x, latest.waveform[:, index], maximum)
            curve.setData(reduced_x, reduced_y, connect="finite")
        spec = latest.spectrogram
        if len(spec.times_ns) < 1 or len(spec.frequencies_hz) < 2:
            return
        time_seconds = spec.times_ns.astype(np.float64) / 1_000_000_000.0
        time_step = (
            float(np.median(np.diff(time_seconds)))
            if len(time_seconds) > 1
            else self._config.hop / self._config.sample_rate_hz
        )
        frequency_step = float(np.median(np.diff(spec.frequencies_hz)))
        rect = QtCore.QRectF(
            time_seconds[0] - time_step / 2,
            spec.frequencies_hz[0] - frequency_step / 2,
            max(time_step, time_seconds[-1] - time_seconds[0] + time_step),
            spec.frequencies_hz[-1] - spec.frequencies_hz[0] + frequency_step,
        )
        for index, image in enumerate(self._images):
            image.setImage(
                spec.decibels[index],
                autoLevels=False,
                levels=(self._config.db_min, self._config.db_max),
            )
            image.setRect(rect)
            # ImageItem.setRect changes only the item transform, so its ViewBox
            # can remain at the initial zero-centred range when x is an epoch
            # timestamp. Set the known data range directly instead of calling
            # updateAutoRange(), which repeatedly scans linked ImageItems and
            # can starve Qt's paint/event loop during a live stream. Keeping
            # disableAutoRange=False preserves follow-live until the user pans
            # or zooms, which disables the corresponding auto-range axis.
            plot = self._spectrogram_plots[index]
            auto_x, auto_y = plot.vb.autoRangeEnabled()
            ranges: dict[str, tuple[float, float]] = {}
            if auto_x:
                ranges["xRange"] = (rect.left(), rect.right())
            if auto_y:
                ranges["yRange"] = (rect.top(), rect.bottom())
            if ranges:
                plot.vb.setRange(
                    **ranges,
                    padding=0.0,
                    disableAutoRange=False,
                )

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._timer.stop()
        self._receiver.stop()
        self._stop.set()
        self._receiver.join(timeout=5.0)
        self._processor.join(timeout=5.0)
        try:
            save_config(self._config, self._config_path)
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(
                self, "Configuration", f"Could not save settings: {error}"
            )
        event.accept()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ShakePie live monitor")
    parser.add_argument("--config", type=Path, help="client JSON configuration")
    parser.add_argument("--host", help="override TCP host (normally the SSH tunnel endpoint)")
    parser.add_argument("--port", type=int, help="override TCP port")
    parser.add_argument(
        "--capture", type=Path, help="append validated received frames to a local capture"
    )
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    config = load_config(arguments.config)
    if arguments.host is not None:
        config.host = arguments.host
    if arguments.port is not None:
        config.port = arguments.port
    config.validate()
    application = QtWidgets.QApplication(sys.argv)
    application.setApplicationName("ShakePie")
    window = MainWindow(config, arguments.config, arguments.capture)
    window.show()
    raise SystemExit(application.exec())


if __name__ == "__main__":
    main()
