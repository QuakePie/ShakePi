from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta

from conftest import write_recording
from flask.testing import FlaskClient

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings
from shakepie_archive.miniseed import (
    RECORD_LENGTH,
    SAMPLES_PER_RECORD,
    encode_record,
    encode_series,
    nominal_rate_hz,
)

START_NS = 1_735_689_600_000_000_000  # 2025-01-01 00:00:00 UTC


def _parse_records(data: bytes) -> list[dict]:
    """A minimal miniSEED 2 reader used to verify what the writer produced."""
    assert len(data) % RECORD_LENGTH == 0
    records = []
    for offset in range(0, len(data), RECORD_LENGTH):
        chunk = data[offset : offset + RECORD_LENGTH]
        year, day, hour, minute, second, _unused, tenth_ms = struct.unpack_from(
            "<HHBBBBH", chunk, 20
        )
        sample_count, factor, multiplier = struct.unpack_from("<Hhh", chunk, 30)
        data_offset, blockette_offset = struct.unpack_from("<HH", chunk, 44)
        blockette, next_offset, encoding, byte_order, length_exponent, _pad = (
            struct.unpack_from("<HHBBBB", chunk, blockette_offset)
        )
        assert blockette == 1000 and next_offset == 0
        assert (encoding, byte_order, length_exponent) == (3, 0, 9)
        samples = list(struct.unpack_from(f"<{sample_count}i", chunk, data_offset))
        start = datetime(year, 1, 1, hour, minute, second, tzinfo=UTC) + timedelta(
            days=day - 1, microseconds=tenth_ms * 100
        )
        records.append(
            {
                "sequence": int(chunk[0:6]),
                "quality": chunk[6:7].decode(),
                "station": chunk[8:13].decode().strip(),
                "channel": chunk[15:18].decode().strip(),
                "network": chunk[18:20].decode().strip(),
                "start_ns": int(start.timestamp() * 1e9),
                "sample_count": sample_count,
                "rate": factor * multiplier,
                "samples": samples,
                "padding_zero": all(
                    value == 0
                    for value in struct.unpack_from(
                        f"<{(RECORD_LENGTH - data_offset - sample_count * 4) // 4}i",
                        chunk,
                        data_offset + sample_count * 4,
                    )
                )
                if sample_count < SAMPLES_PER_RECORD
                else True,
            }
        )
    return records


def test_encode_series_roundtrips_values_and_times() -> None:
    values = list(range(1, 251))  # 250 samples -> records of 112, 112, 26
    data = encode_series(START_NS, 200, values, "test", "EHZ")
    records = _parse_records(data)
    assert [record["sample_count"] for record in records] == [112, 112, 26]
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert [record["quality"] for record in records] == ["D"] * 3
    assert [record["samples"] for record in records] == [
        values[0:112],
        values[112:224],
        values[224:250],
    ]
    period_ns = 5_000_000
    assert records[0]["start_ns"] == START_NS
    assert records[1]["start_ns"] == START_NS + 112 * period_ns
    assert records[2]["start_ns"] == START_NS + 224 * period_ns
    assert records[0]["channel"] == "EHZ"
    assert records[0]["station"] == "TEST"
    assert records[0]["network"] == "SP"
    assert records[0]["rate"] == 200
    assert all(record["padding_zero"] for record in records)


def test_encode_record_wraps_sequence_number() -> None:
    record = encode_record(1_000_000, "station", "SP", "EHN", START_NS, 200, [7])
    parsed = _parse_records(record)
    assert parsed[0]["sequence"] == 0  # 1000000 % 1000000
    assert parsed[0]["samples"] == [7]


def test_series_start_time_encodes_fractional_seconds() -> None:
    start = START_NS + 250_000_000  # 0.25 s into the day
    parsed = _parse_records(encode_series(start, 200, [1, 2, 3], "t", "EHZ"))
    # the reader rebuilds the timestamp through datetime floats, so allow ~1 us
    assert abs(parsed[0]["start_ns"] - start) <= 1_000


def test_nominal_rate_helpers() -> None:
    assert nominal_rate_hz(200_000) == 200
    assert nominal_rate_hz(None) == 200
    assert nominal_rate_hz(199_660) == 200


def test_miniseed_endpoint_exports_all_channels(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    write_recording(settings.archive.data_directory, "day.spb")
    catalog.reconcile(settings.archive.data_directory)
    row = catalog.records_for_day("2025-01-01")[0]

    response = client.get(f"/recordings/{int(row['id'])}/miniseed")

    assert response.status_code == 200
    assert response.mimetype == "application/vnd.fdsn.mseed"
    records = _parse_records(response.data)
    assert len(records) == 3  # 4 contiguous samples per channel fit one record each
    assert [record["channel"] for record in records] == ["EHN", "EHE", "EHZ"]
    assert records[0]["samples"] == [1, 2, 3, 4]  # conftest north pattern
    assert records[1]["samples"] == [2, 3, 4, 5]  # east
    assert records[2]["samples"] == [3, 4, 5, 6]  # vertical
    assert [record["sequence"] for record in records] == [1, 2, 3]  # continuous numbering
    assert "miniseed" in response.headers["Content-Disposition"]


def test_miniseed_endpoint_starts_new_series_after_gap(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    # sequences 0,1 then 3,4: the missing sequence 2 splits the export
    write_recording(
        settings.archive.data_directory,
        "gap.spb",
        first_sequences=(0, 3),
    )
    catalog.reconcile(settings.archive.data_directory)
    row = catalog.records_for_day("2025-01-01")[0]

    response = client.get(f"/recordings/{int(row['id'])}/miniseed?channel=vertical")
    records = _parse_records(response.data)
    assert len(records) == 2
    period_ns = 5_000_000
    # the second series restarts its clock at the block after the gap
    # (the reader rebuilds timestamps through datetime floats, so allow ~1 us)
    assert abs(records[0]["start_ns"] - START_NS) <= 1_000
    assert abs(records[1]["start_ns"] - (START_NS + 2 * period_ns)) <= 1_000
    assert [record["samples"] for record in records] == [[3, 4], [5, 6]]
    assert records[1]["sequence"] == 2


def test_miniseed_endpoint_rejects_unknown_channel_and_missing_recording(
    settings: Settings, catalog: Catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    write_recording(settings.archive.data_directory, "day.spb")
    catalog.reconcile(settings.archive.data_directory)
    row = catalog.records_for_day("2025-01-01")[0]
    assert client.get(f"/recordings/{int(row['id'])}/miniseed?channel=diagonal").status_code == 400
    assert client.get("/recordings/9999/miniseed").status_code == 404


# ---------------------------------------------------------------------------
# command-line exporter (used by the SSH console)


def test_miniseed_export_cli_converts_files_and_days(
    settings: Settings, catalog: Catalog, tmp_path, capsys
) -> None:
    from shakepie_archive.miniseed_export import main

    recordings = settings.archive.data_directory
    write_recording(recordings, "TEST_20250101T010203Z.spb")
    write_recording(recordings, "TEST_20250102T010203Z.spb")
    write_recording(recordings, "TEST_20250101T050607Z.partial.spb")  # never exported
    out = tmp_path / "scratch"

    exit_code = main(
        ["--file", str(recordings / "TEST_20250101T010203Z.spb"), "--out", str(out)]
    )
    assert exit_code == 0
    exported = out / "TEST_20250101T010203Z.miniseed"
    records = _parse_records(exported.read_bytes())
    assert [record["channel"] for record in records] == ["EHN", "EHE", "EHZ"]
    assert records[0]["samples"] == [1, 2, 3, 4]

    exit_code = main(
        ["--day", "2025-01-01", "--recordings-dir", str(recordings), "--out", str(out)]
    )
    assert exit_code == 0
    assert (out / "TEST_20250101T010203Z.miniseed").exists()
    assert not (out / "TEST_20250102T010203Z.miniseed").exists()  # other day is excluded
    assert not list(out.glob("*partial*"))


def test_miniseed_export_cli_single_channel_and_failures(
    settings: Settings, catalog: Catalog, tmp_path, capsys
) -> None:
    from shakepie_archive.miniseed_export import main

    recordings = settings.archive.data_directory
    good = recordings / "TEST_20250101T010203Z.spb"
    write_recording(recordings, "TEST_20250101T010203Z.spb")
    broken = recordings / "BROKEN_20250101T010203Z.spb"
    broken.write_bytes(b"garbage")
    out = tmp_path / "scratch"

    exit_code = main(
        [
            "--file", str(good),
            "--file", str(broken),
            "--channel", "vertical",
            "--out", str(out),
        ]
    )
    assert exit_code == 1  # the broken file is reported but does not stop the rest
    exported = out / "TEST_20250101T010203Z_vertical.miniseed"
    records = _parse_records(exported.read_bytes())
    assert [record["channel"] for record in records] == ["EHZ"]
    assert records[0]["samples"] == [3, 4, 5, 6]

    assert main(["--day", "2025-02-30", "--recordings-dir", str(recordings),
                 "--out", str(out)]) == 1  # no such day
