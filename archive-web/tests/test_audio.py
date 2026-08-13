from __future__ import annotations

import io
import struct
import wave

from conftest import write_recording

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings


def _seed_two_recordings(settings: Settings, catalog: Catalog) -> tuple[int, int]:
    write_recording(
        settings.archive.data_directory,
        "loud.spb",
        samples_per_block=2,
        first_sequences=(0, 2),
    )
    write_recording(
        settings.archive.data_directory,
        "quiet.spb",
        samples_per_block=2,
        first_sequences=(4, 6),
    )
    catalog.reconcile(settings.archive.data_directory)
    rows = catalog.records_for_day("2025-01-01")
    by_name = {row["filename"]: int(row["id"]) for row in rows}
    return by_name["loud.spb"], by_name["quiet.spb"]


def _wav_params(data: bytes) -> tuple[int, int, int, int, int]:
    with wave.open(io.BytesIO(data)) as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    peak = max((abs(sample) for sample in samples), default=0)
    return channels, width, rate, len(samples), peak


def test_audio_endpoint_returns_mono_16bit_8khz(
    settings: Settings, catalog: Catalog, authenticate, client
) -> None:  # type: ignore[no-untyped-def]
    authenticate()
    loud_id, _ = _seed_two_recordings(settings, catalog)
    response = client.get(f"/recordings/{loud_id}/audio?channel=north")
    assert response.status_code == 200
    assert response.mimetype == "audio/wav"
    channels, width, rate, frames, _peak = _wav_params(response.data)
    assert (channels, width, rate) == (1, 2, 8000)
    assert frames > 0


def test_audio_endpoint_normalizes_loudness(
    settings: Settings, catalog: Catalog, authenticate, client
) -> None:  # type: ignore[no-untyped-def]
    authenticate()
    loud_id, _ = _seed_two_recordings(settings, catalog)
    loud = client.get(f"/recordings/{loud_id}/audio?channel=north")
    _channels, _width, _rate, _frames, loud_peak = _wav_params(loud.data)
    assert 0.5 * 32_767 < loud_peak <= 0.8 * 32_767 + 1  # normalized, never clipped


def test_audio_endpoint_channel_selection_differs(
    settings: Settings, catalog: Catalog, authenticate, client
) -> None:
    authenticate()
    write_recording(settings.archive.data_directory, "ch.spb", samples_per_block=1)
    catalog.reconcile(settings.archive.data_directory)
    row = catalog.records_for_day("2025-01-01")[0]
    north = client.get(f"/recordings/{row['id']}/audio?channel=north").data
    east = client.get(f"/recordings/{row['id']}/audio?channel=east").data
    assert north != east


def test_audio_endpoint_rejects_unknown_channel(
    settings: Settings, catalog: Catalog, authenticate, client
) -> None:
    authenticate()
    write_recording(settings.archive.data_directory, "day.spb")
    catalog.reconcile(settings.archive.data_directory)
    row = catalog.records_for_day("2025-01-01")[0]
    assert client.get(f"/recordings/{row['id']}/audio?channel=diagonal").status_code == 400


def test_audio_endpoint_requires_valid_recording(
    settings: Settings, catalog: Catalog, authenticate, client
) -> None:
    authenticate()
    assert client.get("/recordings/9999/audio").status_code == 404
