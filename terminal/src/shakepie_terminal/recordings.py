"""Day-oriented operations over the recorder's data: list, offload, and remove.

Recordings are grouped into UTC days purely from the recorder's own filenames, so
the terminal works even when the archive-web catalog is stale. Deletion only ever
touches finalized ``*.spb`` files — live ``.partial`` files are untouchable by
design, because the recorder may be appending to them right now.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from shakepie_terminal.connection import ShakePieSession
from shakepie_terminal.names import (
    ARCHIVE_WEB_DIR,
    DAYPLOT_DIR,
    RECORDING_DIR,
    RemoteFile,
    human_bytes,
    is_finalized_recording_name,
)


@dataclass(slots=True)
class DayGroup:
    day: str
    files: list[RemoteFile] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.files)

    def label(self, downloaded: set[str] | None = None) -> str:
        extra = ""
        if downloaded is not None and self.day in downloaded:
            extra = "  · already offloaded"
        return f"{self.day}  ({len(self.files)} files · {human_bytes(self.total_bytes)}){extra}"


def _all_files(_name: str) -> bool:
    return True


def _day_directories(name: str) -> bool:
    return not name.startswith(".")


def group_days(files: list[RemoteFile]) -> list[DayGroup]:
    """Group finalized files into chronological UTC days (oldest first)."""
    groups: dict[str, DayGroup] = {}
    for entry in files:
        day = entry.day
        if day is None:
            continue
        groups.setdefault(day, DayGroup(day)).files.append(entry)
    return [groups[day] for day in sorted(groups)]


def list_recording_days(session: ShakePieSession) -> list[DayGroup]:
    files = session.list_files(RECORDING_DIR, is_finalized_recording_name)
    return group_days(files)


def list_dayplot_days(session: ShakePieSession) -> list[DayGroup]:
    """One pseudo-entry per day directory, sized by the sum of its plot images."""
    groups: dict[str, DayGroup] = {}
    for day_dir in session.list_files(DAYPLOT_DIR, _day_directories):
        try:
            plots = session.list_files(f"{DAYPLOT_DIR}/{day_dir.name}", _all_files)
        except Exception:
            continue
        if not plots:
            continue
        group = groups.setdefault(day_dir.name, DayGroup(day_dir.name))
        group.files.append(RemoteFile(day_dir.name, sum(plot.size for plot in plots)))
    return [groups[day] for day in sorted(groups)]


def day_directory_name(day: str) -> str:
    return day.replace("-", "")


def download_days(
    session: ShakePieSession,
    days: list[DayGroup],
    destination: Path,
    *,
    verify: bool = False,
    console: Console | None = None,
) -> tuple[int, int, int]:
    """Download whole days into ``destination/<UTC-day>/``.

    Existing local files with the same size are skipped, so re-running an offload is
    idempotent. Returns ``(files_downloaded, files_skipped, files_failed)``.
    """
    downloaded = skipped = failed = 0
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    with progress:
        for group in days:
            for entry in group.files:
                remote_path = f"{RECORDING_DIR}/{entry.name}"
                local_path = destination / group.day / entry.name
                if local_path.exists() and local_path.stat().st_size == entry.size:
                    skipped += 1
                    continue
                task = progress.add_task(entry.name, total=entry.size)

                def report(sent: int, total: int, task_id: TaskID = task) -> None:
                    progress.update(task_id, completed=sent, total=total)

                try:
                    session.download(remote_path, local_path, progress=report)
                    if verify and entry.size > 0:
                        remote_digest = session.remote_sha256(remote_path)
                        if remote_digest is None:
                            failed += 1
                            continue
                        if ShakePieSession.local_sha256(local_path) != remote_digest:
                            failed += 1
                            continue
                    downloaded += 1
                except Exception:
                    failed += 1
                finally:
                    progress.remove_task(task)
    return downloaded, skipped, failed


def download_dayplots(
    session: ShakePieSession,
    days: list[DayGroup],
    destination: Path,
) -> tuple[int, int, int]:
    """Download every plot image of the chosen days, preserving the day subfolders."""
    downloaded = skipped = failed = 0
    for group in days:
        plots = session.list_files(f"{DAYPLOT_DIR}/{day_directory_name(group.day)}", _all_files)
        for plot in plots:
            remote_path = f"{DAYPLOT_DIR}/{day_directory_name(group.day)}/{plot.name}"
            local_path = destination / "dayplots" / group.day / plot.name
            try:
                if local_path.exists() and local_path.stat().st_size == plot.size:
                    skipped += 1
                    continue
                session.download(remote_path, local_path)
                downloaded += 1
            except Exception:
                failed += 1
    return downloaded, skipped, failed


def export_miniseed_days(
    session: ShakePieSession,
    days: list[DayGroup],
    destination: Path,
    *,
    channel: str = "all",
    console: Console | None = None,
) -> tuple[int, int, int]:
    """Download the chosen days converted to miniSEED (``<day>/<name>.miniseed``).

    Conversion runs on the recorder itself through the archive-web export CLI, so
    the raw-format logic lives in exactly one place; the ``.miniseed`` file is
    transferred over SFTP and the remote scratch copy removed. Existing local
    files are skipped (their size cannot be predicted before conversion).
    Returns ``(files_downloaded, files_skipped, files_failed)``.
    """
    downloaded = skipped = failed = 0
    scratch = "/tmp/shakepie-miniseed"
    session.run(f"mkdir -p {scratch}")
    try:
        for group in days:
            for entry in group.files:
                stem = entry.name.rsplit(".", 1)[0]
                suffix = "" if channel == "all" else f"_{channel}"
                local_path = destination / group.day / f"{stem}{suffix}.miniseed"
                if local_path.exists() and local_path.stat().st_size > 0:
                    skipped += 1
                    continue
                conversion = session.run(
                    f"cd {ARCHIVE_WEB_DIR} && .venv/bin/python -m "
                    f"shakepie_archive.miniseed_export "
                    f"--file {RECORDING_DIR}/{entry.name} --out {scratch} "
                    f"--channel {channel}"
                )
                if not conversion.ok():
                    failed += 1
                    if console is not None:
                        detail = (conversion.stderr.strip() or "conversion failed")[:160]
                        console.print(f"[red]{entry.name}: {detail}[/red]")
                    continue
                try:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    session.download(f"{scratch}/{stem}{suffix}.miniseed", local_path)
                    session.sftp().remove(f"{scratch}/{stem}{suffix}.miniseed")
                    downloaded += 1
                    if console is not None:
                        console.print(f"[green]converted[/green] {entry.name}")
                except Exception:
                    failed += 1
    finally:
        session.run(f"rm -rf {scratch}")
    return downloaded, skipped, failed


def delete_day_files(
    session: ShakePieSession,
    group: DayGroup,
    *,
    require_confirmation: Callable[[DayGroup], bool],
) -> int:
    """Delete one day's finalized recordings after an explicit caller-side confirmation."""
    if not require_confirmation(group):
        return 0
    removed = 0
    for entry in group.files:
        if not is_finalized_recording_name(entry.name):
            continue
        try:
            session.sftp().remove(f"{RECORDING_DIR}/{entry.name}")
            removed += 1
        except OSError:
            continue
    return removed


def delete_dayplots(session: ShakePieSession, day: str) -> int:
    """Remove all plot images of one day (the directory itself is left in place)."""
    removed = 0
    for plot in session.list_files(f"{DAYPLOT_DIR}/{day_directory_name(day)}", _all_files):
        try:
            session.sftp().remove(f"{DAYPLOT_DIR}/{day_directory_name(day)}/{plot.name}")
            removed += 1
        except OSError:
            continue
    return removed
