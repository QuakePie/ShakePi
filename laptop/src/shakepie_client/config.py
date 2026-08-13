"""Persistent, validated laptop-client settings."""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ChannelCalibration:
    polarity: int = 1
    velocity_per_volt: float = 1.0
    displacement_per_volt: float = 1.0
    acceleration_per_volt: float = 1.0
    label: str = ""

    def validate(self) -> None:
        if self.polarity not in (-1, 1):
            raise ValueError("channel polarity must be -1 or 1")
        scales = (
            self.velocity_per_volt,
            self.displacement_per_volt,
            self.acceleration_per_volt,
        )
        if any(not math.isfinite(scale) or not 0.0 < scale < 1e12 for scale in scales):
            raise ValueError("channel calibration scales must be finite and positive")
        if not self.label or len(self.label) > 64:
            raise ValueError("channel label must contain 1..64 characters")


@dataclass(slots=True)
class ClientConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    visible_seconds: float = 30.0
    sample_rate_hz: float = 750.0
    waveform_capacity_seconds: float = 300.0
    queue_blocks: int = 128
    reconnect_initial_seconds: float = 0.25
    reconnect_max_seconds: float = 10.0
    socket_timeout_seconds: float = 2.0
    units: str = "counts"
    reference_voltage_volts: float = 2.5
    pga_gain: int = 64
    local_time: bool = False
    window: str = "hann"
    fft_size: int = 1024
    overlap: int = 768
    spectrogram_history_seconds: float = 120.0
    frequency_min_hz: float = 0.0
    frequency_max_hz: float = 100.0
    color_map: str = "viridis"
    db_min: float = -120.0
    db_max: float = -20.0
    channels: list[ChannelCalibration] = field(
        default_factory=lambda: [
            ChannelCalibration(label="North"),
            ChannelCalibration(label="East"),
            ChannelCalibration(label="Vertical"),
        ]
    )

    @property
    def hop(self) -> int:
        return self.fft_size - self.overlap

    def validate(self) -> None:
        if not self.host:
            raise ValueError("host cannot be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be in 1..65535")
        if not 0.5 <= self.visible_seconds <= self.waveform_capacity_seconds:
            raise ValueError("visible time must be between 0.5 seconds and buffer capacity")
        if not 0.0 < self.sample_rate_hz <= 100_000.0:
            raise ValueError("sample rate is invalid")
        if not 1.0 <= self.waveform_capacity_seconds <= 86_400.0:
            raise ValueError("waveform capacity must be in 1..86400 seconds")
        waveform_samples = math.ceil(self.waveform_capacity_seconds * self.sample_rate_hz)
        if waveform_samples > 5_000_000:
            raise ValueError("waveform configuration exceeds the 5000000-sample memory limit")
        if not 2 <= self.queue_blocks <= 65_536:
            raise ValueError("queue block count must be in 2..65536")
        if not 0.01 <= self.reconnect_initial_seconds <= self.reconnect_max_seconds <= 300.0:
            raise ValueError("reconnect backoff must satisfy 0.01 <= initial <= maximum <= 300")
        if not 0.05 <= self.socket_timeout_seconds <= 300.0:
            raise ValueError("socket timeout must be in 0.05..300 seconds")
        if self.units not in {"counts", "volts", "velocity", "displacement", "acceleration"}:
            raise ValueError("unsupported calibration mode")
        if self.pga_gain not in {1, 2, 4, 8, 16, 32, 64}:
            raise ValueError("unsupported PGA gain")
        if not math.isfinite(self.reference_voltage_volts) or not (
            0.0 < self.reference_voltage_volts <= 100.0
        ):
            raise ValueError("reference voltage must be finite and positive")
        if self.fft_size < 64 or self.fft_size > 32768 or self.fft_size & (self.fft_size - 1):
            raise ValueError("FFT size must be a power of two in 64..32768")
        if not 0 <= self.overlap < self.fft_size or self.hop <= 0:
            raise ValueError("overlap must be smaller than FFT size")
        if not 0 <= self.frequency_min_hz < self.frequency_max_hz <= self.sample_rate_hz / 2:
            raise ValueError("frequency range must fit the Nyquist interval")
        if not self.db_min < self.db_max:
            raise ValueError("dB minimum must be below maximum")
        if not 1.0 <= self.spectrogram_history_seconds <= 86_400.0:
            raise ValueError("spectrogram history must be in 1..86400 seconds")
        spectrogram_frames = max(
            2,
            math.ceil(self.spectrogram_history_seconds * self.sample_rate_hz / self.hop),
        )
        spectrogram_values = 3 * (self.fft_size // 2 + 1) * spectrogram_frames
        if spectrogram_values > 25_000_000:
            raise ValueError("spectrogram configuration exceeds the 25000000-value memory limit")
        if not self.window or not self.color_map:
            raise ValueError("window and color map names cannot be empty")
        if len(self.channels) != 3:
            raise ValueError("exactly three channel calibrations are required")
        for channel in self.channels:
            channel.validate()


def default_config_path() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "ShakePie" / "client.json"


def load_config(path: Path | None = None) -> ClientConfig:
    target = path or default_config_path()
    if not target.exists():
        config = ClientConfig()
        config.validate()
        return config
    raw: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    if "channels" in raw:
        channels = []
        for item in raw["channels"]:
            values = dict(item)
            legacy_scale = values.pop("physical_scale_per_volt", None)
            if legacy_scale is not None:
                values.setdefault("velocity_per_volt", legacy_scale)
                values.setdefault("displacement_per_volt", legacy_scale)
                values.setdefault("acceleration_per_volt", legacy_scale)
            channels.append(ChannelCalibration(**values))
        raw["channels"] = channels
    config = ClientConfig(**raw)
    config.validate()
    return config


def save_config(config: ClientConfig, path: Path | None = None) -> None:
    config.validate()
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
