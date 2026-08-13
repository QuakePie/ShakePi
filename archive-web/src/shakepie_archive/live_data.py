"""Bounded, demand-driven access to the recorder's loopback live stream."""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections import deque
from typing import Any, Final

from shakepie_archive.archive_reader import (
    MAX_BLOCK_SIZE,
    MIN_BLOCK_SIZE,
    Block,
    RecordingError,
    _decode_block,
)
from shakepie_archive.config import LiveConfig

_U32: Final = struct.Struct("<I")
_SAMPLE: Final = struct.Struct("<iiiI")


class LiveFrameParser:
    """Incrementally validate length-prefixed protocol-v1 sample blocks."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[Block]:
        self._buffer.extend(chunk)
        blocks: list[Block] = []
        while len(self._buffer) >= 4:
            frame_length = _U32.unpack_from(self._buffer)[0]
            if not MIN_BLOCK_SIZE <= frame_length <= MAX_BLOCK_SIZE:
                self._buffer.clear()
                raise RecordingError("invalid live frame length")
            if len(self._buffer) < 4 + frame_length:
                break
            encoded = bytes(self._buffer[4 : 4 + frame_length])
            del self._buffer[: 4 + frame_length]
            block, _ = _decode_block(encoded)
            blocks.append(block)
        if len(self._buffer) > MAX_BLOCK_SIZE + 4:
            self._buffer.clear()
            raise RecordingError("live frame buffer exceeded its bound")
        return blocks


class LiveStreamBuffer:
    """Keep a short raw-block history and release the recorder client when idle."""

    def __init__(self, config: LiveConfig) -> None:
        self._config = config
        self._blocks: deque[Block] = deque()
        self._lock = threading.Lock()
        self._demand = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "disabled" if not config.enabled else "idle"
        self._last_error = ""
        self._last_request = 0.0
        self._sample_gaps = 0
        self._last_sample_sequence: int | None = None
        self._sample_rate_millihz: int | None = None

    def _ensure_started(self) -> None:
        if not self._config.enabled:
            return
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="shakepie-web-live-reader",
                daemon=True,
            )
            self._thread.start()

    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._last_error = error[:300]

    def _recently_requested(self) -> bool:
        with self._lock:
            return (
                time.monotonic() - self._last_request
                <= self._config.idle_disconnect_seconds
            )

    def _run(self) -> None:
        while True:
            if not self._recently_requested():
                self._set_state("idle")
                self._demand.wait()
                self._demand.clear()
                continue
            self._set_state("connecting")
            try:
                with socket.create_connection(
                    (self._config.host, self._config.port), timeout=2.0
                ) as connection:
                    connection.settimeout(1.0)
                    parser = LiveFrameParser()
                    self._set_state("connected")
                    while self._recently_requested():
                        try:
                            chunk = connection.recv(64 * 1024)
                        except TimeoutError:
                            continue
                        if not chunk:
                            raise ConnectionError("recorder closed the live stream")
                        for block in parser.feed(chunk):
                            self.append_block(block)
                self._set_state("idle")
            except (OSError, RecordingError) as error:
                self._set_state("unavailable", str(error))
                self._demand.wait(1.0)
                self._demand.clear()

    @staticmethod
    def _block_end_ns(block: Block) -> int:
        offset = _U32.unpack_from(block.payload, (block.sample_count - 1) * 16 + 12)[0]
        return block.utc_start_ns + int(offset)

    def append_block(self, block: Block) -> None:
        """Append one validated block; public to support deterministic protocol tests."""
        last_sequence = block.first_sample_sequence + block.sample_count - 1
        with self._lock:
            restart = (
                self._last_sample_sequence is not None
                and block.first_sample_sequence <= self._last_sample_sequence
            )
            rate_changed = (
                self._sample_rate_millihz is not None
                and block.nominal_group_rate_millihz != self._sample_rate_millihz
            )
            if restart or rate_changed:
                self._blocks.clear()
                self._sample_gaps = 0
                self._last_sample_sequence = None
            if (
                self._last_sample_sequence is not None
                and block.first_sample_sequence > self._last_sample_sequence + 1
            ):
                self._sample_gaps += block.first_sample_sequence - self._last_sample_sequence - 1
            self._blocks.append(block)
            self._last_sample_sequence = last_sequence
            self._sample_rate_millihz = block.nominal_group_rate_millihz
            newest_ns = self._block_end_ns(block)
            cutoff = newest_ns - (self._config.history_seconds + 2) * 1_000_000_000
            while self._blocks and self._block_end_ns(self._blocks[0]) < cutoff:
                self._blocks.popleft()

    def snapshot(
        self,
        after_sequence: int | None = None,
        sample_rate_hz: int | None = None,
    ) -> dict[str, Any]:
        """Return new samples, or a bounded reset snapshot when continuity was lost."""
        with self._lock:
            self._last_request = time.monotonic()
        self._ensure_started()
        self._demand.set()
        with self._lock:
            blocks = list(self._blocks)
            state = self._state
            error = self._last_error
            gaps = self._sample_gaps
            rate_millihz = self._sample_rate_millihz

        reset = after_sequence is None
        if blocks and after_sequence is not None:
            oldest = blocks[0].first_sample_sequence
            newest = blocks[-1].first_sample_sequence + blocks[-1].sample_count - 1
            reset = after_sequence < oldest - 1 or after_sequence > newest
        effective_after = None if reset else after_sequence
        requested_rate_hz = sample_rate_hz or self._config.sample_rate_hz
        if requested_rate_hz <= 0:
            raise ValueError("live sample rate must be positive")
        output_rate_millihz = min(
            requested_rate_hz * 1000,
            rate_millihz or requested_rate_hz * 1000,
        )
        downsample = rate_millihz is not None and output_rate_millihz < rate_millihz

        def time_bucket(timestamp_ns: int) -> int:
            return timestamp_ns * output_rate_millihz // 1_000_000_000_000

        last_bucket: int | None = None
        if downsample and effective_after is not None:
            for block in blocks:
                block_last = block.first_sample_sequence + block.sample_count - 1
                if block.first_sample_sequence <= effective_after <= block_last:
                    sample_index = effective_after - block.first_sample_sequence
                    offset_ns = _U32.unpack_from(block.payload, sample_index * 16 + 12)[0]
                    last_bucket = time_bucket(block.utc_start_ns + int(offset_ns))
                    break
        timestamps_ms: list[float] = []
        sequences: list[int] = []
        north: list[int] = []
        east: list[int] = []
        vertical: list[int] = []
        for block in blocks:
            last_sequence = block.first_sample_sequence + block.sample_count - 1
            if effective_after is not None and last_sequence <= effective_after:
                continue
            for index, (n_value, e_value, z_value, offset_ns) in enumerate(
                _SAMPLE.iter_unpack(block.payload)
            ):
                sequence = block.first_sample_sequence + index
                if effective_after is not None and sequence <= effective_after:
                    continue
                timestamp_ns = block.utc_start_ns + offset_ns
                if downsample:
                    bucket = time_bucket(timestamp_ns)
                    if bucket == last_bucket:
                        continue
                    last_bucket = bucket
                timestamps_ms.append(timestamp_ns / 1_000_000.0)
                sequences.append(sequence)
                north.append(n_value)
                east.append(e_value)
                vertical.append(z_value)

        latest_ns = self._block_end_ns(blocks[-1]) if blocks else None
        age_ms = None if latest_ns is None else max(0.0, (time.time_ns() - latest_ns) / 1e6)
        return {
            "state": state,
            "error": error,
            "reset": reset,
            "history_seconds": self._config.history_seconds,
            "refresh_milliseconds": self._config.refresh_milliseconds,
            "sample_rate_hz": output_rate_millihz / 1000.0,
            "source_sample_rate_hz": (
                None if rate_millihz is None else rate_millihz / 1000.0
            ),
            "sample_gaps": gaps,
            "age_ms": age_ms,
            "timestamps_ms": timestamps_ms,
            "sequences": sequences,
            "north": north,
            "east": east,
            "vertical": vertical,
        }
