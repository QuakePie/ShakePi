from __future__ import annotations

from pathlib import Path

import pytest

from shakepie_client.config import ClientConfig, load_config, save_config


def test_configuration_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    config = ClientConfig(host="localhost", visible_seconds=12.5)
    save_config(config, path)
    loaded = load_config(path)
    assert loaded.host == "localhost"
    assert loaded.visible_seconds == 12.5
    assert [channel.label for channel in loaded.channels] == ["North", "East", "Vertical"]


def test_legacy_single_physical_scale_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(
        '{"channels": ['
        '{"polarity": 1, "physical_scale_per_volt": 2.0, "label": "N"},'
        '{"polarity": 1, "physical_scale_per_volt": 3.0, "label": "E"},'
        '{"polarity": -1, "physical_scale_per_volt": 4.0, "label": "Z"}]}'
    )
    loaded = load_config(path)
    assert loaded.channels[2].velocity_per_volt == 4.0
    assert loaded.channels[2].displacement_per_volt == 4.0
    assert loaded.channels[2].acceleration_per_volt == 4.0


def test_invalid_fft_and_frequency_settings() -> None:
    config = ClientConfig(fft_size=1000)
    with pytest.raises(ValueError):
        config.validate()


def test_memory_products_and_queue_have_explicit_limits() -> None:
    with pytest.raises(ValueError, match="waveform configuration"):
        ClientConfig(sample_rate_hz=100_000.0, waveform_capacity_seconds=100.0).validate()
    with pytest.raises(ValueError, match="spectrogram configuration"):
        ClientConfig(
            sample_rate_hz=100_000.0,
            waveform_capacity_seconds=1.0,
            visible_seconds=1.0,
            fft_size=32_768,
            overlap=32_767,
            spectrogram_history_seconds=10.0,
        ).validate()
    with pytest.raises(ValueError, match="queue block count"):
        ClientConfig(queue_blocks=65_537).validate()
    config = ClientConfig(frequency_max_hz=500.0)
    with pytest.raises(ValueError):
        config.validate()
