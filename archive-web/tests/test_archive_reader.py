from __future__ import annotations

import struct
from pathlib import Path

import pytest
from conftest import write_recording

from shakepie_archive.archive_reader import RecordingError, _decode_block, read_recording


def test_normal_recovered_and_sequence_gap_metadata(tmp_path: Path) -> None:
    normal = write_recording(tmp_path, "normal.spb", first_sequences=(0, 3))
    recovered = write_recording(
        tmp_path, "recovered.recovered.spb", first_sequences=(20, 22), recovered=True
    )

    normal_metadata = read_recording(normal)
    recovered_metadata = read_recording(recovered)

    assert normal_metadata.station_id == "TEST"
    assert normal_metadata.sample_count == 4
    assert normal_metadata.block_count == 2
    assert normal_metadata.sequence_gap_count == 1
    assert normal_metadata.sample_rate_millihz == 200_000
    assert recovered_metadata.recovered is True


@pytest.mark.parametrize(
    ("offset", "message"),
    [
        (124, "header CRC32C"),
        (128 + 4 + 64, "block CRC32C"),
        (-1, "footer CRC32C"),
    ],
)
def test_crc_failures_are_rejected(tmp_path: Path, offset: int, message: str) -> None:
    path = write_recording(tmp_path, "bad.spb")
    content = bytearray(path.read_bytes())
    content[offset] ^= 0xFF
    path.write_bytes(content)
    with pytest.raises(RecordingError, match=message):
        read_recording(path)


def test_invalid_frame_length_and_missing_footer_are_rejected(tmp_path: Path) -> None:
    invalid = write_recording(tmp_path, "length.spb")
    content = bytearray(invalid.read_bytes())
    struct.pack_into("<I", content, 128, 1)
    invalid.write_bytes(content)
    with pytest.raises(RecordingError, match="block length"):
        read_recording(invalid)

    truncated = write_recording(tmp_path, "truncated.spb")
    truncated.write_bytes(truncated.read_bytes()[:-64])
    with pytest.raises(RecordingError, match="footer"):
        read_recording(truncated)


def test_out_of_order_block_is_rejected(tmp_path: Path) -> None:
    path = write_recording(tmp_path, "order.spb")
    content = bytearray(path.read_bytes())
    first_block_length = struct.unpack_from("<I", content, 128)[0]
    second_header = 128 + 4 + first_block_length + 4
    struct.pack_into("<Q", content, second_header + 16, 99)
    second_length = struct.unpack_from("<I", content, second_header - 4)[0]
    crc_offset = second_header + second_length - 4
    from shakepie_archive.archive_reader import _crc32c

    struct.pack_into("<I", content, crc_offset, _crc32c(content[second_header:crc_offset]))
    path.write_bytes(content)
    with pytest.raises(RecordingError, match="block sequence"):
        read_recording(path)


def test_partial_files_are_never_accepted(tmp_path: Path) -> None:
    path = write_recording(tmp_path, "active.partial")
    with pytest.raises(RecordingError, match="not finalized"):
        read_recording(path)


def test_existing_protocol_fixture_matches_python_decoder() -> None:
    fixture = Path(__file__).parents[2] / "protocol" / "fixtures" / "golden-block-v1.spb"
    if not fixture.is_file():
        pytest.skip("full-repository protocol fixture is unavailable in a standalone source copy")
    block, _ = _decode_block(fixture.read_bytes())
    assert block.block_sequence == 42
    assert block.first_sample_sequence == 1000
    assert block.sample_count == 3
