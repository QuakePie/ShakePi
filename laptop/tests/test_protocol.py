from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from shakepie_client.protocol import (
    SAMPLE_DTYPE,
    BlockHeader,
    FrameParser,
    ProtocolError,
    decode_block,
    encode_block,
)

FIXTURE = Path(__file__).parents[2] / "protocol" / "fixtures" / "golden-block-v1.spb"


def expected_header() -> BlockHeader:
    return BlockHeader(5, 42, 1_000, 1_700_000_000_123_456_789, 9_876_543_210, 3, 2, 750_000, 0)


def test_golden_fixture_values_and_round_trip() -> None:
    encoded = FIXTURE.read_bytes()
    block = decode_block(encoded)
    assert block.header == expected_header()
    assert block.samples.tolist() == [
        (-8_388_608, 0, 8_388_607, 0),
        (-123_456, 654_321, -1, 1_333_333),
        (42, -42, 2_000_000, 2_666_666),
    ]
    assert encode_block(block.header, block.samples) == encoded


def test_partial_and_multiple_tcp_frames() -> None:
    encoded = FIXTURE.read_bytes()
    framed = struct.pack("<I", len(encoded)) + encoded
    parser = FrameParser()
    assert parser.feed(framed[:7]) == []
    blocks = parser.feed(framed[7:] + framed)
    assert [block.header.block_sequence for block in blocks] == [42, 42]
    assert parser.buffered_bytes == 0


@pytest.mark.parametrize("mutation", ["crc", "version", "count", "payload"])
def test_corrupt_fields_are_rejected(mutation: str) -> None:
    data = bytearray(FIXTURE.read_bytes())
    if mutation == "crc":
        data[-1] ^= 0x80
    elif mutation == "version":
        data[4:6] = struct.pack("<H", 99)
    elif mutation == "count":
        data[48:52] = struct.pack("<I", 999)
    else:
        data[8:12] = struct.pack("<I", 1)
    with pytest.raises(ProtocolError):
        decode_block(bytes(data))


def test_oversized_frame_is_rejected_without_growth() -> None:
    parser = FrameParser()
    with pytest.raises(ProtocolError):
        parser.feed(struct.pack("<I", 10_000_000))
    assert parser.buffered_bytes == 0


def test_dtype_is_exactly_little_endian_and_packed() -> None:
    assert SAMPLE_DTYPE.itemsize == 16
    samples = np.zeros(1, dtype=SAMPLE_DTYPE)
    samples["north"] = -1
    assert samples.tobytes()[:4] == b"\xff\xff\xff\xff"


def test_sequence_overflow_and_descending_offsets_are_rejected() -> None:
    block = decode_block(FIXTURE.read_bytes())
    overflow = BlockHeader(
        block.header.status_flags,
        block.header.block_sequence,
        (1 << 64) - 1,
        block.header.utc_start_ns,
        block.header.monotonic_start_ns,
        block.header.sample_count,
        block.header.timing_flags,
        block.header.nominal_group_rate_millihz,
        block.header.channel_order,
    )
    with pytest.raises(ProtocolError, match="overflows"):
        encode_block(overflow, block.samples)
    samples = block.samples.astype(SAMPLE_DTYPE, copy=True)
    samples["offset_ns"] = [0, 3_000_000, 2_000_000]
    with pytest.raises(ProtocolError, match="out of order"):
        encode_block(block.header, samples)
    samples = block.samples.astype(SAMPLE_DTYPE, copy=True)
    samples["north"][1] = 8_388_608
    with pytest.raises(ProtocolError, match="24-bit"):
        encode_block(block.header, samples)
