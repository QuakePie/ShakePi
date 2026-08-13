from __future__ import annotations

from shakepie_archive.naming import (
    canonical_dayplot_name,
    legacy_dayplot_name,
    legacy_fingerprint_name,
)


def test_canonical_dayplot_name_matches_documented_pattern() -> None:
    assert canonical_dayplot_name("Caspian", "2026-09-14", "north", "png") == (
        "20260914-Caspian-CH1.png"
    )
    assert canonical_dayplot_name("Caspian", "2026-09-14", "east", "png") == (
        "20260914-Caspian-CH2.png"
    )
    assert canonical_dayplot_name("Caspian", "2026-09-14", "vertical", "png") == (
        "20260914-Caspian-CH3.png"
    )
    assert canonical_dayplot_name("SHAKEPIE", "2026-01-02", "east", "jpg") == (
        "20260102-SHAKEPIE-CH2.jpg"
    )

def test_canonical_dayplot_name_sanitizes_station_and_extension() -> None:
    # The station keeps its configured case (e.g. "Caspian"); unsafe characters go.
    assert canonical_dayplot_name("weird station!", "2026-01-02", "north", "PNG") == (
        "20260102-weirdstation-CH1.png"
    )
    assert canonical_dayplot_name(None, "2026-01-02", "east", "png") == (
        "20260102-SHAKEPIE-CH2.png"
    )
    assert canonical_dayplot_name("", "2026-01-02", "north", "png") == (
        "20260102-SHAKEPIE-CH1.png"
    )

def test_legacy_fingerprint_name_detection() -> None:
    assert legacy_fingerprint_name("2026-09-03/05f7972f4a530d8f-north.png")
    assert legacy_fingerprint_name("4654774bd434eb98-vertical.jpg")
    assert not legacy_fingerprint_name("2026-09-03/20260903-SHAKEPIE-CH1.png")
    assert not legacy_fingerprint_name("2026-09-03/innocent-file.png")

def test_legacy_name_detection_includes_previous_conventions() -> None:
    assert legacy_dayplot_name("2026-09-03/05f7972f4a530d8f-north.png")
    assert legacy_dayplot_name("2026-09-03/SHAKEPIE_20260903_north_dayplot.png")
    assert legacy_dayplot_name("SHAKEPIE_20260903_vertical_dayplot.jpg")
    assert not legacy_dayplot_name("2026-09-03/20260903-SHAKEPIE-CH1.png")
    assert not legacy_dayplot_name("2026-09-03/innocent-file.png")
