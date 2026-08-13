"""Bounded sequential validation of finalized ShakePie v1 recordings."""

from __future__ import annotations

import os
import stat
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

import google_crc32c

FILE_HEADER_SIZE: Final = 128
FILE_FOOTER_SIZE: Final = 64
BLOCK_HEADER_SIZE: Final = 64
SAMPLE_SIZE: Final = 16
BLOCK_CRC_SIZE: Final = 4
MAX_SAMPLES: Final = 256
MIN_BLOCK_SIZE: Final = BLOCK_HEADER_SIZE + SAMPLE_SIZE + BLOCK_CRC_SIZE
MAX_BLOCK_SIZE: Final = BLOCK_HEADER_SIZE + MAX_SAMPLES * SAMPLE_SIZE + BLOCK_CRC_SIZE
MAX_TIMESTAMP_NS: Final = (1 << 63) - 1

_FILE_HEADER = struct.Struct("<4sHHHHHBBHHIIIQQQQ32s28sI")
_BLOCK_HEADER = struct.Struct("<4sHHIIQQQQIIIHBB")
_FOOTER = struct.Struct("<4sHHQQQQQQII")
_SAMPLE = struct.Struct("<iiiI")
_U32 = struct.Struct("<I")


class RecordingError(ValueError):
    """Raised when an authoritative recording fails structural validation."""


@dataclass(frozen=True, slots=True)
class FileHeader:
    pga_gain: int
    adc_data_rate_millihz: int
    reference_voltage_uv: int
    samples_per_block: int
    created_utc_ns: int
    created_monotonic_ns: int
    first_block_sequence: int
    first_sample_sequence: int
    station_id: str


@dataclass(frozen=True, slots=True)
class Block:
    block_sequence: int
    first_sample_sequence: int
    utc_start_ns: int
    sample_count: int
    nominal_group_rate_millihz: int
    timing_flags: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class RecordingMetadata:
    path: Path
    station_id: str
    start_ns: int
    end_ns: int
    sample_rate_millihz: int
    sample_count: int
    block_count: int
    sequence_gap_count: int
    recovered: bool
    size_bytes: int
    mtime_ns: int
    device: int
    inode: int


def finalized_name(name: str) -> bool:
    return name.endswith(".spb") and not name.endswith((".partial.spb", ".partial.empty.spb"))


def _crc32c(data: bytes | memoryview) -> int:
    # The accelerator requires an immutable buffer. Records are at most 4,164 bytes,
    # so this bounded conversion never scales with a complete recording.
    return int(google_crc32c.value(bytes(data)))


def _read_exact(handle: BinaryIO, size: int, label: str) -> bytes:
    value = handle.read(size)
    if len(value) != size:
        raise RecordingError(f"truncated {label}")
    return value


def _decode_header(encoded: bytes) -> FileHeader:
    if len(encoded) != FILE_HEADER_SIZE:
        raise RecordingError("file header has the wrong size")
    fields = _FILE_HEADER.unpack(encoded)
    (
        magic,
        version,
        header_size,
        protocol_version,
        block_header_size,
        sample_size,
        channel_count,
        channel_order,
        pga_gain,
        file_flags,
        adc_rate,
        reference_voltage,
        samples_per_block,
        created_utc,
        created_monotonic,
        first_block,
        first_sample,
        station_raw,
        reserved,
        expected_crc,
    ) = fields
    if magic != b"SPFH" or version != 1:
        raise RecordingError("invalid file header magic or version")
    if (
        header_size != FILE_HEADER_SIZE
        or protocol_version != 1
        or block_header_size != BLOCK_HEADER_SIZE
        or sample_size != SAMPLE_SIZE
        or channel_count != 3
        or channel_order != 0
    ):
        raise RecordingError("invalid file header layout")
    if _crc32c(memoryview(encoded)[:124]) != expected_crc:
        raise RecordingError("file header CRC32C mismatch")
    station_end = station_raw.find(b"\0")
    if station_end <= 0 or any(station_raw[station_end:]) or any(reserved):
        raise RecordingError("invalid station text or nonzero reserved bytes")
    try:
        station = station_raw[:station_end].decode("ascii")
    except UnicodeDecodeError as error:
        raise RecordingError("station ID is not ASCII") from error
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in station):
        raise RecordingError("station ID contains non-printable text")
    if (
        pga_gain not in {1, 2, 4, 8, 16, 32, 64}
        or file_flags != 0
        or adc_rate == 0
        or reference_voltage == 0
        or not 1 <= samples_per_block <= MAX_SAMPLES
    ):
        raise RecordingError("invalid file header values")
    return FileHeader(
        pga_gain,
        adc_rate,
        reference_voltage,
        samples_per_block,
        created_utc,
        created_monotonic,
        first_block,
        first_sample,
        station,
    )


def _decode_block(encoded: bytes) -> tuple[Block, int]:
    if not MIN_BLOCK_SIZE <= len(encoded) <= MAX_BLOCK_SIZE:
        raise RecordingError("invalid block length")
    fields = _BLOCK_HEADER.unpack_from(encoded)
    (
        magic,
        version,
        header_size,
        payload_size,
        _status_flags,
        block_sequence,
        first_sample_sequence,
        utc_start_ns,
        monotonic_start_ns,
        sample_count,
        timing_flags,
        nominal_rate,
        sample_size,
        channel_count,
        channel_order,
    ) = fields
    if magic != b"SPBK" or version != 1 or header_size != BLOCK_HEADER_SIZE:
        raise RecordingError("invalid block header")
    if (
        not 1 <= sample_count <= MAX_SAMPLES
        or payload_size != sample_count * SAMPLE_SIZE
        or len(encoded) != BLOCK_HEADER_SIZE + payload_size + BLOCK_CRC_SIZE
        or sample_size != SAMPLE_SIZE
        or channel_count != 3
        or channel_order != 0
        or nominal_rate == 0
    ):
        raise RecordingError("invalid block layout")
    if first_sample_sequence > (1 << 64) - sample_count:
        raise RecordingError("sample sequence range overflows")
    expected_crc = _U32.unpack_from(encoded, len(encoded) - BLOCK_CRC_SIZE)[0]
    if _crc32c(memoryview(encoded)[:-BLOCK_CRC_SIZE]) != expected_crc:
        raise RecordingError("block CRC32C mismatch")
    payload = encoded[BLOCK_HEADER_SIZE:-BLOCK_CRC_SIZE]
    previous_offset = -1
    last_offset = 0
    for north, east, vertical, offset in _SAMPLE.iter_unpack(payload):
        if any(value < -8_388_608 or value > 8_388_607 for value in (north, east, vertical)):
            raise RecordingError("sample count is outside the signed 24-bit range")
        if offset < previous_offset:
            raise RecordingError("sample offsets are out of order")
        previous_offset = offset
        last_offset = offset
    if (
        utc_start_ns > MAX_TIMESTAMP_NS - last_offset
        or monotonic_start_ns > MAX_TIMESTAMP_NS - last_offset
    ):
        raise RecordingError("sample timestamp is outside the supported range")
    return (
        Block(
            block_sequence,
            first_sample_sequence,
            utc_start_ns,
            sample_count,
            nominal_rate,
            timing_flags,
            payload,
        ),
        last_offset,
    )


def _decode_footer(encoded: bytes) -> tuple[int, int, int, int, int, int, int]:
    fields = _FOOTER.unpack(encoded)
    (
        magic,
        version,
        size,
        blocks,
        samples,
        first_block,
        last_block,
        first_sample,
        last_sample,
        flags,
        crc,
    ) = fields
    if magic != b"SPFT" or version != 1 or size != FILE_FOOTER_SIZE:
        raise RecordingError("invalid file footer")
    if _crc32c(memoryview(encoded)[:60]) != crc:
        raise RecordingError("file footer CRC32C mismatch")
    if (
        blocks == 0
        or samples == 0
        or last_block < first_block
        or last_sample < first_sample
        or last_block - first_block != blocks - 1
        or last_sample - first_sample < samples - 1
        or flags & ~1
    ):
        raise RecordingError("inconsistent file footer")
    return blocks, samples, first_block, last_block, first_sample, last_sample, flags


def read_recording(
    path: Path, on_block: Callable[[Block], None] | None = None
) -> RecordingMetadata:
    """Validate one finalized recording without retaining more than one block."""
    if not finalized_name(path.name):
        raise RecordingError("recording is not finalized")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RecordingError(f"cannot open recording: {error}") from error
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode):
            raise RecordingError("recording is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            header = _decode_header(_read_exact(handle, FILE_HEADER_SIZE, "file header"))
            blocks = 0
            samples = 0
            gaps = 0
            previous_block = -1
            previous_sample = -1
            start_ns = -1
            end_ns = -1
            sample_rate = 0
            footer_fields: tuple[int, int, int, int, int, int, int] | None = None
            while True:
                prefix = handle.read(4)
                if prefix == b"SPFT":
                    footer_fields = _decode_footer(
                        prefix + _read_exact(handle, FILE_FOOTER_SIZE - 4, "file footer")
                    )
                    if handle.read(1):
                        raise RecordingError("trailing bytes after file footer")
                    break
                if len(prefix) != 4:
                    raise RecordingError("recording has no complete footer")
                length = _U32.unpack(prefix)[0]
                block, last_offset = _decode_block(_read_exact(handle, length, "sample block"))
                if blocks == 0:
                    if (
                        block.block_sequence != header.first_block_sequence
                        or block.first_sample_sequence != header.first_sample_sequence
                    ):
                        raise RecordingError("first block does not match the file header")
                    start_ns = block.utc_start_ns
                    sample_rate = block.nominal_group_rate_millihz
                else:
                    if block.block_sequence != previous_block + 1:
                        raise RecordingError("block sequence is not contiguous")
                    if block.first_sample_sequence <= previous_sample:
                        raise RecordingError("sample sequence is not increasing")
                    gaps += block.first_sample_sequence - previous_sample - 1
                    if block.nominal_group_rate_millihz != sample_rate:
                        raise RecordingError("nominal sample rate changed inside the recording")
                previous_block = block.block_sequence
                previous_sample = block.first_sample_sequence + block.sample_count - 1
                end_ns = block.utc_start_ns + last_offset
                blocks += 1
                samples += block.sample_count
                if on_block is not None:
                    on_block(block)
            assert footer_fields is not None
            (
                footer_blocks,
                footer_samples,
                first_block,
                last_block,
                first_sample,
                last_sample,
                flags_value,
            ) = footer_fields
            if (
                blocks != footer_blocks
                or samples != footer_samples
                or first_block != header.first_block_sequence
                or first_sample != header.first_sample_sequence
                or last_block != previous_block
                or last_sample != previous_sample
            ):
                raise RecordingError("file footer does not match sample blocks")
            if start_ns < 0 or end_ns < start_ns:
                raise RecordingError("recording contains no usable timestamps")
            period_ns = max(1, round(1_000_000_000_000 / sample_rate))
            return RecordingMetadata(
                path=path,
                station_id=header.station_id,
                start_ns=start_ns,
                end_ns=end_ns + period_ns,
                sample_rate_millihz=sample_rate,
                sample_count=samples,
                block_count=blocks,
                sequence_gap_count=gaps,
                recovered=bool(flags_value & 1) or path.name.endswith(".recovered.spb"),
                size_bytes=information.st_size,
                mtime_ns=information.st_mtime_ns,
                device=information.st_dev,
                inode=information.st_ino,
            )
    finally:
        os.close(descriptor)
