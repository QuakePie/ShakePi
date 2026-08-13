from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from conftest import write_recording
from flask.testing import FlaskClient

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings
from shakepie_archive.detector import CoincidenceDetector, DetectorConfig, StaLtaDetector

DAY_START_NS = 1_735_689_600_000_000_000  # 2025-01-01 00:00:00 UTC
PERIOD_NS = 5_000_000  # 200 Hz


def _noise_samples(count: int, amplitude: int = 10) -> list[int]:
    # A deterministic alternating pattern keeps the long-term mean flat and non-zero.
    return [amplitude if index % 2 == 0 else -amplitude for index in range(count)]


def _detector(**overrides) -> StaLtaDetector:
    config = DetectorConfig(
        sta_seconds=0.05,
        lta_seconds=0.5,
        threshold=4.0,
        off_seconds=0.5,
        cooldown_seconds=0.0,
        maximum_events=100,
    )
    return StaLtaDetector(config, 200.0)


def _feed_block(detector: StaLtaDetector, values: list[int], start_ns: int) -> list:
    events = []
    timestamp = start_ns
    for value in values:
        event = detector.feed(timestamp, value)
        if event is not None:
            events.append(event)
        timestamp += PERIOD_NS
    return events


def test_detector_config_validates_bounds() -> None:
    with pytest.raises(ValueError):
        DetectorConfig(lta_seconds=0.1, sta_seconds=1.0)
    with pytest.raises(ValueError):
        DetectorConfig(threshold=1.0)
    DetectorConfig(threshold=1.2)  # the lowest accepted trigger
    with pytest.raises(ValueError):
        DetectorConfig(minimum_components=0)
    with pytest.raises(ValueError):
        DetectorConfig(minimum_components=4)
    DetectorConfig(minimum_components=3)


def test_detector_ignores_quiet_noise() -> None:
    detector = _detector()
    events = _feed_block(detector, _noise_samples(400), DAY_START_NS)
    assert events == []
    assert detector.warmed_up


def test_detector_triggers_on_disturbance_and_reports_peak() -> None:
    detector = _detector()
    warmup = _noise_samples(200)
    start_ns = DAY_START_NS
    _feed_block(detector, warmup, start_ns)
    spike_start = start_ns + len(warmup) * PERIOD_NS
    disturbance = [5_000] * 40 + _noise_samples(120)
    events = _feed_block(detector, disturbance, spike_start)

    assert len(events) == 1
    event = events[0]
    assert event.started_ns >= spike_start
    assert event.ended_ns > event.started_ns
    assert event.peak_amplitude == 5_000
    assert event.peak_ratio >= 4.0


def test_detector_cooldown_suppresses_repeated_triggers() -> None:
    detector = StaLtaDetector(
        DetectorConfig(
            sta_seconds=0.05,
            lta_seconds=0.5,
            threshold=4.0,
            off_seconds=0.5,
            cooldown_seconds=10.0,
            maximum_events=100,
        ),
        200.0,
    )
    start_ns = DAY_START_NS
    values = _noise_samples(200) + [5_000] * 40 + _noise_samples(40) + [5_000] * 40
    values += _noise_samples(2000)  # ride out the 10 s cooldown
    events = _feed_block(detector, values, start_ns)
    assert len(events) == 1


def test_detector_reset_clears_state_without_inventing_events() -> None:
    detector = _detector()
    _feed_block(detector, _noise_samples(150), DAY_START_NS)
    detector.reset()
    assert not detector.warmed_up
    events = _feed_block(detector, [5_000] * 60, DAY_START_NS)
    assert events == []  # trigger needs a full window again before it can fire


# ---------------------------------------------------------------------------
# N-of-M coincidence trigger


def _coincidence_config(**overrides) -> DetectorConfig:
    values = {
        "sta_seconds": 0.05,
        "lta_seconds": 0.5,
        "threshold": 4.0,
        "off_seconds": 0.5,
        "cooldown_seconds": 0.0,
        "maximum_events": 100,
        "minimum_components": 2,
    }
    values.update(overrides)
    return DetectorConfig(**values)


def _noise_group(count: int, amplitude: int = 10) -> list[tuple[int, int, int]]:
    """Three channels of alternating noise so every channel has a non-zero mean."""
    return [
        (
            amplitude if index % 2 == 0 else -amplitude,
            -amplitude if index % 2 == 0 else amplitude,
            amplitude if index % 2 == 1 else -amplitude,
        )
        for index in range(count)
    ]


def _feed_coincidence(
    detector: CoincidenceDetector, samples: list[tuple[int, int, int]], start_ns: int
) -> list:
    events = []
    timestamp = start_ns
    for north, east, vertical in samples:
        event = detector.feed_sample(timestamp, north, east, vertical)
        if event is not None:
            events.append(event)
        timestamp += PERIOD_NS
    return events


def test_coincidence_rejects_single_component_disturbance() -> None:
    detector = CoincidenceDetector(_coincidence_config(), 200.0)
    noise = _noise_group(200)
    disturbance = _noise_group(160)
    samples = noise + [
        (north, east, 5_000 if index < 40 else vertical)
        for index, (north, east, vertical) in enumerate(disturbance)
    ]
    events = _feed_coincidence(detector, samples, DAY_START_NS)
    assert events == []  # only the vertical channel saw the spike


def test_coincidence_stores_event_when_two_components_trigger() -> None:
    detector = CoincidenceDetector(_coincidence_config(), 200.0)
    noise = _noise_group(200)
    disturbance = _noise_group(160)
    samples = noise + [
        (
            5_000 if index < 40 else north,
            5_000 if index < 40 else east,
            vertical,
        )
        for index, (north, east, vertical) in enumerate(disturbance)
    ]
    events = _feed_coincidence(detector, samples, DAY_START_NS)
    assert len(events) == 1
    event = events[0]
    assert event.started_ns >= DAY_START_NS + 200 * PERIOD_NS
    assert event.ended_ns > event.started_ns
    assert event.peak_amplitude == 5_000
    assert event.peak_ratio >= 4.0


def test_coincidence_single_component_mode_matches_legacy_trigger() -> None:
    config = _coincidence_config(minimum_components=1)
    legacy = StaLtaDetector(config, 200.0)
    coincidence = CoincidenceDetector(config, 200.0)
    values = _noise_samples(200) + [5_000] * 40 + _noise_samples(120)
    legacy_events = _feed_block(legacy, values, DAY_START_NS)
    coincidence_events = _feed_coincidence(
        coincidence, [(0, 0, value) for value in values], DAY_START_NS
    )
    assert len(legacy_events) == len(coincidence_events) == 1
    assert legacy_events[0] == coincidence_events[0]


def test_catalog_schema_v2_creates_events_and_migrates_v1(tmp_path) -> None:
    settings_catalog = tmp_path / "archive.sqlite"
    catalog = Catalog(settings_catalog)
    catalog.initialize()
    assert catalog.schema_version() == 2
    with catalog.reader() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "events" in tables

    # A schema-v1 catalog (no events table) is upgraded in place.
    old = sqlite3.connect(settings_catalog)
    old.executescript("DROP TABLE events; PRAGMA user_version = 1;")
    old.commit()
    old.close()
    catalog.initialize()
    assert catalog.schema_version() == 2


def test_store_event_trims_to_bound(settings: Settings, catalog: Catalog) -> None:
    start = DAY_START_NS
    for index in range(12):
        catalog.store_event(
            started_ns=start + index * 1_000_000_000,
            ended_ns=start + index * 1_000_000_000 + 500_000_000,
            peak_amplitude=1_000 + index,
            peak_ratio=5.0 + index,
            station_id="TEST",
            maximum_events=10,
        )
    events = catalog.recent_events(limit=50)
    assert len(events) == 10
    assert min(int(row["peak_amplitude"]) for row in events) == 1_002


def test_event_summary_counts_today(settings: Settings, catalog: Catalog) -> None:
    now_ns = int(datetime.now(UTC).timestamp() * 1e9)
    catalog.store_event(
        started_ns=now_ns - 60_000_000_000,
        ended_ns=now_ns,
        peak_amplitude=4_242,
        peak_ratio=6.5,
        station_id="TEST",
        maximum_events=100,
    )
    summary = catalog.event_summary(today_start_ns=now_ns - 86_400_000_000_000)
    assert summary["total"] == 1
    assert summary["today"] == 1
    assert summary["last_peak"] == 4_242


def test_events_page_lists_stored_events(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    write_recording(settings.archive.data_directory, "day.spb")
    catalog.reconcile(settings.archive.data_directory)
    catalog.store_event(
        started_ns=DAY_START_NS + 3_600_000_000_000,
        ended_ns=DAY_START_NS + 3_600_000_000_000 + 4_000_000_000,
        peak_amplitude=9_999,
        peak_ratio=7.25,
        station_id="TEST",
        maximum_events=100,
    )
    page = client.get("/events")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "9,999 counts" in body
    ratio_mark = chr(215)
    assert (f"{ratio_mark}7.3" in body) or (f"{ratio_mark}7.2" in body)
    assert 'at=01%3A00%3A00' in body or "at=01:00:00" in body


def test_latest_dayplot_day_requires_all_channels(
    settings: Settings, catalog: Catalog
) -> None:
    assert catalog.latest_dayplot_day() is None
    with catalog.transaction(immediate=True) as connection:
        connection.executemany(
            "INSERT INTO dayplot_cache "
            "(utc_day, source_fingerprint, channel, relative_path, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("2025-01-01", "f" * 64, channel, f"2025-01-01/x-{channel}.png", "now")
                for channel in ("north", "east", "vertical")
            ],
        )
    assert catalog.latest_dayplot_day() == "2025-01-01"


def test_delete_event_removes_only_that_row(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    catalog.store_event(
        started_ns=DAY_START_NS,
        ended_ns=DAY_START_NS + 1_000_000_000,
        peak_amplitude=100,
        peak_ratio=3.0,
        station_id="TEST",
        maximum_events=100,
    )
    catalog.store_event(
        started_ns=DAY_START_NS + 2_000_000_000,
        ended_ns=DAY_START_NS + 3_000_000_000,
        peak_amplitude=200,
        peak_ratio=4.0,
        station_id="TEST",
        maximum_events=100,
    )
    target = catalog.recent_events(limit=10)[0]

    response = client.post(
        f"/events/{target['id']}/delete", data={"csrf_token": authenticate()}
    )

    assert response.status_code == 302
    remaining = catalog.recent_events(limit=10)
    assert len(remaining) == 1
    assert remaining[0]["id"] != target["id"]
    with sqlite3.connect(settings.archive.catalog_path) as connection:
        audits = connection.execute("SELECT action FROM audit_log").fetchall()
    assert any(row[0] == "event_deleted" for row in audits)


def test_delete_unknown_event_reports_missing(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    response = client.post(
        "/events/424242/delete", data={"csrf_token": authenticate()}
    )
    assert response.status_code == 302
    page = client.get("/events")
    assert "no longer in the list" in page.get_data(as_text=True)


def test_delete_event_requires_csrf(client: FlaskClient) -> None:
    response = client.post("/events/1/delete", data={"csrf_token": "wrong"})
    assert response.status_code == 400


def _seed_events(catalog: Catalog, count: int) -> list[int]:
    ids = []
    for index in range(count):
        catalog.store_event(
            started_ns=DAY_START_NS + index * 1_000_000_000,
            ended_ns=DAY_START_NS + index * 1_000_000_000 + 500_000_000,
            peak_amplitude=1_000 + index,
            peak_ratio=3.0 + index,
            station_id="TEST",
            maximum_events=100,
        )
        ids.append(catalog.recent_events(limit=1)[0]["id"])
    return ids


def test_delete_events_bulk_removes_selected_rows(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    ids = _seed_events(catalog, 3)
    target_ids = ids[:2]

    response = client.post(
        "/events/delete-bulk",
        json={"ids": target_ids},
        headers={"X-CSRF-Token": "test-csrf"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"removed": 2, "requested": 2}
    remaining = catalog.recent_events(limit=10)
    assert [row["id"] for row in remaining] == [ids[2]]


def test_delete_events_bulk_skips_unknown_ids(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    ids = _seed_events(catalog, 1)

    response = client.post(
        "/events/delete-bulk",
        json={"ids": [ids[0], 987654]},
        headers={"X-CSRF-Token": "test-csrf"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"removed": 1, "requested": 2}


def test_delete_events_bulk_rejects_bad_payload(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    for payload in ({}, {"ids": []}, {"ids": "nope"}, {"ids": ["abc"]}):
        response = client.post(
            "/events/delete-bulk",
            json=payload,
            headers={"X-CSRF-Token": "test-csrf"},
        )
        assert response.status_code == 400
    assert catalog.recent_events(limit=10) == []


def test_delete_events_bulk_requires_csrf(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    response = client.post(
        "/events/delete-bulk", json={"ids": [1]}, headers={"X-CSRF-Token": "wrong"}
    )
    assert response.status_code == 400


def test_events_page_filters_by_utc_day(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    for offset in (0, 86_400_000_000_000, 86_400_000_000_000 + 3_600_000_000_000):
        catalog.store_event(
            started_ns=DAY_START_NS + offset,
            ended_ns=DAY_START_NS + offset + 500_000_000,
            peak_amplitude=500,
            peak_ratio=3.0,
            station_id="TEST",
            maximum_events=100,
        )

    page = client.get("/events?date=2025-01-02")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert body.count("js-remove-event") == 2
    assert 'value="2025-01-02"' in body

    page = client.get("/events?date=2025-01-01")
    assert client.get("/events?date=2025-01-01").get_data(as_text=True).count(
        "js-remove-event"
    ) == 1


def test_events_page_rejects_invalid_date(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    assert client.get("/events?date=not-a-date").status_code == 400


def test_events_page_range_filters(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    from datetime import UTC, datetime

    authenticate()
    now_ns = int(datetime.now(UTC).timestamp() * 1e9)
    catalog.store_event(
        started_ns=now_ns - 8 * 86_400_000_000_000,  # 8 days ago (outside 7-day window)
        ended_ns=now_ns - 86_400_000_000_000,
        peak_amplitude=1,
        peak_ratio=3.0,
        station_id="TEST",
        maximum_events=100,
    )
    catalog.store_event(
        started_ns=now_ns - 60_000_000_000,  # 1 min ago
        ended_ns=now_ns,
        peak_amplitude=2,
        peak_ratio=3.0,
        station_id="TEST",
        maximum_events=100,
    )
    all_page = client.get("/events")
    assert all_page.get_data(as_text=True).count("js-remove-event") == 2
    today_page = client.get("/events?range=today")
    assert today_page.get_data(as_text=True).count("js-remove-event") == 1
    seven_page = client.get("/events?range=7")
    assert seven_page.get_data(as_text=True).count("js-remove-event") == 1
