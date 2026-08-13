"""Rebuildable SQLite metadata catalog and lightweight dayplot queue."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, cast

from shakepie_archive.archive_reader import RecordingError, finalized_name, read_recording
from shakepie_archive.naming import canonical_dayplot_name, legacy_dayplot_name

SCHEMA_VERSION: Final = 2


@dataclass(frozen=True, slots=True)
class Page:
    items: list[sqlite3.Row]
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.per_page - 1) // self.per_page)


class Catalog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        with self.transaction(immediate=True) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"catalog schema {version} is newer than this application")
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE recordings (
                        id INTEGER PRIMARY KEY,
                        relative_path TEXT NOT NULL UNIQUE,
                        filename TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'active'
                            CHECK (state IN ('active', 'trash', 'missing', 'purged')),
                        utc_day TEXT,
                        station_id TEXT,
                        start_ns INTEGER,
                        end_ns INTEGER,
                        sample_rate_millihz INTEGER,
                        size_bytes INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        device TEXT NOT NULL,
                        inode TEXT NOT NULL,
                        sample_count INTEGER,
                        block_count INTEGER,
                        recovered INTEGER NOT NULL DEFAULT 0,
                        validation_status TEXT NOT NULL
                            CHECK (validation_status IN ('valid', 'invalid')),
                        validation_error TEXT,
                        sequence_gap_count INTEGER NOT NULL DEFAULT 0,
                        indexed_at TEXT NOT NULL
                    );
                    CREATE INDEX idx_recordings_day_start
                        ON recordings(utc_day, start_ns) WHERE state = 'active';
                    CREATE INDEX idx_recordings_state ON recordings(state);

                    -- Retained solely so existing schema-v1 catalogs remain readable.
                    CREATE TABLE trash (
                        recording_id INTEGER PRIMARY KEY REFERENCES recordings(id),
                        trash_name TEXT NOT NULL UNIQUE,
                        original_path TEXT NOT NULL,
                        deleted_at TEXT NOT NULL,
                        deleted_by TEXT NOT NULL,
                        restored_at TEXT,
                        purged_at TEXT
                    );

                    CREATE TABLE dayplot_cache (
                        utc_day TEXT NOT NULL,
                        source_fingerprint TEXT NOT NULL,
                        channel TEXT NOT NULL CHECK (channel IN ('north', 'east', 'vertical')),
                        relative_path TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (utc_day, source_fingerprint, channel)
                    );

                    CREATE TABLE dayplot_jobs (
                        id INTEGER PRIMARY KEY,
                        utc_day TEXT NOT NULL,
                        source_fingerprint TEXT NOT NULL,
                        state TEXT NOT NULL
                            CHECK (state IN ('queued', 'running', 'completed', 'failed')),
                        progress TEXT NOT NULL,
                        requested_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        error_message TEXT
                    );
                    CREATE INDEX idx_dayplot_jobs_state_requested
                        ON dayplot_jobs(state, requested_at);
                    CREATE UNIQUE INDEX idx_dayplot_jobs_active
                        ON dayplot_jobs(utc_day, source_fingerprint)
                        WHERE state IN ('queued', 'running');

                    CREATE TABLE audit_log (
                        id INTEGER PRIMARY KEY,
                        occurred_at TEXT NOT NULL,
                        username TEXT NOT NULL,
                        action TEXT NOT NULL,
                        recording_id INTEGER,
                        filename TEXT,
                        detail TEXT NOT NULL
                    );
                    """
                )
                version = 1
            if version == 1:
                connection.executescript(
                    """
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY,
                        started_ns INTEGER NOT NULL,
                        ended_ns INTEGER NOT NULL,
                        peak_amplitude INTEGER NOT NULL,
                        peak_ratio REAL NOT NULL,
                        station_id TEXT,
                        detected_at TEXT NOT NULL
                    );
                    CREATE INDEX idx_events_started ON events(started_ns);
                    """
                )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("PRAGMA optimize")

    def schema_version(self) -> int:
        with self.reader() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def reconcile(self, data_directory: Path) -> dict[str, int]:
        """Index each changed finalized file once; never opens active partials."""
        seen: set[str] = set()
        indexed = 0
        invalid = 0
        skipped = 0
        with self.reader() as connection:
            existing = {
                str(row["relative_path"]): row
                for row in connection.execute(
                    "SELECT relative_path, size_bytes, mtime_ns, device, inode, validation_status "
                    "FROM recordings WHERE state = 'active'"
                )
            }
        pending: list[dict[str, Any]] = []
        with os.scandir(data_directory) as entries:
            for entry in entries:
                name = entry.name
                if (
                    not finalized_name(name)
                    or entry.is_symlink()
                    or not entry.is_file(follow_symlinks=False)
                ):
                    continue
                seen.add(name)
                information = entry.stat(follow_symlinks=False)
                old = existing.get(name)
                if (
                    old is not None
                    and int(old["size_bytes"]) == information.st_size
                    and int(old["mtime_ns"]) == information.st_mtime_ns
                    and (
                        os.name != "posix"
                        or (
                            int(old["device"]) == information.st_dev
                            and int(old["inode"]) == information.st_ino
                        )
                    )
                ):
                    skipped += 1
                    continue
                now = _now()
                try:
                    metadata = read_recording(data_directory / name)
                    utc_day = (
                        datetime.fromtimestamp(metadata.start_ns / 1e9, UTC).date().isoformat()
                    )
                    pending.append(
                        {
                            "relative_path": name,
                            "filename": name,
                            "utc_day": utc_day,
                            "station_id": metadata.station_id,
                            "start_ns": metadata.start_ns,
                            "end_ns": metadata.end_ns,
                            "sample_rate": metadata.sample_rate_millihz,
                            "size": metadata.size_bytes,
                            "mtime": metadata.mtime_ns,
                            "device": str(metadata.device),
                            "inode": str(metadata.inode),
                            "samples": metadata.sample_count,
                            "blocks": metadata.block_count,
                            "recovered": int(metadata.recovered),
                            "validation": "valid",
                            "error": None,
                            "gaps": metadata.sequence_gap_count,
                            "indexed": now,
                        }
                    )
                    indexed += 1
                except (OSError, RecordingError) as error:
                    pending.append(
                        {
                            "relative_path": name,
                            "filename": name,
                            "utc_day": None,
                            "station_id": None,
                            "start_ns": None,
                            "end_ns": None,
                            "sample_rate": None,
                            "size": information.st_size,
                            "mtime": information.st_mtime_ns,
                            "device": str(information.st_dev),
                            "inode": str(information.st_ino),
                            "samples": None,
                            "blocks": None,
                            "recovered": int(name.endswith(".recovered.spb")),
                            "validation": "invalid",
                            "error": str(error)[:500],
                            "gaps": 0,
                            "indexed": now,
                        }
                    )
                    invalid += 1
        with self.transaction(immediate=True) as connection:
            for item in pending:
                connection.execute(
                    """
                    INSERT INTO recordings (
                        relative_path, filename, state, utc_day, station_id, start_ns, end_ns,
                        sample_rate_millihz, size_bytes, mtime_ns, device, inode, sample_count,
                        block_count, recovered, validation_status, validation_error,
                        sequence_gap_count, indexed_at
                    ) VALUES (
                        :relative_path, :filename, 'active', :utc_day, :station_id, :start_ns,
                        :end_ns, :sample_rate, :size, :mtime, :device, :inode, :samples, :blocks,
                        :recovered, :validation, :error, :gaps, :indexed
                    )
                    ON CONFLICT(relative_path) DO UPDATE SET
                        filename = excluded.filename,
                        state = 'active',
                        utc_day = excluded.utc_day,
                        station_id = excluded.station_id,
                        start_ns = excluded.start_ns,
                        end_ns = excluded.end_ns,
                        sample_rate_millihz = excluded.sample_rate_millihz,
                        size_bytes = excluded.size_bytes,
                        mtime_ns = excluded.mtime_ns,
                        device = excluded.device,
                        inode = excluded.inode,
                        sample_count = excluded.sample_count,
                        block_count = excluded.block_count,
                        recovered = excluded.recovered,
                        validation_status = excluded.validation_status,
                        validation_error = excluded.validation_error,
                        sequence_gap_count = excluded.sequence_gap_count,
                        indexed_at = excluded.indexed_at
                    """,
                    item,
                )
            active_paths = set(existing)
            missing = active_paths - seen
            if missing:
                connection.executemany(
                    "UPDATE recordings SET state = 'missing' WHERE relative_path = ? "
                    "AND state = 'active'",
                    ((name,) for name in missing),
                )
        return {"indexed": indexed, "invalid": invalid, "skipped": skipped, "missing": len(missing)}

    def days(self) -> list[str]:
        with self.reader() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT utc_day FROM recordings "
                    "WHERE state = 'active' AND utc_day IS NOT NULL ORDER BY utc_day"
                )
            ]

    def available_day_counts(self) -> list[dict[str, Any]]:
        """UTC days that contain finalized recordings, with per-day recording counts."""
        with self.reader() as connection:
            return [
                {"date": str(row["utc_day"]), "recordings": int(row["recordings"])}
                for row in connection.execute(
                    "SELECT utc_day, COUNT(*) AS recordings FROM recordings "
                    "WHERE state = 'active' AND utc_day IS NOT NULL "
                    "GROUP BY utc_day ORDER BY utc_day"
                )
            ]

    def station_for_day(self, utc_day: str) -> str:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT station_id FROM recordings WHERE utc_day = ? AND state = 'active' "
                "AND station_id IS NOT NULL ORDER BY start_ns LIMIT 1",
                (utc_day,),
            ).fetchone()
        return str(row["station_id"]) if row is not None else "SHAKEPIE"

    def auto_queue_completed_dayplots(self, today: date | None = None) -> list[str]:
        """Queue dayplot jobs for finished, gap-free days that have no current cached plot.

        Called periodically by the worker so each completed day receives its daily plot
        automatically without anyone opening the web interface.
        """
        today = today or datetime.now(UTC).date()
        queued: list[str] = []
        for day in self.days():
            if date.fromisoformat(day) >= today:
                continue
            if not self.day_summary(day)["complete"]:
                continue
            try:
                fingerprint = self.source_fingerprint(day)
            except LookupError:
                continue
            with self.reader() as connection:
                cached = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM dayplot_cache "
                        "WHERE utc_day = ? AND source_fingerprint = ?",
                        (day, fingerprint),
                    ).fetchone()[0]
                )
            if cached == 3:
                continue
            with self.reader() as connection:
                active_job = connection.execute(
                    "SELECT 1 FROM dayplot_jobs WHERE utc_day = ? "
                    "AND state IN ('queued', 'running') LIMIT 1",
                    (day,),
                ).fetchone()
            if active_job is not None:
                continue
            try:
                self.queue_dayplot(day, force=False)
            except FileExistsError:
                continue
            queued.append(day)
        return queued

    def migrate_dayplot_names(
        self, output_directory: Path, station_id: str | None = None
    ) -> tuple[int, int]:
        """Rename cached dayplots that predate the current naming convention.

        Returns ``(renamed, updated)``. Runs at worker startup so every cached image is
        reachable under its stable canonical filename; existing canonical files are never
        overwritten. ``station_id`` (from the recorder settings) names the migrated
        files; when it is missing, the station recorded for each day is used.
        """
        with self.reader() as connection:
            rows = list(
                connection.execute(
                    "SELECT utc_day, source_fingerprint, channel, relative_path FROM dayplot_cache"
                )
            )
        renamed = 0
        updated = 0
        for row in rows:
            relative_path = str(row["relative_path"])
            if not legacy_dayplot_name(relative_path):
                continue
            current = output_directory / relative_path
            try:
                if current.is_symlink() or not current.is_file():
                    continue
            except OSError:
                continue
            canonical = canonical_dayplot_name(
                station_id or self.station_for_day(str(row["utc_day"])),
                str(row["utc_day"]),
                str(row["channel"]),
                current.suffix.lstrip("."),
            )
            target = current.parent / canonical
            if target.exists():
                continue
            try:
                os.replace(current, target)
            except OSError:
                continue
            renamed += 1
            with self.transaction(immediate=True) as connection:
                cursor = connection.execute(
                    "UPDATE dayplot_cache SET relative_path = ? "
                    "WHERE utc_day = ? AND source_fingerprint = ? AND channel = ? "
                    "AND relative_path = ?",
                    (
                        str(target.relative_to(output_directory)),
                        str(row["utc_day"]),
                        str(row["source_fingerprint"]),
                        str(row["channel"]),
                        relative_path,
                    ),
                )
            updated += int(cursor.rowcount or 0)
        return renamed, updated

    def cleanup_orphan_dayplots(self, output_directory: Path) -> int:
        """Delete legacy fingerprint-named files that no cache row references anymore."""
        with self.reader() as connection:
            referenced = {
                str(row["relative_path"])
                for row in connection.execute("SELECT relative_path FROM dayplot_cache")
            }
        removed = 0
        if not output_directory.is_dir():
            return removed
        for day_directory in output_directory.iterdir():
            if not day_directory.is_dir() or day_directory.name == "tmp":
                continue
            for candidate in day_directory.iterdir():
                relative_path = str(candidate.relative_to(output_directory))
                if not candidate.is_symlink() and candidate.is_file() and (
                    legacy_dayplot_name(relative_path) and relative_path not in referenced
                ):
                    try:
                        candidate.unlink()
                        removed += 1
                    except OSError:
                        continue
        return removed

    def recordings_for_day(self, utc_day: str, page: int, per_page: int = 25) -> Page:
        page = max(1, page)
        per_page = max(1, min(per_page, 100))
        with self.reader() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM recordings WHERE state = 'active' AND utc_day = ?",
                    (utc_day,),
                ).fetchone()[0]
            )
            rows = list(
                connection.execute(
                    "SELECT * FROM recordings WHERE state = 'active' AND utc_day = ? "
                    "ORDER BY start_ns, id LIMIT ? OFFSET ?",
                    (utc_day, per_page, (page - 1) * per_page),
                )
            )
        return Page(rows, page, per_page, total)

    def records_for_day(self, utc_day: str) -> list[sqlite3.Row]:
        with self.reader() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM recordings WHERE state = 'active' AND utc_day = ? "
                    "AND validation_status = 'valid' ORDER BY start_ns, id",
                    (utc_day,),
                )
            )

    def day_summary(self, utc_day: str) -> dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes,
                       MIN(start_ns) AS first_ns, MAX(end_ns) AS last_ns,
                       COALESCE(SUM(sequence_gap_count), 0) AS gaps,
                       COALESCE(SUM(recovered), 0) AS recovered,
                       COALESCE(SUM(validation_status != 'valid'), 0) AS invalid
                FROM recordings WHERE state = 'active' AND utc_day = ?
                """,
                (utc_day,),
            ).fetchone()
        selected = date.fromisoformat(utc_day)
        day_start = int(datetime.combine(selected, datetime.min.time(), UTC).timestamp() * 1e9)
        day_end = day_start + 86_400_000_000_000
        tolerance = 2_000_000_000
        complete = bool(
            row["count"]
            and selected < datetime.now(UTC).date()
            and row["invalid"] == 0
            and row["gaps"] == 0
            and row["first_ns"] <= day_start + tolerance
            and row["last_ns"] >= day_end - tolerance
        )
        return {
            "count": int(row["count"]),
            "bytes": int(row["bytes"]),
            "first_ns": row["first_ns"],
            "last_ns": row["last_ns"],
            "gaps": int(row["gaps"]),
            "recovered": int(row["recovered"]),
            "invalid": int(row["invalid"]),
            "complete": complete,
        }

    def recording(self, recording_id: int, *, state: str | None = None) -> sqlite3.Row | None:
        query = "SELECT * FROM recordings WHERE id = ?"
        parameters: tuple[Any, ...] = (recording_id,)
        if state is not None:
            query += " AND state = ?"
            parameters += (state,)
        with self.reader() as connection:
            return cast(sqlite3.Row | None, connection.execute(query, parameters).fetchone())

    def latest_recording(self) -> sqlite3.Row | None:
        with self.reader() as connection:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM recordings WHERE state = 'active' "
                    "AND validation_status = 'valid' ORDER BY start_ns DESC LIMIT 1"
                ).fetchone(),
            )

    def index_status(self) -> dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, SUM(validation_status = 'invalid') AS invalid, "
                "MAX(indexed_at) AS updated FROM recordings WHERE state IN ('active', 'missing')"
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "invalid": int(row["invalid"] or 0),
            "updated": row["updated"],
        }

    def mark_deleted(self, recording_id: int, username: str) -> None:
        """Mark an active recording permanently deleted after its file was removed."""
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT filename FROM recordings WHERE id = ? AND state = 'active'",
                (recording_id,),
            ).fetchone()
            if row is None:
                raise LookupError("recording is no longer available")
            connection.execute(
                "UPDATE recordings SET state = 'purged' WHERE id = ?", (recording_id,)
            )
            self._audit(
                connection,
                username,
                "permanent_delete",
                recording_id,
                row["filename"],
                "deleted",
            )

    def audit_failure(
        self, username: str, action: str, recording_id: int | None, filename: str, detail: str
    ) -> None:
        with self.transaction(immediate=True) as connection:
            self._audit(connection, username, action, recording_id, filename, detail[:500])

    def audit_event(self, username: str, action: str, detail: str) -> None:
        """Record an administrative action (settings changes, restarts) in the audit log."""
        with self.transaction(immediate=True) as connection:
            self._audit(connection, username, action, None, None, detail[:500])

    def store_event(
        self,
        *,
        started_ns: int,
        ended_ns: int,
        peak_amplitude: int,
        peak_ratio: float,
        station_id: str | None,
        maximum_events: int,
    ) -> None:
        """Persist one detected event and keep only the newest ``maximum_events`` rows."""
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO events "
                "(started_ns, ended_ns, peak_amplitude, peak_ratio, station_id, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    started_ns,
                    ended_ns,
                    peak_amplitude,
                    peak_ratio,
                    station_id,
                    _now(),
                ),
            )
            connection.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY started_ns DESC, id DESC LIMIT ?)",
                (maximum_events,),
            )

    def recent_events(
        self,
        limit: int = 50,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> list[sqlite3.Row]:
        limit = max(1, min(limit, 500))
        query = "SELECT * FROM events WHERE 1=1"
        parameters: list[Any] = []
        if start_ns is not None:
            query += " AND started_ns >= ?"
            parameters.append(start_ns)
        if end_ns is not None:
            query += " AND started_ns < ?"
            parameters.append(end_ns)
        query += " ORDER BY started_ns DESC, id DESC LIMIT ?"
        parameters.append(limit)
        with self.reader() as connection:
            return list(connection.execute(query, parameters))

    def delete_event(self, event_id: int, username: str) -> bool:
        """Remove one detected event (for example a false trigger); audited.

        Only the catalog row is removed — the underlying recording is untouched.
        """
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT started_ns FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
            self._audit(
                connection,
                username,
                "event_deleted",
                None,
                None,
                f"removed event {event_id} (started_ns {row['started_ns']})",
            )
        return True

    def event_summary(self, today_start_ns: int) -> dict[str, Any]:
        """Counts and the latest event for the overview card."""
        with self.reader() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            today = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE started_ns >= ?", (today_start_ns,)
                ).fetchone()[0]
            )
            last = connection.execute(
                "SELECT started_ns, peak_amplitude FROM events "
                "ORDER BY started_ns DESC, id DESC LIMIT 1"
            ).fetchone()
        return {
            "total": total,
            "today": today,
            "last_started_ns": int(last["started_ns"]) if last is not None else None,
            "last_peak": int(last["peak_amplitude"]) if last is not None else None,
        }

    def latest_dayplot_day(self) -> str | None:
        """Newest UTC day whose three cached dayplot channels are all present."""
        with self.reader() as connection:
            row = connection.execute(
                "SELECT utc_day FROM dayplot_cache GROUP BY utc_day "
                "HAVING COUNT(*) = 3 ORDER BY utc_day DESC LIMIT 1"
            ).fetchone()
        return str(row["utc_day"]) if row is not None else None

    def day_coverage_ns(self, utc_day: str) -> int:
        """Nanoseconds of the UTC day covered by valid, finalized recordings."""
        day_start = int(
            datetime.combine(date.fromisoformat(utc_day), datetime.min.time(), UTC).timestamp()
            * 1e9
        )
        return self.coverage_between(day_start, day_start + 86_400_000_000_000)

    def coverage_between(self, start_ns: int, end_ns: int) -> int:
        """Covered nanoseconds in [start_ns, end_ns) from valid active recordings.

        Overlapping recordings are merged before summing, so double-counted time is
        impossible and the value is a true data-continuity measure.
        """
        with self.reader() as connection:
            rows = connection.execute(
                "SELECT start_ns, end_ns FROM recordings WHERE state = 'active' "
                "AND validation_status = 'valid' "
                "AND start_ns IS NOT NULL AND end_ns IS NOT NULL "
                "AND start_ns < ? AND end_ns > ?",
                (end_ns, start_ns),
            ).fetchall()
        intervals = sorted(
            (max(int(row["start_ns"]), start_ns), min(int(row["end_ns"]), end_ns))
            for row in rows
        )
        covered = 0
        current_start: int | None = None
        current_end = 0
        for start, end in intervals:
            if end <= start:
                continue
            if current_start is None or start > current_end:
                if current_start is not None:
                    covered += current_end - current_start
                current_start, current_end = start, end
            elif end > current_end:
                current_end = end
        if current_start is not None:
            covered += current_end - current_start
        return max(0, covered)

    def count_events_in_window(self, start_ns: int, end_ns: int) -> int:
        """Detected events whose start falls in [start_ns, end_ns)."""
        with self.reader() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM events WHERE started_ns >= ? AND started_ns < ?",
                (start_ns, end_ns),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def count_removed_in_window(self, start_iso: str, end_iso: str) -> int:
        """Audit-log count of events the operator removed in a UTC time window."""
        with self.reader() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = 'event_deleted' "
                "AND occurred_at >= ? AND occurred_at < ?",
                (start_iso, end_iso),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        username: str,
        action: str,
        recording_id: int | None,
        filename: str | None,
        detail: str,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_log "
            "(occurred_at, username, action, recording_id, filename, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), username, action, recording_id, filename, detail),
        )

    def source_fingerprint(self, utc_day: str) -> str:
        records = self.records_for_day(utc_day)
        if not records:
            raise LookupError("no validated recordings are available for that day")
        material = [
            {
                "id": row["id"],
                "path": row["relative_path"],
                "size": row["size_bytes"],
                "mtime": row["mtime_ns"],
                "start": row["start_ns"],
                "end": row["end_ns"],
                "samples": row["sample_count"],
                "blocks": row["block_count"],
                "gaps": row["sequence_gap_count"],
            }
            for row in records
        ]
        encoded = json.dumps(material, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def queue_dayplot(self, utc_day: str, *, force: bool = False) -> tuple[int, str]:
        fingerprint = self.source_fingerprint(utc_day)
        with self.transaction(immediate=True) as connection:
            cached = connection.execute(
                "SELECT COUNT(*) FROM dayplot_cache WHERE utc_day = ? AND source_fingerprint = ?",
                (utc_day, fingerprint),
            ).fetchone()[0]
            if cached == 3 and not force:
                raise FileExistsError("an unchanged cached dayplot already exists")
            duplicate = connection.execute(
                "SELECT id, state FROM dayplot_jobs WHERE utc_day = ? AND source_fingerprint = ? "
                "AND state IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
                (utc_day, fingerprint),
            ).fetchone()
            if duplicate is not None:
                return int(duplicate["id"]), str(duplicate["state"])
            cursor = connection.execute(
                "INSERT INTO dayplot_jobs "
                "(utc_day, source_fingerprint, state, progress, requested_at) "
                "VALUES (?, ?, 'queued', 'Waiting for worker', ?)",
                (utc_day, fingerprint, _now()),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the queued job ID")
            return cursor.lastrowid, "queued"

    def queue_all_dayplots(self) -> list[str]:
        """Queue a forced dayplot job for every indexed day.

        The single-consumer worker regenerates the plots one by one. Days whose sources
        have no validated recordings are skipped; days with an already queued or running
        job keep that job instead of receiving a duplicate.
        """
        queued: list[str] = []
        for day in self.days():
            try:
                self.queue_dayplot(day, force=True)
            except LookupError:
                continue
            queued.append(day)
        return queued

    def latest_job(self, utc_day: str) -> sqlite3.Row | None:
        with self.reader() as connection:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM dayplot_jobs WHERE utc_day = ? ORDER BY id DESC LIMIT 1",
                    (utc_day,),
                ).fetchone(),
            )

    def cached_dayplot(self, utc_day: str) -> dict[str, sqlite3.Row]:
        try:
            fingerprint = self.source_fingerprint(utc_day)
        except LookupError:
            return {}
        with self.reader() as connection:
            rows = list(
                connection.execute(
                "SELECT * FROM dayplot_cache WHERE utc_day = ? AND source_fingerprint = ?",
                (utc_day, fingerprint),
                )
            )
            if len(rows) != 3 and date.fromisoformat(utc_day) == datetime.now(UTC).date():
                latest = connection.execute(
                    "SELECT source_fingerprint FROM dayplot_cache WHERE utc_day = ? "
                    "GROUP BY source_fingerprint HAVING COUNT(*) = 3 "
                    "ORDER BY MAX(created_at) DESC LIMIT 1",
                    (utc_day,),
                ).fetchone()
                if latest is not None:
                    rows = list(
                        connection.execute(
                            "SELECT * FROM dayplot_cache WHERE utc_day = ? "
                            "AND source_fingerprint = ?",
                            (utc_day, latest["source_fingerprint"]),
                        )
                    )
            return {str(row["channel"]): row for row in rows}

    def recover_running_jobs(self) -> int:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE dayplot_jobs SET state = 'queued', "
                "progress = 'Recovered after worker restart', "
                "started_at = NULL WHERE state = 'running'"
            )
            return cursor.rowcount

    def claim_job(self) -> sqlite3.Row | None:
        with self.transaction(immediate=True) as connection:
            if connection.execute(
                "SELECT 1 FROM dayplot_jobs WHERE state = 'running' LIMIT 1"
            ).fetchone():
                return None
            row = connection.execute(
                "SELECT * FROM dayplot_jobs WHERE state = 'queued' "
                "ORDER BY requested_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE dayplot_jobs SET state = 'running', progress = 'Starting North', "
                "started_at = ?, error_message = NULL WHERE id = ? AND state = 'queued'",
                (_now(), row["id"]),
            )
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM dayplot_jobs WHERE id = ?", (row["id"],)
                ).fetchone(),
            )

    def update_job(self, job_id: int, state: str, progress: str, error: str | None = None) -> None:
        finished = _now() if state in {"completed", "failed"} else None
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE dayplot_jobs SET state = ?, progress = ?, error_message = ?, "
                "finished_at = COALESCE(?, finished_at) WHERE id = ?",
                (state, progress[:200], error[:1000] if error else None, finished, job_id),
            )

    def job(self, job_id: int) -> sqlite3.Row | None:
        with self.reader() as connection:
            return cast(
                sqlite3.Row | None,
                connection.execute("SELECT * FROM dayplot_jobs WHERE id = ?", (job_id,)).fetchone(),
            )

    def complete_dayplot(self, job_id: int, paths: dict[str, str]) -> None:
        with self.transaction(immediate=True) as connection:
            job = connection.execute(
                "SELECT * FROM dayplot_jobs WHERE id = ? AND state = 'running'", (job_id,)
            ).fetchone()
            if job is None:
                raise LookupError("dayplot job is no longer running")
            for channel, relative_path in paths.items():
                connection.execute(
                    "INSERT OR REPLACE INTO dayplot_cache "
                    "(utc_day, source_fingerprint, channel, relative_path, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (job["utc_day"], job["source_fingerprint"], channel, relative_path, _now()),
                )
            connection.execute(
                "UPDATE dayplot_jobs SET state = 'completed', progress = 'All channels complete', "
                "finished_at = ? WHERE id = ?",
                (_now(), job_id),
            )


def adjacent_days(days: list[str], selected: str) -> tuple[str | None, str | None]:
    if selected not in days:
        return None, None
    index = days.index(selected)
    return (
        days[index - 1] if index > 0 else None,
        days[index + 1] if index + 1 < len(days) else None,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
