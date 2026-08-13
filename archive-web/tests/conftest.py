from __future__ import annotations

import struct
from collections.abc import Callable
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash

from shakepie_archive.app import create_app
from shakepie_archive.archive_reader import _crc32c
from shakepie_archive.catalog import Catalog
from shakepie_archive.config import (
    ArchiveConfig,
    DayplotConfig,
    EventDetectorConfig,
    SecurityConfig,
    Settings,
    WatchConfig,
    WebConfig,
)

FILE_HEADER = struct.Struct("<4sHHHHHBBHHIIIQQQQ32s28sI")
BLOCK_HEADER = struct.Struct("<4sHHIIQQQQIIIHBB")
FOOTER = struct.Struct("<4sHHQQQQQQII")
SAMPLE = struct.Struct("<iiiI")


def write_recording(
    directory: Path,
    name: str,
    *,
    start_ns: int = 1_735_689_600_000_000_000,
    first_sequences: tuple[int, ...] = (0, 2),
    recovered: bool = False,
    sample_rate_millihz: int = 200_000,
    samples_per_block: int = 2,
    samples: list[tuple[int, int, int]] | None = None,
) -> Path:
    """Write a small finalized recording.

    ``samples`` optionally supplies explicit (north, east, vertical) groups; the
    list is cut into blocks of ``samples_per_block`` (a short final block is
    allowed) and ``first_sequences`` must carry one entry per block. Without it
    the deterministic count pattern (1, 2, 3), (2, 3, 4), ... is used.
    """
    station = b"TEST" + bytes(28)
    if samples is None:
        blocks = len(first_sequences)
    else:
        blocks = max(1, -(-len(samples) // samples_per_block))
        assert len(first_sequences) == blocks, "one first sequence per block"
    header = bytearray(
        FILE_HEADER.pack(
            b"SPFH",
            1,
            128,
            1,
            64,
            16,
            3,
            0,
            64,
            0,
            2_000_000,
            2_500_000,
            samples_per_block,
            start_ns,
            100,
            10,
            first_sequences[0],
            station,
            bytes(28),
            0,
        )
    )
    struct.pack_into("<I", header, 124, _crc32c(memoryview(header)[:124]))
    frames = bytearray(header)
    last_sequence = 0
    period_ns = round(1_000_000_000_000 / sample_rate_millihz)
    for index, first_sequence in enumerate(first_sequences):
        if samples is None:
            groups = [
                (
                    index * samples_per_block + sample + 1,
                    index * samples_per_block + sample + 2,
                    index * samples_per_block + sample + 3,
                )
                for sample in range(samples_per_block)
            ]
        else:
            groups = samples[index * samples_per_block : (index + 1) * samples_per_block]
        payload = b"".join(
            SAMPLE.pack(north, east, vertical, sample * period_ns)
            for sample, (north, east, vertical) in enumerate(groups)
        )
        block = bytearray(
            BLOCK_HEADER.pack(
                b"SPBK",
                1,
                64,
                len(payload),
                0,
                10 + index,
                first_sequence,
                start_ns + index * samples_per_block * period_ns,
                100 + index * samples_per_block * period_ns,
                len(groups),
                0,
                sample_rate_millihz,
                16,
                3,
                0,
            )
        )
        block.extend(payload)
        block.extend(struct.pack("<I", _crc32c(block)))
        frames.extend(struct.pack("<I", len(block)))
        frames.extend(block)
        last_sequence = first_sequence + len(groups) - 1
    footer = bytearray(
        FOOTER.pack(
            b"SPFT",
            1,
            64,
            blocks,
            len(samples) if samples is not None else blocks * samples_per_block,
            10,
            10 + blocks - 1,
            first_sequences[0],
            last_sequence,
            1 if recovered else 0,
            0,
        )
    )
    struct.pack_into("<I", footer, 60, _crc32c(memoryview(footer)[:60]))
    frames.extend(footer)
    path = directory / name
    path.write_bytes(frames)
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    return Settings(
        web=WebConfig(),
        archive=ArchiveConfig(
            mount_point=tmp_path,
            data_directory=recordings,
            catalog_path=tmp_path / "index" / "archive.sqlite",
            require_mount=False,
        ),
        dayplot=DayplotConfig(
            output_directory=tmp_path / "dayplots",
            temporary_directory=tmp_path / "dayplots" / "tmp",
            minimum_free_gib=1,
        ),
        security=SecurityConfig(
            secret_key="s" * 48,
            password_hash=generate_password_hash("correct horse battery staple", method="scrypt"),
        ),
        event_detector=EventDetectorConfig(enabled=False),
    watch=WatchConfig(enabled=False, state_path=tmp_path / "watch.json"),
        health_file=tmp_path / "health.json",
        source_path=tmp_path / "archive-web.conf",
    )


@pytest.fixture
def catalog(settings: Settings) -> Catalog:
    value = Catalog(settings.archive.catalog_path)
    value.initialize()
    return value


@pytest.fixture
def app(settings: Settings) -> Flask:
    value = create_app(settings)
    value.config.update(TESTING=True)
    return value


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def authenticate(client: FlaskClient) -> Callable[[], str]:
    def apply() -> str:
        with client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-csrf"
        return "test-csrf"

    return apply
