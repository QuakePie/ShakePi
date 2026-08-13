from __future__ import annotations

import struct
from collections.abc import Callable

import pytest
from conftest import BLOCK_HEADER, SAMPLE
from flask import Flask
from flask.testing import FlaskClient

from shakepie_archive.archive_reader import RecordingError, _crc32c
from shakepie_archive.config import LiveConfig
from shakepie_archive.live_data import LiveFrameParser, LiveStreamBuffer


def _live_frame(first_sequence: int = 100) -> bytes:
    payload = SAMPLE.pack(10, 20, 30, 0) + SAMPLE.pack(11, 21, 31, 5_000_000)
    encoded = bytearray(
        BLOCK_HEADER.pack(
            b"SPBK",
            1,
            64,
            len(payload),
            0,
            42,
            first_sequence,
            1_735_689_600_000_000_000,
            100,
            2,
            0,
            200_000,
            16,
            3,
            0,
        )
    )
    encoded.extend(payload)
    encoded.extend(struct.pack("<I", _crc32c(encoded)))
    return struct.pack("<I", len(encoded)) + encoded


def test_live_parser_accepts_chunked_crc_valid_frames_and_snapshot() -> None:
    frame = _live_frame()
    parser = LiveFrameParser()
    assert parser.feed(frame[:17]) == []
    blocks = parser.feed(frame[17:])
    assert len(blocks) == 1

    buffer = LiveStreamBuffer(LiveConfig(enabled=False, history_seconds=5))
    buffer.append_block(blocks[0])
    snapshot = buffer.snapshot(sample_rate_hz=200)
    assert snapshot["sample_rate_hz"] == 200.0
    assert snapshot["source_sample_rate_hz"] == 200.0
    assert snapshot["sequences"] == [100, 101]
    assert snapshot["north"] == [10, 11]
    assert snapshot["east"] == [20, 21]
    assert snapshot["vertical"] == [30, 31]

    incremental = buffer.snapshot(after_sequence=100, sample_rate_hz=200)
    assert incremental["reset"] is False
    assert incremental["sequences"] == [101]

def test_live_parser_rejects_invalid_crc_and_length() -> None:
    corrupted = bytearray(_live_frame())
    corrupted[-1] ^= 0xFF
    with pytest.raises(RecordingError, match="CRC32C"):
        LiveFrameParser().feed(corrupted)
    with pytest.raises(RecordingError, match="frame length"):
        LiveFrameParser().feed(struct.pack("<I", 1))


def test_live_page_and_api_require_login_and_return_bounded_payload(
    app: Flask,
    client: FlaskClient,
    authenticate: Callable[[], str],
) -> None:
    requested_rates: list[int | None] = []

    class FakeLive:
        def snapshot(
            self,
            after_sequence: int | None = None,
            sample_rate_hz: int | None = None,
        ) -> dict[str, object]:
            requested_rates.append(sample_rate_hz)
            return {
                "state": "connected",
                "error": "",
                "reset": after_sequence is None,
                "sample_rate_hz": float(sample_rate_hz or 200),
                "sample_gaps": 0,
                "age_ms": 10.0,
                "timestamps_ms": [1000.0],
                "sequences": [7],
                "north": [1],
                "east": [2],
                "vertical": [3],
            }

    app.extensions["shakepie_live"] = FakeLive()
    assert client.get("/live").status_code == 302
    authenticate()
    page = client.get("/live")
    assert page.status_code == 200
    assert b"Live data" in page.data
    assert b"0\xe2\x80\x93100 Hz" in page.data
    assert b"Sample rate" in page.data
    assert b"200 Hz" in page.data
    response = client.get("/api/live?after_sequence=6&sample_rate=200")
    assert response.status_code == 200
    assert response.get_json()["north"] == [1]
    assert requested_rates == [200]
    assert client.get("/api/live?after_sequence=-1").status_code == 400
    assert client.get("/api/live?sample_rate=300").status_code == 400
    assert client.get("/api/live?sample_rate=100").status_code == 400
