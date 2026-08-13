from __future__ import annotations

import hashlib
from pathlib import Path

from shakepie_terminal.names import RemoteFile, is_finalized_recording_name
from shakepie_terminal.recordings import (
    DayGroup,
    download_days,
    export_miniseed_days,
    group_days,
)


class FakeSession:
    """Minimal session stand-in: a dict of remote paths to file contents."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def download(self, remote_path: str, local_path: Path,
                 progress=None) -> Path:  # type: ignore[no-untyped-def]
        content = self.files[remote_path]
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        if progress is not None:
            progress(len(content), len(content))
        return local_path

    def remote_sha256(self, remote_path: str) -> str | None:
        return hashlib.sha256(self.files[remote_path]).hexdigest()

    @staticmethod
    def local_sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


class _Result:
    def __init__(self, ok: bool = True, stdout: str = "", stderr: str = "") -> None:
        self._ok = ok
        self.stdout = stdout
        self.stderr = stderr

    def ok(self) -> bool:
        return self._ok


class FakeSftp:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def remove(self, path: str) -> None:
        self.removed.append(path)


class ConversionSession(FakeSession):
    """Adds run()/sftp() so export_miniseed_days can be exercised end to end."""

    def __init__(self, exports: dict[str, bytes], failing: set[str] | None = None) -> None:
        super().__init__({})
        self.exports = exports  # remote scratch path -> converted bytes
        self.failing = failing or set()  # recording names whose conversion fails
        self.commands: list[str] = []
        self.sftp_client = FakeSftp()

    def run(self, command: str) -> _Result:  # type: ignore[no-untyped-def]
        self.commands.append(command)
        for name in self.failing:
            if name in command and "miniseed_export" in command:
                return _Result(ok=False, stderr=f"FAILED {name}: broken")
        return _Result()

    def download(self, remote_path: str, local_path: Path,
                 progress=None) -> Path:  # type: ignore[no-untyped-def]
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self.exports[remote_path])
        return local_path

    def sftp(self) -> FakeSftp:
        return self.sftp_client


def _day(day: str, names: list[tuple[str, int]]) -> DayGroup:
    return DayGroup(day, [RemoteFile(name, size) for name, size in names])


def test_group_days_sorts_and_skips_foreign_names() -> None:
    groups = group_days(
        [
            RemoteFile("SHAKEPIE_20260915T181500Z.spb", 10),
            RemoteFile("SHAKEPIE_20260914T120000Z.spb", 20),
            RemoteFile("SHAKEPIE_20260915T193000Z_u1789500948834172586.spb", 30),
            RemoteFile("unrelated.log", 5),
        ]
    )
    assert [group.day for group in groups] == ["2026-09-14", "2026-09-15"]
    assert groups[0].total_bytes == 20
    assert groups[1].total_bytes == 40  # the collision-suffixed file belongs to the day too


def test_download_days_writes_day_directories_and_is_idempotent(tmp_path: Path) -> None:
    remote = {
        "/mnt/shakepie-data/recordings/SHAKEPIE_20260915T181500Z.spb": b"one",
        "/mnt/shakepie-data/recordings/SHAKEPIE_20260915T183000Z.spb": b"two-two",
    }
    session = FakeSession(remote)
    groups = [
        _day(
            "2026-09-15",
            [
                ("SHAKEPIE_20260915T181500Z.spb", 3),
                ("SHAKEPIE_20260915T183000Z.spb", 7),
            ],
        )
    ]
    done, skipped, failed = download_days(session, groups, tmp_path, console=None)
    assert (done, skipped, failed) == (2, 0, 0)
    assert (tmp_path / "2026-09-15" / "SHAKEPIE_20260915T181500Z.spb").read_bytes() == b"one"

    done, skipped, failed = download_days(session, groups, tmp_path, console=None)
    assert (done, skipped, failed) == (0, 2, 0)


def test_download_days_verify_detects_corruption(tmp_path: Path) -> None:
    remote = {"/mnt/shakepie-data/recordings/SHAKEPIE_20260915T181500Z.spb": b"payload"}

    class CorruptingSession(FakeSession):
        def download(self, remote_path, local_path, progress=None):  # type: ignore[no-untyped-def]
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"CORRUPTED")

    groups = [_day("2026-09-15", [("SHAKEPIE_20260915T181500Z.spb", 8)])]
    done, _skipped, failed = download_days(
        CorruptingSession(remote), groups, tmp_path, verify=True, console=None
    )
    assert (done, failed) == (0, 1)


def test_download_days_isolates_a_failing_file(tmp_path: Path) -> None:
    remote = {
        "/mnt/shakepie-data/recordings/SHAKEPIE_20260915T181500Z.spb": b"good",
        "/mnt/shakepie-data/recordings/SHAKEPIE_20260915T183000Z.spb": b"good-two",
    }
    session = FakeSession(remote)

    original_download = session.download

    def flaky(remote_path: str, local_path: Path, progress=None):  # type: ignore[no-untyped-def]
        if "183000" in remote_path:
            raise OSError("network blip")
        return original_download(remote_path, local_path, progress)

    session.download = flaky  # type: ignore[method-assign]
    groups = [
        _day(
            "2026-09-15",
            [
                ("SHAKEPIE_20260915T181500Z.spb", 4),
                ("SHAKEPIE_20260915T183000Z.spb", 8),
            ],
        )
    ]
    done, _skipped, failed = download_days(session, groups, tmp_path, console=None)
    assert (done, failed) == (1, 1)
    assert (tmp_path / "2026-09-15" / "SHAKEPIE_20260915T181500Z.spb").exists()


def test_partial_files_never_group_into_days() -> None:
    assert not is_finalized_recording_name("SHAKEPIE_20260915T190000Z.partial")
    assert group_days([RemoteFile("SHAKEPIE_20260915T190000Z.partial", 5)]) == []


# ---------------------------------------------------------------------------
# MiniSEED conversion download


def _miniseed_day(names: list[tuple[str, int]]) -> list[DayGroup]:
    return [_day("2026-09-15", names)]


def test_export_miniseed_downloads_converted_files(tmp_path: Path) -> None:
    session = ConversionSession(
        {
            "/tmp/shakepie-miniseed/SHAKEPIE_20260915T181500Z.miniseed": b"mseed-n-e-z",
            "/tmp/shakepie-miniseed/SHAKEPIE_20260915T183000Z.miniseed": b"mseed-two",
        }
    )
    groups = _miniseed_day(
        [
            ("SHAKEPIE_20260915T181500Z.spb", 400),
            ("SHAKEPIE_20260915T183000Z.spb", 800),
        ]
    )
    done, skipped, failed = export_miniseed_days(session, groups, tmp_path)
    assert (done, skipped, failed) == (2, 0, 0)
    first = tmp_path / "2026-09-15" / "SHAKEPIE_20260915T181500Z.miniseed"
    assert first.read_bytes() == b"mseed-n-e-z"
    # the conversion command targets the right recording and channel selection
    exports = [command for command in session.commands if "miniseed_export" in command]
    assert len(exports) == 2
    assert "SHAKEPIE_20260915T181500Z.spb" in exports[0]
    assert "--channel all" in exports[0]
    # scratch copies are removed after transfer and the directory cleaned up
    assert len(session.sftp_client.removed) == 2
    assert session.commands[-1] == "rm -rf /tmp/shakepie-miniseed"


def test_export_miniseed_single_channel_naming_and_skip_existing(tmp_path: Path) -> None:
    session = ConversionSession(
        {"/tmp/shakepie-miniseed/SHAKEPIE_20260915T181500Z_vertical.miniseed": b"z-only"}
    )
    groups = _miniseed_day([("SHAKEPIE_20260915T181500Z.spb", 400)])
    done, skipped, failed = export_miniseed_days(
        session, groups, tmp_path, channel="vertical"
    )
    assert (done, skipped, failed) == (1, 0, 0)
    assert (
        tmp_path / "2026-09-15" / "SHAKEPIE_20260915T181500Z_vertical.miniseed"
    ).read_bytes() == b"z-only"

    # a second run skips what is already on this machine
    done, skipped, failed = export_miniseed_days(
        session, groups, tmp_path, channel="vertical"
    )
    assert (done, skipped, failed) == (0, 1, 0)


def test_export_miniseed_isolates_conversion_failures(tmp_path: Path) -> None:
    session = ConversionSession(
        {"/tmp/shakepie-miniseed/SHAKEPIE_20260915T183000Z.miniseed": b"ok"},
        failing={"SHAKEPIE_20260915T181500Z.spb"},
    )
    groups = _miniseed_day(
        [
            ("SHAKEPIE_20260915T181500Z.spb", 400),
            ("SHAKEPIE_20260915T183000Z.spb", 800),
        ]
    )
    done, _skipped, failed = export_miniseed_days(session, groups, tmp_path)
    assert (done, failed) == (1, 1)
    assert (tmp_path / "2026-09-15" / "SHAKEPIE_20260915T183000Z.miniseed").exists()
    assert not (tmp_path / "2026-09-15" / "SHAKEPIE_20260915T181500Z.miniseed").exists()
