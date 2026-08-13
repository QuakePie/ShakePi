"""Minimal miniSEED 2.4 writer for ShakePie recordings (no external dependencies).

One purpose: hand finalized recordings to the wider seismology ecosystem (ObsPy,
Swarm, jAmaSeis, SeisComP) in the interchange format every tool reads. The output
uses 512-byte records with INT32 samples (encoding 3), so counts are exported
losslessly — no STEIM compression, at roughly 2x the size of the raw archive.

Headers and samples are written little-endian and blockette 1000 declares byte
order 0, which libmseed-based readers (ObsPy, Swarm) accept directly. Channels
are named EHN/EHE/EHZ (80-250 Hz band per the SEED convention).
"""

from __future__ import annotations

import array
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from shakepie_archive.archive_reader import _SAMPLE, Block
from shakepie_archive.constants import NOMINAL_SAMPLE_RATE_HZ

RECORD_LENGTH = 512
_HEADER_AND_BLOCKETTE = 64
_INT32_BYTES = 4
SAMPLES_PER_RECORD = (RECORD_LENGTH - _HEADER_AND_BLOCKETTE) // _INT32_BYTES  # 112

DEFAULT_NETWORK = "SP"
CHANNEL_CODES = {"north": "EHN", "east": "EHE", "vertical": "EHZ"}
_CHANNEL_INDICES = {"north": 0, "east": 1, "vertical": 2}


class SeriesCollector:
    """Accumulates channel samples from recording blocks, splitting at discontinuities.

    A sample-sequence gap or a clock-step timing flag closes the current series and
    starts a new one with its own start time, so exported records carry truthful
    timing. Shared by the web export endpoint and the command-line exporter.
    """

    def __init__(self, channels: Sequence[str]) -> None:
        self._channels = tuple(channels)
        self.series: dict[str, list[tuple[int, int, array.array[int]]]] = {
            name: [] for name in self._channels
        }
        self._current: dict[str, array.array[int]] = {}
        self._start_ns = 0
        self._rate_hz = 200
        self._expected_next: int | None = None

    def _flush(self) -> None:
        for name, values in self._current.items():
            if len(values):
                self.series[name].append((self._start_ns, self._rate_hz, values))
        self._current.clear()

    def add_block(self, block: Block) -> None:
        if (
            self._expected_next is not None
            and block.first_sample_sequence != self._expected_next
        ) or block.timing_flags & 1:
            self._flush()
        if not self._current:
            self._start_ns = block.utc_start_ns
            self._rate_hz = nominal_rate_hz(block.nominal_group_rate_millihz)
            for name in self._channels:
                self._current[name] = array.array("i")
        for group in _SAMPLE.iter_unpack(block.payload):
            for name in self._channels:
                self._current[name].append(group[_CHANNEL_INDICES[name]])
        self._expected_next = block.first_sample_sequence + block.sample_count

    def finish(self) -> dict[str, list[tuple[int, int, array.array[int]]]]:
        """Close the open series and return {channel: [(start_ns, rate_hz, values), ...]}."""
        self._flush()
        return self.series


def encode_all(
    series: Mapping[str, Sequence[tuple[int, int, Sequence[int]]]],
    station: str,
    first_sequence_number: int = 1,
) -> bytes:
    """Encode collected series into one miniSEED volume with continuous numbering."""
    buffer = bytearray()
    sequence_number = first_sequence_number
    for channel, chunks in series.items():
        code = CHANNEL_CODES[channel]
        for start_ns, rate_hz, values in chunks:
            buffer.extend(
                encode_series(
                    start_ns,
                    rate_hz,
                    values,
                    station,
                    code,
                    first_sequence_number=sequence_number,
                )
            )
            sequence_number += -(-len(values) // SAMPLES_PER_RECORD)
    return bytes(buffer)


def _start_time_fields(start_ns: int) -> tuple[int, int, int, int, int, int]:
    """Split a nanosecond UTC timestamp into miniSEED's (year, day, h, m, s, 0.1 ms)."""
    moment = datetime.fromtimestamp(start_ns / 1e9, UTC)
    tenth_millisecond = (start_ns % 1_000_000_000) // 100_000
    return (
        moment.year,
        moment.timetuple().tm_yday,
        moment.hour,
        moment.minute,
        moment.second,
        tenth_millisecond,
    )


def encode_record(
    sequence_number: int,
    station: str,
    network: str,
    channel: str,
    start_ns: int,
    sample_rate_hz: int,
    samples: Sequence[int],
) -> bytes:
    """Build one 512-byte miniSEED record holding up to SAMPLES_PER_RECORD samples.

    Short final chunks are zero-padded to the fixed record length; the declared
    sample count tells readers how many values are real.
    """
    if not 1 <= len(samples) <= SAMPLES_PER_RECORD:
        raise ValueError(f"a record carries 1..{SAMPLES_PER_RECORD} samples")
    rate = max(1, sample_rate_hz)
    if rate > 32_767:
        raise ValueError("sample rate exceeds the miniSEED integer encoding")
    buffer = bytearray(RECORD_LENGTH)
    buffer[0:6] = f"{sequence_number % 1_000_000:06d}".encode("ascii")
    buffer[6:7] = b"D"  # data record
    buffer[7:8] = b" "
    buffer[8:13] = station.upper()[:5].ljust(5).encode("ascii")
    buffer[13:15] = b"  "  # empty location code
    buffer[15:18] = channel.upper()[:3].encode("ascii")
    buffer[18:20] = network.upper()[:2].encode("ascii")
    year, day_of_year, hour, minute, second, tenth_ms = _start_time_fields(start_ns)
    # Fixed data header (offsets per the SEED 2.4 manual), little-endian throughout.
    buffer[20:22] = year.to_bytes(2, "little")
    buffer[22:24] = day_of_year.to_bytes(2, "little")
    buffer[24] = hour
    buffer[25] = minute
    buffer[26] = second
    buffer[27] = 0  # unused
    buffer[28:30] = tenth_ms.to_bytes(2, "little")
    buffer[30:32] = len(samples).to_bytes(2, "little")
    buffer[32:34] = rate.to_bytes(2, "little", signed=True)  # sample rate factor
    buffer[34:36] = (1).to_bytes(2, "little", signed=True)  # sample rate multiplier
    buffer[36] = 0  # activity flags
    buffer[37] = 0  # I/O flags
    buffer[38] = 0  # data quality flags
    buffer[39] = 1  # exactly one blockette follows
    buffer[40:44] = (0).to_bytes(4, "little", signed=True)  # time correction
    buffer[44:46] = _HEADER_AND_BLOCKETTE.to_bytes(2, "little")  # data start
    buffer[46:48] = (48).to_bytes(2, "little")  # first blockette
    # Blockette 1000: encoding 3 (INT32), byte order 0 (little), 2^9-byte records.
    buffer[48:50] = (1000).to_bytes(2, "little")
    buffer[50:52] = (0).to_bytes(2, "little")  # no further blockettes
    buffer[52] = 3
    buffer[53] = 0
    buffer[54] = 9
    buffer[55] = 0
    for index, value in enumerate(samples):
        begin = _HEADER_AND_BLOCKETTE + index * _INT32_BYTES
        buffer[begin : begin + _INT32_BYTES] = int(value).to_bytes(4, "little", signed=True)
    return bytes(buffer)


def encode_series(
    start_ns: int,
    sample_rate_hz: int,
    values: Sequence[int],
    station: str,
    channel: str,
    network: str = DEFAULT_NETWORK,
    first_sequence_number: int = 1,
) -> bytes:
    """Encode one gap-free sample series as consecutive miniSEED records."""
    rate = max(1, sample_rate_hz)
    chunks: list[bytes] = []
    sequence_number = first_sequence_number
    for offset in range(0, len(values), SAMPLES_PER_RECORD):
        chunk = values[offset : offset + SAMPLES_PER_RECORD]
        chunk_start_ns = start_ns + round(offset * 1_000_000_000 / rate)
        chunks.append(
            encode_record(
                sequence_number,
                station,
                network,
                channel,
                chunk_start_ns,
                rate,
                chunk,
            )
        )
        sequence_number += 1
    return b"".join(chunks)


def nominal_rate_hz(sample_rate_millihz: int | None) -> int:
    """The recording's nominal group rate as whole Hz for the miniSEED headers."""
    rate = round((sample_rate_millihz or NOMINAL_SAMPLE_RATE_HZ * 1000) / 1000.0)
    return max(1, min(rate, 32_767))
