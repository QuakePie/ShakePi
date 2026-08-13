from __future__ import annotations

from pathlib import Path

from shakepie_archive.recorder_settings import (
    atomic_write,
    backup_file,
    parse_recorder_conf,
    read_station_id,
    render_recorder_conf,
    update_live_rates,
    validate_values,
)

BASE_CONF = """\
# ShakePie recorder configuration. Paths are examples and must match the Pi.
adc.backend=hardware
adc.spi_device=/dev/spidev0.0
adc.data_rate_sps=2000

acquisition.samples_per_block=64
acquisition.nominal_group_rate_hz=200
acquisition.max_consecutive_adc_errors=3

storage.rotation_minutes=15
storage.station_id=SHAKEPIE
"""

BASE_VALUES = parse_recorder_conf(BASE_CONF)


def test_parse_conf_reads_keys_and_ignores_comments() -> None:
    assert BASE_VALUES["adc.data_rate_sps"] == "2000"
    assert BASE_VALUES["storage.station_id"] == "SHAKEPIE"
    assert "adc.backend" in BASE_VALUES


def test_validate_accepts_consistent_hardware_rates() -> None:
    errors = validate_values(
        BASE_VALUES,
        {"acquisition.nominal_group_rate_hz": "300", "adc.data_rate_sps": "2000"},
    )
    assert errors == []


def test_validate_rejects_rate_the_pipeline_cannot_sustain() -> None:
    errors = validate_values(BASE_VALUES, {"acquisition.nominal_group_rate_hz": "500"})
    assert any("six times" in message for message in errors)


def test_validate_rejects_out_of_range_and_bad_values() -> None:
    errors = validate_values(
        BASE_VALUES,
        {
            "acquisition.samples_per_block": "512",
            "storage.rotation_minutes": "0",
            "storage.station_id": "bad station!",
            "adc.data_rate_sps": "1234",
        },
    )
    assert len(errors) == 4


def test_render_conf_replaces_in_place_and_preserves_comments() -> None:
    rendered = render_recorder_conf(BASE_CONF, {"storage.rotation_minutes": "30"})
    assert "storage.rotation_minutes=30" in rendered
    assert "# ShakePie recorder configuration." in rendered
    assert "adc.backend=hardware" in rendered
    assert rendered.count("storage.rotation_minutes") == 1


def test_render_conf_appends_missing_keys() -> None:
    rendered = render_recorder_conf("adc.backend=fake\n", {"storage.station_id": "TEST"})
    assert rendered.splitlines()[-1] == "storage.station_id=TEST"


def test_update_live_rates_rewrites_both_view_rates() -> None:
    conf = "[dayplot]\ntarget_sample_rate_hz = 200\n\n[live]\nsample_rate_hz = 200\n"
    updated = update_live_rates(conf, 250)
    assert "target_sample_rate_hz = 250" in updated
    assert "sample_rate_hz = 250" in updated
    assert "[live]" in updated


def test_atomic_write_and_backup(tmp_path: Path) -> None:
    target = tmp_path / "recorder.conf"
    target.write_text(BASE_CONF, encoding="utf-8")
    backup = backup_file(target)
    assert backup is not None and backup.is_file() and backup != target

    atomic_write(target, "new content\n")
    assert target.read_text(encoding="utf-8") == "new content\n"
    assert backup.read_text(encoding="utf-8") == BASE_CONF
    # Only the timestamp differs between consecutive backups; the old one is untouched.
    assert target.stat().st_size == len("new content\n")


def test_read_station_id_returns_the_configured_station(tmp_path: Path) -> None:
    conf = tmp_path / "recorder.conf"
    conf.write_text("# comment\nstorage.station_id=Caspian\n", encoding="utf-8")
    assert read_station_id(conf) == "Caspian"
    assert read_station_id(tmp_path / "missing.conf") is None
    invalid = tmp_path / "invalid.conf"
    invalid.write_text("storage.station_id=bad station!\n", encoding="utf-8")
    assert read_station_id(invalid) is None
