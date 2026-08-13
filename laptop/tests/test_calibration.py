from __future__ import annotations

import numpy as np
import pytest

from shakepie_client.calibration import calibrate
from shakepie_client.config import ChannelCalibration

CHANNELS = [
    ChannelCalibration(1, 1.0, 10.0, 100.0, "N"),
    ChannelCalibration(-1, 2.0, 20.0, 200.0, "E"),
    ChannelCalibration(1, 3.0, 30.0, 300.0, "Z"),
]


def test_counts_apply_channel_polarity() -> None:
    counts = np.array([[10, 20, -30]], dtype=np.int32)
    result = calibrate(counts, "counts", 2.5, 1, CHANNELS)
    assert result.unit == "counts"
    np.testing.assert_array_equal(result.values, [[10.0, -20.0, -30.0]])


def test_voltage_and_future_physical_scale_are_separate_from_raw() -> None:
    counts = np.array([[8_388_607, 8_388_607, 8_388_607]], dtype=np.int32)
    volts = calibrate(counts, "volts", 2.5, 1, CHANNELS)
    np.testing.assert_allclose(volts.values, [[5.0, -5.0, 5.0]])
    velocity = calibrate(counts, "velocity", 2.5, 1, CHANNELS)
    np.testing.assert_allclose(velocity.values, [[5.0, -10.0, 15.0]])
    assert velocity.unit == "m/s"
    displacement = calibrate(counts, "displacement", 2.5, 1, CHANNELS)
    np.testing.assert_allclose(displacement.values, [[50.0, -100.0, 150.0]])
    acceleration = calibrate(counts, "acceleration", 2.5, 1, CHANNELS)
    np.testing.assert_allclose(acceleration.values, [[500.0, -1000.0, 1500.0]])


def test_bad_shape_or_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        calibrate(np.zeros(3, dtype=np.int32), "counts", 2.5, 1, CHANNELS)
    with pytest.raises(ValueError):
        calibrate(np.zeros((1, 3), dtype=np.int32), "banana", 2.5, 1, CHANNELS)
