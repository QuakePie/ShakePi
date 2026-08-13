"""Reconnect-capable background TCP receiver."""

from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO

from .protocol import Block, FrameParser, ProtocolError


@dataclass(frozen=True, slots=True)
class ReceiverStatus:
    state: str = "stopped"
    reconnects: int = 0
    recorder_restarts: int = 0
    blocks: int = 0
    block_gaps: int = 0
    sample_gaps: int = 0
    crc_errors: int = 0
    protocol_errors: int = 0
    queue_drops: int = 0
    latency_ms: float = 0.0
    effective_rate_hz: float = 0.0
    status_flags: int = 0
    timing_flags: int = 0
    last_error: str = ""


class Receiver(threading.Thread):
    def __init__(
        self,
        host: str,
        port: int,
        output: queue.Queue[Block],
        stop_event: threading.Event,
        *,
        timeout_seconds: float = 2.0,
        reconnect_initial_seconds: float = 0.25,
        reconnect_max_seconds: float = 10.0,
        capture_path: Path | None = None,
    ) -> None:
        super().__init__(name="shakepie-receiver", daemon=False)
        self._host = host
        self._port = port
        self._output = output
        self._stop_event = stop_event
        self._timeout = timeout_seconds
        self._initial_backoff = reconnect_initial_seconds
        self._maximum_backoff = reconnect_max_seconds
        self._capture_path = capture_path
        self._capture_failed = False
        self._capture_error = ""
        self._status = ReceiverStatus()
        self._status_lock = threading.Lock()
        self._socket_lock = threading.Lock()
        self._socket: socket.socket | None = None
        # Continuity state deliberately survives TCP reconnects. Otherwise a
        # transport outage could conceal exactly the data loss operators need
        # the client to report.
        self._previous_block: int | None = None
        self._previous_sample: int | None = None
        self._previous_utc_ns: int | None = None
        self._previous_monotonic_ns: int | None = None
        self._previous_sample_monotonic_ns: int | None = None

    def status(self) -> ReceiverStatus:
        with self._status_lock:
            return self._status

    def _update(self, **changes: Any) -> None:
        with self._status_lock:
            self._status = replace(self._status, **changes)

    def stop(self) -> None:
        self._stop_event.set()
        with self._socket_lock:
            if self._socket is not None:
                with suppress(OSError):
                    self._socket.shutdown(socket.SHUT_RDWR)

    def run(self) -> None:
        capture: BinaryIO | None = None
        try:
            if self._capture_path is not None:
                try:
                    self._capture_path.parent.mkdir(parents=True, exist_ok=True)
                    capture = self._capture_path.open("ab", buffering=0)
                except OSError as error:
                    self._capture_failed = True
                    self._capture_error = f"capture disabled: {error}"
                    self._update(last_error=self._capture_error)
            backoff = self._initial_backoff
            while not self._stop_event.is_set():
                self._update(state="connecting")
                try:
                    with socket.create_connection(
                        (self._host, self._port), timeout=self._timeout
                    ) as connection:
                        connection.settimeout(self._timeout)
                        with self._socket_lock:
                            self._socket = connection
                        self._update(state="connected", last_error=self._capture_error)
                        backoff = self._initial_backoff
                        self._receive_connection(
                            connection, None if self._capture_failed else capture
                        )
                except (OSError, ProtocolError) as error:
                    current = self.status()
                    changes: dict[str, Any] = {"state": "disconnected", "last_error": str(error)}
                    if isinstance(error, ProtocolError):
                        key = "crc_errors" if "CRC32C" in str(error) else "protocol_errors"
                        changes[key] = getattr(current, key) + 1
                    self._update(**changes)
                finally:
                    with self._socket_lock:
                        self._socket = None
                if self._stop_event.wait(backoff):
                    break
                current = self.status()
                self._update(reconnects=current.reconnects + 1)
                backoff = min(self._maximum_backoff, max(self._initial_backoff, backoff * 2.0))
        finally:
            if capture is not None:
                with suppress(OSError):
                    capture.close()
            self._update(state="stopped")

    def _receive_connection(self, connection: socket.socket, capture: BinaryIO | None) -> None:
        parser = FrameParser()
        first_block_on_connection = True
        rate_started = time.monotonic()
        rate_samples = 0
        while not self._stop_event.is_set():
            try:
                data = connection.recv(64 * 1024)
            except TimeoutError:
                continue
            if not data:
                raise ConnectionError("stream closed")
            for block in parser.feed(data):
                current = self.status()
                block_gaps = current.block_gaps
                sample_gaps = current.sample_gaps
                previous_block = self._previous_block
                previous_sample = self._previous_sample
                previous_sample_monotonic = self._previous_sample_monotonic_ns
                sequence_reset = (
                    first_block_on_connection
                    and previous_block is not None
                    and previous_sample is not None
                    and block.header.block_sequence <= previous_block
                    and block.header.first_sample_sequence <= previous_sample
                    and (
                        block.header.utc_start_ns != self._previous_utc_ns
                        or block.header.monotonic_start_ns != self._previous_monotonic_ns
                    )
                )
                if sequence_reset:
                    self._update(recorder_restarts=current.recorder_restarts + 1)
                    current = self.status()
                    previous_block = None
                    previous_sample = None
                    previous_sample_monotonic = None
                if previous_block is not None and block.header.block_sequence <= previous_block:
                    raise ProtocolError("out-of-order block sequence")
                if (
                    previous_sample is not None
                    and block.header.first_sample_sequence <= previous_sample
                ):
                    raise ProtocolError("out-of-order sample sequence")
                first_sample_monotonic = block.header.monotonic_start_ns + int(
                    block.samples["offset_ns"][0]
                )
                last_sample_monotonic = block.header.monotonic_start_ns + int(
                    block.samples["offset_ns"][-1]
                )
                if (
                    previous_sample_monotonic is not None
                    and first_sample_monotonic <= previous_sample_monotonic
                ):
                    raise ProtocolError("non-increasing monotonic sample time")
                if previous_block is not None and block.header.block_sequence > previous_block + 1:
                    block_gaps += block.header.block_sequence - previous_block - 1
                if (
                    previous_sample is not None
                    and block.header.first_sample_sequence > previous_sample + 1
                ):
                    sample_gaps += block.header.first_sample_sequence - previous_sample - 1
                self._previous_block = block.header.block_sequence
                self._previous_sample = (
                    block.header.first_sample_sequence + block.header.sample_count - 1
                )
                self._previous_utc_ns = block.header.utc_start_ns
                self._previous_monotonic_ns = block.header.monotonic_start_ns
                self._previous_sample_monotonic_ns = last_sample_monotonic
                first_block_on_connection = False
                if capture is not None:
                    try:
                        capture.write(struct.pack("<I", len(block.encoded)) + block.encoded)
                    except OSError as error:
                        self._capture_failed = True
                        self._capture_error = f"capture disabled: {error}"
                        self._update(last_error=self._capture_error)
                        capture = None
                try:
                    self._output.put_nowait(block)
                except queue.Full:
                    with suppress(queue.Empty):
                        self._output.get_nowait()
                    self._output.put_nowait(block)
                    current = self.status()
                    self._update(queue_drops=current.queue_drops + 1)
                rate_samples += block.header.sample_count
                elapsed = time.monotonic() - rate_started
                effective_rate = (
                    rate_samples / elapsed if elapsed > 0.25 else current.effective_rate_hz
                )
                if elapsed > 2.0:
                    rate_samples = 0
                    rate_started = time.monotonic()
                latency_ms = (time.time_ns() - block.header.utc_start_ns) / 1_000_000.0
                self._update(
                    blocks=current.blocks + 1,
                    block_gaps=block_gaps,
                    sample_gaps=sample_gaps,
                    latency_ms=latency_ms,
                    effective_rate_hz=effective_rate,
                    status_flags=block.header.status_flags,
                    timing_flags=block.header.timing_flags,
                )
