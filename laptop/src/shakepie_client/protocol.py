"""Strict, allocation-conscious decoder for ShakePie protocol v1."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

MAGIC: Final = b"SPBK"
VERSION: Final = 1
HEADER_SIZE: Final = 64
SAMPLE_SIZE: Final = 16
CRC_SIZE: Final = 4
MAX_SAMPLES: Final = 256
MAX_BLOCK_SIZE: Final = HEADER_SIZE + MAX_SAMPLES * SAMPLE_SIZE + CRC_SIZE
MAX_FRAME_SIZE: Final = MAX_BLOCK_SIZE
TIMING_UTC_CLOCK_STEP: Final = 1 << 0
MAX_SIGNED_TIMESTAMP_NS: Final = (1 << 63) - 1

_HEADER = struct.Struct("<4sHHIIQQQQIIIHBB")
_SAMPLE = struct.Struct("<iiiI")
_U32 = struct.Struct("<I")
SAMPLE_DTYPE: Final[np.dtype[np.void]] = np.dtype(
    [
        ("north", "<i4"),
        ("east", "<i4"),
        ("vertical", "<i4"),
        ("offset_ns", "<u4"),
    ],
    align=False,
)


class ProtocolError(ValueError):
    """Raised when untrusted protocol data is malformed."""


@dataclass(frozen=True, slots=True)
class BlockHeader:
    status_flags: int
    block_sequence: int
    first_sample_sequence: int
    utc_start_ns: int
    monotonic_start_ns: int
    sample_count: int
    timing_flags: int
    nominal_group_rate_millihz: int
    channel_order: int


@dataclass(frozen=True, slots=True)
class Block:
    header: BlockHeader
    samples: npt.NDArray[np.void]
    encoded: bytes


def crc32c(data: bytes | bytearray | memoryview) -> int:
    """Return Castagnoli CRC-32 using the protocol's reflected parameters."""
    crc = 0xFFFFFFFF
    for value in memoryview(data).cast("B"):
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return (~crc) & 0xFFFFFFFF


def decode_block(encoded: bytes) -> Block:
    if len(encoded) < HEADER_SIZE + CRC_SIZE:
        raise ProtocolError("block is too short")
    (
        magic,
        version,
        header_size,
        payload_size,
        status_flags,
        block_sequence,
        first_sample_sequence,
        utc_start_ns,
        monotonic_start_ns,
        sample_count,
        timing_flags,
        nominal_group_rate_millihz,
        sample_size,
        channel_count,
        channel_order,
    ) = _HEADER.unpack_from(encoded)
    if magic != MAGIC:
        raise ProtocolError("invalid block magic")
    if version != VERSION:
        raise ProtocolError(f"unsupported protocol version {version}")
    if header_size != HEADER_SIZE:
        raise ProtocolError("invalid header length")
    if not 1 <= sample_count <= MAX_SAMPLES:
        raise ProtocolError("impossible sample count")
    if first_sample_sequence > (1 << 64) - sample_count:
        raise ProtocolError("sample sequence range overflows")
    if payload_size != sample_count * SAMPLE_SIZE:
        raise ProtocolError("invalid payload length")
    if sample_size != SAMPLE_SIZE or channel_count != 3:
        raise ProtocolError("invalid sample layout")
    if channel_order != 0:
        raise ProtocolError("unsupported channel order")
    if nominal_group_rate_millihz == 0:
        raise ProtocolError("invalid nominal sample rate")
    expected_size = HEADER_SIZE + payload_size + CRC_SIZE
    if len(encoded) != expected_size:
        raise ProtocolError("encoded block length does not match header")
    expected_crc = _U32.unpack_from(encoded, expected_size - CRC_SIZE)[0]
    if crc32c(memoryview(encoded)[:-CRC_SIZE]) != expected_crc:
        raise ProtocolError("CRC32C mismatch")

    samples = np.frombuffer(encoded, dtype=SAMPLE_DTYPE, count=sample_count, offset=HEADER_SIZE)
    counts = samples[["north", "east", "vertical"]]
    for name in counts.dtype.names or ():
        if np.any(counts[name] < -8_388_608) or np.any(counts[name] > 8_388_607):
            raise ProtocolError("sample count exceeds the signed 24-bit range")
    offsets = samples["offset_ns"]
    if len(offsets) > 1 and np.any(offsets[1:] < offsets[:-1]):
        raise ProtocolError("sample offsets are out of order")
    last_offset = int(offsets[-1])
    if (
        utc_start_ns > MAX_SIGNED_TIMESTAMP_NS - last_offset
        or monotonic_start_ns > MAX_SIGNED_TIMESTAMP_NS - last_offset
    ):
        raise ProtocolError("timestamp is outside the client range")
    samples.setflags(write=False)
    return Block(
        header=BlockHeader(
            status_flags=status_flags,
            block_sequence=block_sequence,
            first_sample_sequence=first_sample_sequence,
            utc_start_ns=utc_start_ns,
            monotonic_start_ns=monotonic_start_ns,
            sample_count=sample_count,
            timing_flags=timing_flags,
            nominal_group_rate_millihz=nominal_group_rate_millihz,
            channel_order=channel_order,
        ),
        samples=samples,
        encoded=encoded,
    )


def encode_block(header: BlockHeader, samples: npt.NDArray[np.void]) -> bytes:
    if samples.dtype != SAMPLE_DTYPE:
        raise ProtocolError("samples use the wrong dtype")
    count = len(samples)
    if count != header.sample_count or not 1 <= count <= MAX_SAMPLES:
        raise ProtocolError("invalid sample count")
    if not 0 <= header.first_sample_sequence <= (1 << 64) - count:
        raise ProtocolError("sample sequence range overflows")
    if header.channel_order != 0:
        raise ProtocolError("unsupported channel order")
    if not 1 <= header.nominal_group_rate_millihz < (1 << 32):
        raise ProtocolError("invalid nominal sample rate")
    counts = samples[["north", "east", "vertical"]]
    for name in counts.dtype.names or ():
        if np.any(counts[name] < -8_388_608) or np.any(counts[name] > 8_388_607):
            raise ProtocolError("sample count exceeds the signed 24-bit range")
    offsets = samples["offset_ns"]
    if len(offsets) > 1 and np.any(offsets[1:] < offsets[:-1]):
        raise ProtocolError("sample offsets are out of order")
    last_offset = int(offsets[-1])
    if (
        not 0 <= header.utc_start_ns <= MAX_SIGNED_TIMESTAMP_NS - last_offset
        or not 0 <= header.monotonic_start_ns <= MAX_SIGNED_TIMESTAMP_NS - last_offset
    ):
        raise ProtocolError("timestamp is outside the client range")
    payload_size = count * SAMPLE_SIZE
    output = bytearray(HEADER_SIZE + payload_size + CRC_SIZE)
    _HEADER.pack_into(
        output,
        0,
        MAGIC,
        VERSION,
        HEADER_SIZE,
        payload_size,
        header.status_flags,
        header.block_sequence,
        header.first_sample_sequence,
        header.utc_start_ns,
        header.monotonic_start_ns,
        count,
        header.timing_flags,
        header.nominal_group_rate_millihz,
        SAMPLE_SIZE,
        3,
        header.channel_order,
    )
    output[HEADER_SIZE : HEADER_SIZE + payload_size] = samples.tobytes(order="C")
    _U32.pack_into(output, len(output) - CRC_SIZE, crc32c(memoryview(output)[:-CRC_SIZE]))
    return bytes(output)


class FrameParser:
    """Incremental TCP frame parser with an explicit memory bound."""

    def __init__(self, max_frame_size: int = MAX_FRAME_SIZE) -> None:
        if not HEADER_SIZE + CRC_SIZE <= max_frame_size <= MAX_FRAME_SIZE:
            raise ValueError("max_frame_size is outside protocol bounds")
        self._maximum = max_frame_size
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[Block]:
        self._buffer.extend(data)
        blocks: list[Block] = []
        while len(self._buffer) >= 4:
            length = _U32.unpack_from(self._buffer)[0]
            if length < HEADER_SIZE + CRC_SIZE or length > self._maximum:
                self._buffer.clear()
                raise ProtocolError(f"invalid frame length {length}")
            if len(self._buffer) < 4 + length:
                break
            encoded = bytes(self._buffer[4 : 4 + length])
            del self._buffer[: 4 + length]
            blocks.append(decode_block(encoded))
        return blocks

    def clear(self) -> None:
        self._buffer.clear()
