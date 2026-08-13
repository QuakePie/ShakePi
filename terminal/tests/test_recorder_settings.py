from __future__ import annotations

from shakepie_terminal.recorder_settings import (
    parse_recorder_conf,
    render_recorder_conf,
    validate_values,
)

BASE_CONF = """\
# recorder configuration
adc.backend=hardware
adc.data_rate_sps=2000
acquisition.samples_per_block=64
acquisition.nominal_group_rate_hz=200
acquisition.max_consecutive_adc_errors=3
storage.rotation_minutes=15
storage.station_id=SHAKEPIE
"""

BASE_VALUES = parse_recorder_conf(BASE_CONF)


def test_parse_reads_keys_and_ignores_comments() -> None:
    assert BASE_VALUES["adc.data_rate_sps"] == "2000"
    assert BASE_VALUES["storage.station_id"] == "SHAKEPIE"


def test_validate_rejects_rate_the_pipeline_cannot_sustain() -> None:
    errors = validate_values(BASE_VALUES, {"acquisition.nominal_group_rate_hz": "1000"})
    assert any("six times" in message for message in errors)


def test_validate_rejects_bad_values() -> None:
    errors = validate_values(
        BASE_VALUES,
        {
            "adc.data_rate_sps": "1234",
            "acquisition.samples_per_block": "512",
            "storage.rotation_minutes": "0",
            "storage.station_id": "bad id!",
        },
    )
    assert len(errors) == 4


def test_validate_accepts_consistent_change() -> None:
    assert validate_values(BASE_VALUES, {"adc.data_rate_sps": "3750"}) == []


def test_render_replaces_in_place_and_preserves_comments() -> None:
    rendered = render_recorder_conf(BASE_CONF, {"storage.rotation_minutes": "30"})
    assert "storage.rotation_minutes=30" in rendered
    assert "# recorder configuration" in rendered
    assert rendered.count("storage.rotation_minutes") == 1


def test_render_appends_missing_keys() -> None:
    rendered = render_recorder_conf("adc.backend=fake\n", {"storage.station_id": "TEST"})
    assert rendered.splitlines()[-1] == "storage.station_id=TEST"
