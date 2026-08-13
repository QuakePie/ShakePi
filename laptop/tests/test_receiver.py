from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from io import BytesIO
from pathlib import Path

import pytest

from shakepie_client.protocol import (
    SAMPLE_DTYPE,
    Block,
    BlockHeader,
    ProtocolError,
    decode_block,
    encode_block,
)
from shakepie_client.receiver import Receiver

FIXTURE = Path(__file__).parents[2] / "protocol" / "fixtures" / "golden-block-v1.spb"


class FailingCapture(BytesIO):
    def write(self, data: bytes) -> int:
        raise OSError("capture disk full")


def block_after(source: Block, block_sequence: int, sample_sequence: int) -> bytes:
    header = BlockHeader(
        source.header.status_flags,
        block_sequence,
        sample_sequence,
        source.header.utc_start_ns
        + (sample_sequence - source.header.first_sample_sequence) * 1_000_000,
        source.header.monotonic_start_ns
        + (sample_sequence - source.header.first_sample_sequence) * 1_000_000,
        source.header.sample_count,
        source.header.timing_flags,
        source.header.nominal_group_rate_millihz,
        source.header.channel_order,
    )
    return encode_block(header, source.samples.astype(SAMPLE_DTYPE, copy=True))


def test_receiver_handles_partial_network_chunks() -> None:
    left, right = socket.socketpair()
    output: queue.Queue[Block] = queue.Queue(maxsize=2)
    stop = threading.Event()
    receiver = Receiver("unused", 0, output, stop)
    encoded = FIXTURE.read_bytes()
    framed = struct.pack("<I", len(encoded)) + encoded

    def send() -> None:
        right.sendall(framed[:3])
        right.sendall(framed[3:29])
        right.sendall(framed[29:])
        right.close()

    sender = threading.Thread(target=send)
    sender.start()
    with pytest.raises(ConnectionError):
        receiver._receive_connection(left, None)
    sender.join()
    left.close()
    assert output.get_nowait().header.block_sequence == 42


def test_capture_failure_does_not_stop_live_delivery() -> None:
    left, right = socket.socketpair()
    output: queue.Queue[Block] = queue.Queue(maxsize=2)
    stop = threading.Event()
    receiver = Receiver("unused", 0, output, stop)
    encoded = FIXTURE.read_bytes()
    right.sendall(struct.pack("<I", len(encoded)) + encoded)
    right.close()
    with pytest.raises(ConnectionError):
        receiver._receive_connection(left, FailingCapture())
    left.close()
    assert output.get_nowait().header.block_sequence == 42
    assert "capture disabled" in receiver.status().last_error


def test_out_of_order_sequence_is_rejected() -> None:
    left, right = socket.socketpair()
    output: queue.Queue[Block] = queue.Queue(maxsize=2)
    stop = threading.Event()
    receiver = Receiver("unused", 0, output, stop)
    encoded = FIXTURE.read_bytes()
    framed = struct.pack("<I", len(encoded)) + encoded
    right.sendall(framed + framed)
    with pytest.raises(ProtocolError, match="out-of-order"):
        receiver._receive_connection(left, None)
    left.close()
    right.close()


def test_non_increasing_monotonic_sample_time_is_rejected() -> None:
    left, right = socket.socketpair()
    output: queue.Queue[Block] = queue.Queue(maxsize=2)
    stop = threading.Event()
    receiver = Receiver("unused", 0, output, stop)
    first = decode_block(FIXTURE.read_bytes())
    second_header = BlockHeader(
        first.header.status_flags,
        first.header.block_sequence + 1,
        first.header.first_sample_sequence + first.header.sample_count,
        first.header.utc_start_ns + 4_000_000,
        first.header.monotonic_start_ns - 1,
        first.header.sample_count,
        first.header.timing_flags,
        first.header.nominal_group_rate_millihz,
        first.header.channel_order,
    )
    second = encode_block(second_header, first.samples.astype(SAMPLE_DTYPE, copy=True))
    first_framed = struct.pack("<I", len(first.encoded)) + first.encoded
    second_framed = struct.pack("<I", len(second)) + second
    right.sendall(first_framed + second_framed)
    with pytest.raises(ProtocolError, match="monotonic"):
        receiver._receive_connection(left, None)
    left.close()
    right.close()


def test_receiver_queue_drops_oldest_complete_block() -> None:
    left, right = socket.socketpair()
    output: queue.Queue[Block] = queue.Queue(maxsize=1)
    stop = threading.Event()
    receiver = Receiver("unused", 0, output, stop)
    encoded = FIXTURE.read_bytes()
    first = decode_block(encoded)
    next_header = BlockHeader(
        first.header.status_flags,
        43,
        1_003,
        first.header.utc_start_ns + 4_000_000,
        first.header.monotonic_start_ns + 4_000_000,
        first.header.sample_count,
        first.header.timing_flags,
        first.header.nominal_group_rate_millihz,
        first.header.channel_order,
    )
    samples = first.samples.astype(SAMPLE_DTYPE, copy=True)
    second = encode_block(next_header, samples)
    framed = struct.pack("<I", len(encoded)) + encoded
    next_framed = struct.pack("<I", len(second)) + second
    right.sendall(framed + next_framed)
    right.close()
    with pytest.raises(ConnectionError):
        receiver._receive_connection(left, None)
    left.close()
    assert output.qsize() == 1
    assert output.get_nowait().header.block_sequence == 43
    assert receiver.status().queue_drops == 1


def test_receiver_reconnect_preserves_gap_detection() -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = int(listener.getsockname()[1])
    first = decode_block(FIXTURE.read_bytes())
    second = block_after(first, 44, 1_006)
    stop = threading.Event()
    output: queue.Queue[Block] = queue.Queue(maxsize=4)

    def serve() -> None:
        for encoded in (first.encoded, second):
            connection, _ = listener.accept()
            with connection:
                connection.sendall(struct.pack("<I", len(encoded)) + encoded)
                if encoded is second:
                    stop.wait(2.0)
        listener.close()

    server = threading.Thread(target=serve)
    server.start()
    receiver = Receiver(
        "127.0.0.1",
        port,
        output,
        stop,
        timeout_seconds=0.1,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.02,
    )
    receiver.start()
    deadline = time.monotonic() + 3.0
    while output.qsize() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    receiver.stop()
    receiver.join(timeout=2.0)
    server.join(timeout=2.0)

    assert not receiver.is_alive()
    assert not server.is_alive()
    assert [output.get_nowait().header.block_sequence for _ in range(2)] == [42, 44]
    assert receiver.status().reconnects >= 1
    assert receiver.status().block_gaps == 1
    assert receiver.status().sample_gaps == 3


def test_receiver_accepts_explicit_sequence_reset_only_on_new_connection() -> None:
    output: queue.Queue[Block] = queue.Queue(maxsize=4)
    stop = threading.Event()
    receiver = Receiver("unused", 0, output, stop)
    first = decode_block(FIXTURE.read_bytes())

    for encoded in (first.encoded, block_after(first, 0, 0)):
        left, right = socket.socketpair()
        right.sendall(struct.pack("<I", len(encoded)) + encoded)
        right.close()
        with pytest.raises(ConnectionError):
            receiver._receive_connection(left, None)
        left.close()

    assert receiver.status().recorder_restarts == 1
    assert receiver.status().block_gaps == 0
    assert receiver.status().sample_gaps == 0
