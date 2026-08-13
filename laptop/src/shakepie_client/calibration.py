"""Vectorized display calibration; authoritative files remain raw counts."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from .config import ChannelCalibration


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    values: npt.NDArray[np.float64]
    unit: str


def calibrate(
    counts: npt.NDArray[np.int32],
    mode: str,
    reference_voltage_volts: float,
    pga_gain: int,
    channels: list[ChannelCalibration],
) -> CalibrationResult:
    """Convert an (N,3) array without per-sample Python work."""
    if counts.ndim != 2 or counts.shape[1] != 3:
        raise ValueError("counts must have shape (N, 3)")
    polarity = np.asarray([channel.polarity for channel in channels], dtype=np.float64)
    raw = counts.astype(np.float64, copy=False) * polarity
    if mode == "counts":
        return CalibrationResult(raw, "counts")
    volts = raw * ((2.0 * reference_voltage_volts / pga_gain) / 8_388_607.0)
    if mode == "volts":
        return CalibrationResult(volts, "V")
    scale_attributes = {
        "velocity": "velocity_per_volt",
        "displacement": "displacement_per_volt",
        "acceleration": "acceleration_per_volt",
    }
    units = {"velocity": "m/s", "displacement": "m", "acceleration": "m/s²"}
    if mode not in units:
        raise ValueError(f"unsupported calibration mode {mode}")
    attribute = scale_attributes[mode]
    scale = np.asarray([getattr(channel, attribute) for channel in channels], dtype=np.float64)
    return CalibrationResult(volts * scale, units[mode])
