from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shakepie_archive.watch import (
    WatchPoint,
    WatchStore,
    cenc_feed_url,
    check_point,
    haversine_km,
    isc_query_url,
    parse_cenc_json,
    parse_isc_text,
    parse_usgs_geojson,
    query_source,
    sources_for,
    usgs_query_url,
    validate_point_fields,
)

POINT = WatchPoint(
    name="home",
    latitude=35.7,
    longitude=51.4,
    radius_km=300,
    sources="both",
    min_magnitude=2.0,
    created_at="2026-09-17T00:00:00+00:00",
)

USGS_SAMPLE = json.dumps(
    {
        "features": [
            {
                "id": "us7000abcd",
                "properties": {
                    "mag": 4.5,
                    "place": "54 km SE of Tehran",
                    "time": 1_735_689_600_000,
                },
                "geometry": {"coordinates": [51.8, 35.5, 10.0]},
            },
            {
                "id": "faraway",
                "properties": {"mag": 6.0, "place": "too far", "time": 1_735_689_660_000},
                "geometry": {"coordinates": [60.0, 35.5, 5.0]},
            },
        ]
    }
)

ISC_SAMPLE = (
    "#EventID |Type |Magnitude |Magnitude type |Longitude |Latitude |Depth/km |Time |Event Region\n"
    " 1234567 | earthquake | 4.2 | mb |  51.5 |  35.6 |  8 | 2026-01-01T01:02:03.0 | Some region\n"
    " badline | earthquake |  |  |  51.5 |  35.6 |  8 | not-a-time | x\n"
)

CENC_SAMPLE = json.dumps(
    [
        {
            "id": "CD.20260916135950.501",
            "time": "2026-09-16 13:52:00",
            "latitude": 37.8,
            "longitude": 95.63,
            "depth": 10,
            "magnitude": 3.6,
            "location": "青海海西州直辖区",
        },
        {
            "id": "bad-row",
            "time": "not-a-time",
            "latitude": 1.0,
            "longitude": 2.0,
            "depth": 5,
            "magnitude": 3.0,
            "location": "x",
        },
        {
            "id": "CC.20260914091300.2",
            "time": "2026-09-14 09:13:00",
            "latitude": 25.1,
            "longitude": 98.5,
            "depth": "9",
            "magnitude": "4.1",
            "location": "云南保山市",
        },
    ]
)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_now(self, title: str, body: str) -> None:
        self.sent.append((title, body))


def test_haversine_known_distance() -> None:
    distance = haversine_km(0.0, 0.0, 0.0, 1.0)
    assert 110 < distance < 113  # one degree of longitude at the equator


def test_validate_point_fields_accepts_and_rejects() -> None:
    errors, point = validate_point_fields("home", "35.7", "51.4", "300", "2.0", "both")
    assert errors == []
    assert point is not None and point.name == "home"
    for bad in (
        ("bad name!", "0", "0", "100", "2", "usgs"),
        ("ok", "99", "0", "100", "2", "usgs"),
        ("ok", "0", "-181", "100", "2", "usgs"),
        ("ok", "0", "0", "5000", "2", "usgs"),
        ("ok", "0", "0", "100", "12", "usgs"),
        ("ok", "0", "0", "100", "2", "emsc"),
    ):
        assert validate_point_fields(*bad)[0]


def test_usgs_and_isc_urls() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    usgs = usgs_query_url("https://earthquake.usgs.gov", POINT, start, end)
    assert "earthquake.usgs.gov/fdsnws/event/1/query" in usgs
    assert "format=geojson" in usgs and "maxradiuskm=300" in usgs
    assert "starttime=2026-01-01T00:00:00Z" in usgs
    isc = isc_query_url("https://isc.ac.uk", POINT, start, end)
    assert "isc.ac.uk/fdsnws/event/1/query" in isc and "format=text" in isc


def test_parse_usgs_geojson() -> None:
    quakes = parse_usgs_geojson(USGS_SAMPLE)
    assert len(quakes) == 2
    assert quakes[0]["source_id"] == "us7000abcd"
    assert quakes[0]["time_ns"] == 1_735_689_600_000_000_000
    assert quakes[0]["magnitude"] == 4.5
    assert quakes[0]["depth_km"] == 10.0


def test_parse_isc_text_maps_header_and_skips_bad_rows() -> None:
    quakes = parse_isc_text(ISC_SAMPLE)
    assert len(quakes) == 1
    assert quakes[0]["source_id"] == "1234567"
    assert quakes[0]["magnitude"] == 4.2
    assert quakes[0]["latitude"] == 35.6
    assert quakes[0]["place"] == "Some region"


def test_query_source_filters_distance_and_magnitude() -> None:
    def fetch(url: str) -> str:
        assert "maxradiuskm=300" in url
        return USGS_SAMPLE

    hits = query_source(
        "usgs", POINT, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC),
        usgs_base="https://earthquake.usgs.gov", isc_base="https://isc.ac.uk", fetch=fetch,
    )
    # query_source returns distances; the radius cut is applied by check_point
    by_id = {hit.source_id: hit for hit in hits}
    assert by_id["us7000abcd"].distance_km < 300
    assert by_id["faraway"].distance_km > 500


def test_cenc_source_selection_and_url() -> None:
    assert sources_for("cenc") == ["cenc"]
    assert sources_for("usgs_cenc") == ["usgs", "cenc"]
    assert sources_for("all") == ["usgs", "isc", "cenc"]
    errors, point = validate_point_fields("home", "35.7", "51.4", "300", "2.0", "cenc")
    assert errors == [] and point is not None
    errors, _point = validate_point_fields("home", "35.7", "51.4", "300", "2.0", "emsc")
    assert errors
    assert cenc_feed_url("https://www.ceic.ac.cn/") == "https://www.ceic.ac.cn/data/data.json"


def test_parse_cenc_json_converts_beijing_time_and_skips_bad_rows() -> None:
    quakes = parse_cenc_json(CENC_SAMPLE)
    assert len(quakes) == 2  # the malformed time row is dropped
    first = quakes[0]
    assert first["source_id"] == "CD.20260916135950.501"
    # 13:52:00 Beijing time is 05:52:00 UTC
    assert first["time_ns"] == 1_789_537_920_000_000_000
    assert first["magnitude"] == 3.6
    assert first["depth_km"] == 10.0
    assert first["place"] == "青海海西州直辖区"
    second = quakes[1]
    assert second["magnitude"] == 4.1  # numeric strings are accepted
    assert second["depth_km"] == 9.0
    with pytest.raises(ValueError):
        parse_cenc_json("not json")  # check_point reports this as a source error
    assert parse_cenc_json("{}") == []


def test_query_source_cenc_filters_time_window() -> None:
    def fetch(url: str) -> str:
        assert "data.json" in url
        return CENC_SAMPLE

    hits = query_source(
        "cenc", POINT,
        datetime(2026, 9, 16, 5, 0, tzinfo=UTC), datetime(2026, 9, 16, 6, 0, tzinfo=UTC),
        usgs_base="https://x", isc_base="https://y", fetch=fetch,
    )
    assert len(hits) == 1  # only the 05:52 UTC quake is inside the window
    assert hits[0].source_id == "CD.20260916135950.501"


def test_check_point_cenc_end_to_end(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "watch.json")
    point = WatchPoint(
        name="qinghai", latitude=37.0, longitude=96.0, radius_km=300,
        sources="cenc", min_magnitude=2.5, created_at="2026-09-17T00:00:00+00:00",
    )
    store.upsert_point(point)

    added, error = check_point(
        store, point, datetime(2026, 9, 16, 12, 0, tzinfo=UTC), None,
        usgs_base="https://x", isc_base="https://y", timeout=5,
        fetch=lambda url: CENC_SAMPLE,
    )
    assert error == ""
    assert added == 1  # the M3.6 is ~140 km away and inside the 24 h window
    assert store.quakes("qinghai")[0]["source"] == "cenc"


def test_store_points_and_quakes_roundtrip(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "watch.json")
    assert store.interval_hours() == 1
    store.set_interval_hours(4)
    store.upsert_point(POINT)
    # same name replaces instead of duplicating
    store.upsert_point(
        WatchPoint(POINT.name, 1.0, 2.0, 100, "usgs", 1.0, POINT.created_at)
    )
    assert len(store.points()) == 1
    assert store.points()[0].radius_km == 100

    def fake_fetch(url: str) -> str:
        return USGS_SAMPLE

    hits = query_source(
        "usgs", POINT, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC),
        usgs_base="https://earthquake.usgs.gov", isc_base="https://isc.ac.uk",
        fetch=fake_fetch,
    )
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert store.add_quakes("home", hits, now) == 2
    assert store.add_quakes("home", hits, now) == 0  # dedupe by source id
    quakes = store.quakes("home")
    assert quakes[0]["time_ns"] > quakes[1]["time_ns"]  # newest first

    # a new store instance reads the same persisted data
    assert WatchStore(tmp_path / "watch.json").points()[0].latitude == 1.0

    assert store.delete_point("home")
    assert store.points() == [] and store.quakes("home") == []


def test_check_point_stores_new_and_notifies_once(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "watch.json")
    store.upsert_point(POINT)
    notifier = FakeNotifier()

    def fetch(url: str) -> str:
        return USGS_SAMPLE

    added, error = check_point(
        store, POINT, datetime(2026, 1, 1, 12, 0, tzinfo=UTC), notifier,
        usgs_base="https://earthquake.usgs.gov", isc_base="https://isc.ac.uk",
        timeout=5, fetch=fetch,
    )
    assert error == ""
    assert added == 1  # the faraway M6 was filtered by radius
    assert len(notifier.sent) == 1
    assert "home" in notifier.sent[0][0]
    assert "M4.5" in notifier.sent[0][1]

    # the second check queries a shifted window but nothing is new
    added, error = check_point(
        store, POINT, datetime(2026, 1, 1, 13, 0, tzinfo=UTC), notifier,
        usgs_base="https://earthquake.usgs.gov", isc_base="https://isc.ac.uk",
        timeout=5, fetch=fetch,
    )
    assert added == 0 and error == ""
    assert len(notifier.sent) == 1


def test_check_point_reports_source_errors(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "watch.json")
    store.upsert_point(POINT)

    def failing(url: str) -> str:
        raise OSError("network unreachable")

    added, error = check_point(
        store, POINT, datetime(2026, 1, 1, tzinfo=UTC), None,
        usgs_base="https://x", isc_base="https://y",
        timeout=1, fetch=failing,
    )
    assert added == 0
    assert "usgs" in error and "isc" in error
