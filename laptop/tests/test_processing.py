from __future__ import annotations

import queue
import threading

import numpy as np

from shakepie_client.config import ClientConfig
from shakepie_client.processing import ProcessedSnapshot, ProcessingWorker
from shakepie_client.protocol import SAMPLE_DTYPE, Block, BlockHeader, decode_block, encode_block


def make_block(sequence: int) -> Block:
    samples = np.zeros(64, dtype=SAMPLE_DTYPE)
    samples["north"] = np.arange(64)
    samples["east"] = np.arange(64) * 2
    samples["vertical"] = np.arange(64) * -1
    samples["offset_ns"] = np.arange(64, dtype=np.uint32) * 1_000_000
    header = BlockHeader(
        0,
        sequence,
        sequence * 64,
        1_700_000_000_000_000_000 + sequence * 64_000_000,
        sequence * 64_000_000,
        64,
        0,
        1_000_000,
        0,
    )
    return decode_block(encode_block(header, samples))


def test_processing_worker_output_is_bounded() -> None:
    config = ClientConfig(
        sample_rate_hz=1000.0,
        fft_size=64,
        overlap=32,
        frequency_max_hz=100.0,
        waveform_capacity_seconds=2.0,
        spectrogram_history_seconds=1.0,
    )
    input_queue: queue.Queue[Block] = queue.Queue(maxsize=8)
    output_queue: queue.Queue[ProcessedSnapshot] = queue.Queue(maxsize=1)
    stop = threading.Event()
    worker = ProcessingWorker(config, input_queue, output_queue, stop)
    worker.start()
    for sequence in range(4):
        input_queue.put(make_block(sequence))
    stop.set()
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert output_queue.qsize() == 1
    snapshot = output_queue.get_nowait()
    assert snapshot.waveform.shape[1] == 3
    assert np.max(snapshot.spectrogram.decibels) <= 0.0


def test_processing_runtime_settings_are_applied_safely() -> None:
    config = ClientConfig(
        sample_rate_hz=1000.0,
        fft_size=64,
        overlap=32,
        frequency_max_hz=100.0,
        waveform_capacity_seconds=2.0,
        spectrogram_history_seconds=1.0,
    )
    input_queue: queue.Queue[Block] = queue.Queue(maxsize=2)
    output_queue: queue.Queue[ProcessedSnapshot] = queue.Queue(maxsize=2)
    stop = threading.Event()
    worker = ProcessingWorker(config, input_queue, output_queue, stop)
    worker.set_units("volts")
    worker.set_visible_seconds(0.5)
    input_queue.put(make_block(0))
    stop.set()
    worker.start()
    worker.join(timeout=5.0)
    assert output_queue.get_nowait().unit == "V"


def test_processing_clears_visual_history_at_sequence_gap() -> None:
    config = ClientConfig(
        sample_rate_hz=1000.0,
        fft_size=64,
        overlap=32,
        frequency_max_hz=100.0,
        waveform_capacity_seconds=2.0,
        spectrogram_history_seconds=1.0,
    )
    input_queue: queue.Queue[Block] = queue.Queue(maxsize=2)
    output_queue: queue.Queue[ProcessedSnapshot] = queue.Queue(maxsize=2)
    stop = threading.Event()
    worker = ProcessingWorker(config, input_queue, output_queue, stop)
    input_queue.put(make_block(0))
    input_queue.put(make_block(2))
    stop.set()
    worker.start()
    worker.join(timeout=5.0)
    output_queue.get_nowait()
    latest = output_queue.get_nowait()
    assert len(latest.timestamps_ns) == 64
    assert latest.spectrogram.decibels.shape[2] == 1
