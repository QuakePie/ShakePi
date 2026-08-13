"""Server-rendered ShakePie archive application factory."""

from __future__ import annotations

import array as array_module
import io
import math
import subprocess
import wave
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from queue import Queue
from threading import Thread
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash
from werkzeug.wrappers import Response

from shakepie_archive import recorder_settings
from shakepie_archive.analysis import hodogram, hv_ratio
from shakepie_archive.archive_reader import (
    _SAMPLE,
    Block,
    RecordingError,
    read_recording,
)
from shakepie_archive.catalog import Catalog, adjacent_days
from shakepie_archive.config import Settings, load_settings, prepare_filesystem
from shakepie_archive.constants import NOMINAL_SAMPLE_RATE_HZ
from shakepie_archive.deletion import delete_recording
from shakepie_archive.detector import DetectorConfig, EventDetectorWorker
from shakepie_archive.downloads import UnsafeFileError, open_catalog_recording
from shakepie_archive.health import read_health, soc_temperature, status_snapshot
from shakepie_archive.live_data import LiveStreamBuffer
from shakepie_archive.miniseed import SeriesCollector, encode_all
from shakepie_archive.notify import BarkNotifier, DailySummaryScheduler, build_daily_summary
from shakepie_archive.recorder_settings import FIELDS, atomic_write
from shakepie_archive.recording_plot import analyze_recording
from shakepie_archive.security import install_security, safe_next
from shakepie_archive.watch import WatchService, WatchStore, validate_point_fields


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or suffix == "TiB":
            return f"{amount:.1f} {suffix}"
        amount /= 1024
    return "—"


def _format_time(value: int | None) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value / 1e9, UTC).strftime("%H:%M:%S")


def _format_datetime(value: int | None) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value / 1e9, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_utc_day(value: int | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(value / 1e9, UTC).strftime("%Y-%m-%d")


def _format_utc_time(value: int | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(value / 1e9, UTC).strftime("%H:%M:%S")


def _format_duration(start: int | None, end: int | None) -> str:
    if start is None or end is None:
        return "—"
    seconds = max(0, (end - start) / 1e9)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds_value = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_value:02d}"


def _selected_day(catalog: Catalog) -> tuple[list[str], str | None]:
    days = catalog.days()
    requested = request.args.get("date")
    if requested:
        try:
            date.fromisoformat(requested)
        except ValueError:
            abort(400, description="Invalid UTC date")
    selected = requested if requested in days else (days[-1] if days else None)
    return days, selected


def _load_recorder_admin(settings: Settings) -> tuple[str | None, dict[str, str]]:
    """Read the recorder configuration for the settings page; text is None when unreadable."""
    try:
        text = settings.recorder_admin.recorder_conf_path.read_text(encoding="utf-8")
    except OSError:
        return None, {}
    return text, recorder_settings.parse_recorder_conf(text)


def _configured_group_rate(settings: Settings) -> int:
    """The group rate the recorder is configured for (health.json omits it)."""
    try:
        values = recorder_settings.parse_recorder_conf(
            settings.recorder_admin.recorder_conf_path.read_text(encoding="utf-8")
        )
        return int(values["acquisition.nominal_group_rate_hz"])
    except (OSError, KeyError, ValueError):
        return NOMINAL_SAMPLE_RATE_HZ


def build_daily_summary_scheduler(
    configured: Settings, notifier: BarkNotifier, catalog: Catalog
) -> DailySummaryScheduler:
    """Nightly Bark report at Beijing midnight: events, coverage, disk, recorder state."""

    def build() -> tuple[str, str]:
        health, _error, _stale = read_health(configured.health_file, 45)
        state = str(health.get("state", "unavailable")) if health else "unavailable"
        return build_daily_summary(
            catalog,
            configured.archive.data_directory,
            _configured_group_rate(configured),
            state,
            soc_temperature(),
            datetime.now(UTC),
        )

    return DailySummaryScheduler(build, configured.notify.summary_hour, notifier)


# Bumped whenever a static asset changes so browsers fetch the fresh copy
# (static files are cache-revalidated, and the version query forces it).
STATIC_VERSION = "11"

# Upper bound for one in-memory miniSEED export: 4M sample groups (~5.5 h at
# 200 Hz, ~50 MB of transient buffers) keeps gunicorn's memory envelope safe.
MAX_MINISEED_SAMPLES = 4_000_000


def create_app(
    settings: Settings | None = None,
    *,
    prepare: bool = True,
    live_source: LiveStreamBuffer | None = None,
    detector_worker: EventDetectorWorker | None = None,
) -> Flask:
    configured = settings or load_settings()
    if prepare:
        prepare_filesystem(configured)
    app = Flask(__name__)
    app.config["SETTINGS"] = configured
    catalog = Catalog(configured.archive.catalog_path)
    catalog.initialize()
    app.extensions["shakepie_catalog"] = catalog
    live = live_source or LiveStreamBuffer(configured.live)
    app.extensions["shakepie_live"] = live
    notifier: BarkNotifier | None = None
    if configured.notify.enabled and configured.notify.bark_key:
        notifier = BarkNotifier(
            configured.notify.bark_key,
            configured.notify.bark_server,
            configured.notify.rate_limit_seconds,
        )
        notifier.start()
    if detector_worker is not None:
        detector = detector_worker
    elif configured.event_detector.enabled:
        detector = EventDetectorWorker(
            DetectorConfig(
                sta_seconds=configured.event_detector.sta_seconds,
                lta_seconds=configured.event_detector.lta_seconds,
                threshold=configured.event_detector.threshold,
                off_seconds=configured.event_detector.off_seconds,
                cooldown_seconds=configured.event_detector.cooldown_seconds,
                maximum_events=configured.event_detector.maximum_events,
                minimum_components=configured.event_detector.minimum_components,
            ),
            configured.live.host,
            configured.live.port,
            catalog,
            lambda: str(
                (catalog.latest_recording() or {"station_id": None})["station_id"] or "SHAKEPIE"
            ),
            configured.event_detector.maximum_events,
            notifier=notifier,
        )
        if notifier is not None and configured.notify.daily_summary:
            summary_scheduler = build_daily_summary_scheduler(configured, notifier, catalog)
            summary_scheduler.start()
            app.extensions["shakepie_daily_summary"] = summary_scheduler
    else:
        detector = None
    if detector is not None:
        detector.start()
    app.extensions["shakepie_detector"] = detector
    watch_store = WatchStore(configured.watch.state_path)
    watch_service: WatchService | None = None
    if configured.watch.enabled:
        watch_service = WatchService(
            watch_store,
            notifier,
            usgs_base=configured.watch.usgs_base,
            isc_base=configured.watch.isc_base,
            cenc_base=configured.watch.cenc_base,
            timeout=float(configured.watch.timeout_seconds),
        )
        watch_service.start()
    app.extensions["shakepie_watch_service"] = watch_service
    limiter = install_security(app, configured.security)
    app.jinja_env.filters.update(
        format_bytes=_format_bytes,
        format_time=_format_time,
        format_datetime=_format_datetime,
        format_duration=_format_duration,
        utc_day=_format_utc_day,
        utc_time=_format_utc_time,
    )
    app.jinja_env.globals["static_version"] = STATIC_VERSION

    @app.get("/login")
    def login() -> str:
        return render_template("login.html", next=safe_next(request.args.get("next")))

    @app.get("/health")
    def health() -> Response:
        return jsonify({"status": "ok"})

    @app.post("/login")
    def login_submit() -> Response:
        address = request.remote_addr or "unknown"
        if not limiter.allowed(address):
            abort(429, description="Too many login attempts. Try again later.")
        if not check_password_hash(
            configured.security.password_hash, request.form.get("password", "")
        ):
            limiter.failed(address)
            flash("The password was not accepted.", "error")
            return redirect(url_for("login", next=safe_next(request.form.get("next"))))
        limiter.clear(address)
        session.clear()
        session["authenticated"] = True
        session["username"] = "admin"
        session.permanent = True
        return redirect(safe_next(request.form.get("next")))

    @app.post("/logout")
    def logout() -> Response:
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def overview() -> str:
        return render_template(
            "index.html",
            status=status_snapshot(
                configured, catalog, app.extensions["shakepie_detector"]
            ),
            refresh_seconds=configured.web.status_refresh_seconds,
        )

    @app.get("/api/status")
    def status_api() -> Response:
        return jsonify(
            status_snapshot(configured, catalog, app.extensions["shakepie_detector"])
        )

    @app.get("/events")
    def events_page() -> str:
        """List detected events with day filtering.

        ``?date=YYYY-MM-DD`` shows exactly one UTC day; ``?range=today|yesterday|7|30``
        shows a rolling window; no parameter shows all stored events (newest first).
        """
        now = datetime.now(UTC)
        date_text = request.args.get("date", "")
        range_choice = request.args.get("range", "all")
        start_ns: int | None = None
        end_ns: int | None = None
        active_filter = (
            range_choice if range_choice in {"all", "today", "yesterday", "7", "30"} else "all"
        )
        if date_text:
            try:
                selected = date.fromisoformat(date_text)
            except ValueError:
                abort(400, description="Invalid UTC date")
            day_start = datetime.combine(selected, datetime.min.time(), UTC)
            start_ns = int(day_start.timestamp() * 1e9)
            end_ns = start_ns + 86_400_000_000_000
            active_filter = "date"
        elif range_choice == "today":
            day_start = datetime.combine(now.date(), datetime.min.time(), UTC)
            start_ns = int(day_start.timestamp() * 1e9)
        elif range_choice == "yesterday":
            day_start = datetime.combine(now.date(), datetime.min.time(), UTC)
            start_ns = int(day_start.timestamp() * 1e9) - 86_400_000_000_000
            end_ns = start_ns + 86_400_000_000_000
        elif range_choice in {"7", "30"}:
            start_ns = int((now - timedelta(days=int(range_choice))).timestamp() * 1e9)
        events = catalog.recent_events(limit=100, start_ns=start_ns, end_ns=end_ns)
        return render_template(
            "events.html",
            events=events,
            active_filter=active_filter,
            date_filter=date_text,
        )

    @app.post("/events/<int:event_id>/delete")
    def delete_event(event_id: int) -> Response:
        """Remove one detected event from the list (for example a false trigger).

        Only the catalog entry is deleted; the underlying recording is untouched, so
        this cannot lose recorded data.
        """
        username = str(session.get("username", "unknown"))
        removed = catalog.delete_event(event_id, username)
        if removed:
            flash("The event was removed from the list.", "success")
        else:
            flash("That event is no longer in the list.", "error")
        return redirect(url_for("events_page"))

    @app.post("/events/delete-bulk")
    def delete_events_bulk() -> Response:
        """Remove several detected events at once (multi-select on the events page).

        Unknown or already-removed ids are silently skipped; the response reports how
        many entries were actually deleted. Only catalog entries are removed.
        """
        payload = request.get_json(silent=True) or {}
        raw_ids = payload.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            abort(400, description="No event ids supplied")
        try:
            ids = [int(value) for value in raw_ids][:500]
        except (TypeError, ValueError):
            abort(400, description="Invalid event ids")
        username = str(session.get("username", "unknown"))
        removed = sum(catalog.delete_event(event_id, username) for event_id in ids)
        return jsonify({"removed": removed, "requested": len(ids)})

    @app.get("/watch")
    def watch_page() -> str:
        edit_name = request.args.get("edit", "")
        selected_point = request.args.get("point", "")
        points = watch_store.points()
        edit_values = watch_store.get_point(edit_name) if edit_name else None
        point_names = [point.name for point in points]
        if selected_point and selected_point not in point_names:
            selected_point = ""
        quakes = watch_store.quakes(selected_point or None)
        return render_template(
            "watch.html",
            points=points,
            point_names=point_names,
            last_checks={point.name: watch_store.last_check(point.name) for point in points},
            quakes=quakes,
            interval_hours=watch_store.interval_hours(),
            edit_values=edit_values,
            selected_point=selected_point,
            watch_enabled=configured.watch.enabled,
            service=watch_service,
        )

    @app.post("/watch/points")
    def watch_points_save() -> Response:
        if not configured.watch.enabled:
            abort(404)
        errors, point = validate_point_fields(
            request.form.get("name", "").strip(),
            request.form.get("latitude", ""),
            request.form.get("longitude", ""),
            request.form.get("radius_km", ""),
            request.form.get("min_magnitude", "0"),
            request.form.get("sources", "both"),
        )
        if errors:
            for message in errors:
                flash(message, "error")
            return redirect(url_for("watch_page"))
        assert point is not None
        original = request.form.get("original", "").strip()
        if original and original != point.name:
            watch_store.delete_point(original)
        watch_store.upsert_point(point)
        catalog.audit_event(
            str(session.get("username", "unknown")),
            "watch_point_saved",
            f"{point.name} lat={point.latitude} lon={point.longitude} "
            f"radius={point.radius_km}km sources={point.sources}",
        )
        if watch_service is not None:
            watch_service.request_check()
        flash(f"Watch point {point.name} saved. A first check runs shortly.", "success")
        return redirect(url_for("watch_page"))

    @app.post("/watch/points/delete")
    def watch_points_delete() -> Response:
        if not configured.watch.enabled:
            abort(404)
        name = (request.form.get("name") or request.args.get("name") or "").strip()
        if watch_store.delete_point(name):
            catalog.audit_event(
                str(session.get("username", "unknown")), "watch_point_deleted", name
            )
            flash(f"Watch point {name} deleted.", "success")
        else:
            flash("That watch point no longer exists.", "error")
        return redirect(url_for("watch_page"))

    @app.post("/watch/interval")
    def watch_interval() -> Response:
        if not configured.watch.enabled:
            abort(404)
        try:
            hours = int(request.form.get("hours", "1"))
            if not 1 <= hours <= 168:
                raise ValueError
        except ValueError:
            flash("Check interval must be between 1 and 168 hours.", "error")
            return redirect(url_for("watch_page"))
        watch_store.set_interval_hours(hours)
        if watch_service is not None:
            watch_service.request_check()
        flash(f"Check interval set to {hours} hour(s).", "success")
        return redirect(url_for("watch_page"))

    @app.post("/watch/check")
    def watch_check() -> Response:
        if not configured.watch.enabled or watch_service is None:
            flash("Earthquake watch is disabled.", "error")
            return redirect(url_for("watch_page"))
        watch_service.request_check()
        flash("Check started — results appear on this page within a few seconds.", "success")
        return redirect(url_for("watch_page"))

    @app.get("/api/days")
    @app.get("/api/days")
    def days_api() -> Response:
        return jsonify(catalog.available_day_counts())

    @app.get("/live")
    def live_page() -> str:
        return render_template(
            "live.html",
            enabled=configured.live.enabled,
            history_seconds=configured.live.history_seconds,
            refresh_milliseconds=configured.live.refresh_milliseconds,
            frequency_max_hz=configured.live.frequency_max_hz,
            sample_rate_hz=configured.live.sample_rate_hz,
        )

    @app.get("/kiosk")
    def kiosk_page() -> str:
        """Full-screen wall display: live waveforms, a UTC clock, and station status."""
        return render_template(
            "kiosk.html",
            live_enabled=configured.live.enabled,
            history_seconds=configured.live.history_seconds,
            refresh_milliseconds=configured.live.refresh_milliseconds,
            sample_rate_hz=configured.live.sample_rate_hz,
        )

    @app.get("/api/live")
    def live_api() -> Response | tuple[Response, int]:
        if not configured.live.enabled:
            return jsonify({"state": "disabled", "error": "Live data is disabled"}), 503
        after_text = request.args.get("after_sequence")
        after_sequence: int | None = None
        if after_text:
            try:
                after_sequence = int(after_text)
            except ValueError:
                abort(400, description="Invalid live sequence")
            if after_sequence < 0:
                abort(400, description="Invalid live sequence")
        rate_text = request.args.get("sample_rate", str(configured.live.sample_rate_hz))
        try:
            sample_rate_hz = int(rate_text)
        except ValueError:
            abort(400, description="Invalid live sample rate")
        if sample_rate_hz != configured.live.sample_rate_hz:
            abort(
                400,
                description=f"Live sample rate must be {configured.live.sample_rate_hz} Hz",
            )
        active_live: LiveStreamBuffer = app.extensions["shakepie_live"]
        return jsonify(active_live.snapshot(after_sequence, sample_rate_hz))

    @app.get("/archive")
    def archive() -> str:
        days, selected = _selected_day(catalog)
        if selected is None:
            return render_template(
                "archive.html", days=days, selected=None, page=None, summary=None
            )
        try:
            page_number = max(1, int(request.args.get("page", "1")))
        except ValueError:
            abort(400, description="Invalid page number")
        page = catalog.recordings_for_day(selected, page_number)
        previous, following = adjacent_days(days, selected)
        return render_template(
            "archive.html",
            days=days,
            selected=selected,
            page=page,
            summary=catalog.day_summary(selected),
            previous=previous,
            following=following,
            latest=days[-1],
        )

    @app.get("/recorded")
    def recorded_page() -> str:
        days, selected = _selected_day(catalog)
        return render_template(
            "recorded.html",
            days=days,
            selected=selected,
            requested_recording=request.args.get("recording", ""),
            requested_at=request.args.get("at", ""),
            frequency_max_hz=configured.recorded_plot.frequency_max_hz,
        )

    @app.get("/api/recordings")
    def recording_options_api() -> Response:
        utc_day = request.args.get("date", "")
        try:
            date.fromisoformat(utc_day)
        except ValueError:
            abort(400, description="Invalid UTC date")
        rows = catalog.records_for_day(utc_day)
        return jsonify(
            [
                {
                    "id": int(row["id"]),
                    "filename": str(row["filename"]),
                    "start": _format_datetime(int(row["start_ns"])),
                    "end": _format_datetime(int(row["end_ns"])),
                    "duration": _format_duration(int(row["start_ns"]), int(row["end_ns"])),
                    "size": _format_bytes(int(row["size_bytes"])),
                    "samples": int(row["sample_count"]),
                    "rate_hz": (
                        int(row["sample_rate_millihz"]) / 1000.0
                        if row["sample_rate_millihz"] is not None
                        else None
                    ),
                    "gaps": int(row["sequence_gap_count"]),
                    "validation": str(row["validation_status"]).title(),
                }
                for row in rows
            ]
        )

    @app.get("/api/recordings/<int:recording_id>/plot")
    def recording_plot_api(recording_id: int) -> Response | tuple[Response, int]:
        row = catalog.recording(recording_id, state="active")
        if row is None or row["validation_status"] != "valid":
            abort(404)
        try:
            result = analyze_recording(
                configured.archive.data_directory,
                row,
                configured.recorded_plot,
            )
        except (RecordingError, UnsafeFileError) as error:
            return jsonify({"error": str(error)}), 409
        return jsonify(result)

    @app.get("/recordings/<int:recording_id>/download")
    def download(recording_id: int) -> Response:
        row = catalog.recording(recording_id, state="active")
        if row is None or row["validation_status"] != "valid":
            abort(404)
        try:
            opened = open_catalog_recording(configured.archive.data_directory, row)
        except UnsafeFileError as error:
            abort(409, description=str(error))
        response = send_file(
            opened.handle,
            mimetype="application/vnd.shakepie.recording",
            as_attachment=True,
            download_name=opened.filename,
            conditional=True,
            etag=False,
            last_modified=opened.mtime,
        )
        response.content_length = opened.size_bytes
        response.call_on_close(opened.handle.close)
        return response

    @app.get("/recordings/<int:recording_id>/audio")
    def recording_audio(recording_id: int) -> Response:
        """Play one recording as sound ("audification").

        The seismometer records below human hearing, so the data is sped up ~40x into
        the audible range and played at 8 kHz. Peak-normalized to 80% so every recording
        plays at the same safe loudness, deterministic for the same file. Bounded to
        60 s of audio; only catalog-selected, valid files are accepted.
        """
        channel_param = request.args.get("channel", "north")
        channel_index = {"north": 0, "east": 1, "vertical": 2}.get(channel_param)
        if channel_index is None:
            abort(400, description="Unknown channel; use north, east, or vertical")
        row = catalog.recording(recording_id, state="active")
        if row is None or row["validation_status"] != "valid":
            abort(404)
        try:
            opened = open_catalog_recording(configured.archive.data_directory, row)
        except UnsafeFileError as error:
            abort(409, description=str(error))
        opened.handle.close()
        candidate = configured.archive.data_directory / str(row["relative_path"])

        nominal_hz = int(row["sample_rate_millihz"] or 200_000) / 1000.0
        decim = max(1, round(nominal_hz / 200.0))  # 200 Hz audio-equivalent speed
        audio_rate = 8000
        # cap the sound at one minute on very long recordings
        if row["sample_count"]:
            decim = max(decim, math.ceil(row["sample_count"] / (audio_rate * 60)))
        kept: list[int] = []
        peak = 0
        seen = 0

        def on_block(block: Block) -> None:
            nonlocal peak, seen
            for sample in _SAMPLE.iter_unpack(block.payload):
                if seen % decim == 0:
                    value = sample[channel_index]
                    kept.append(value)
                    magnitude = abs(value)
                    if magnitude > peak:
                        peak = magnitude
                seen += 1

        metadata = read_recording(candidate, on_block)
        expected = (
            int(row["device"]),
            int(row["inode"]),
            int(row["size_bytes"]),
            int(row["mtime_ns"]),
        )
        if (
            metadata.device,
            metadata.inode,
            metadata.size_bytes,
            metadata.mtime_ns,
        ) != expected:
            abort(409, description="Recording changed while it was being read")

        scale = 0.8 * 32_767 / peak if peak else 1.0
        pcm = array_module.array("h", (round(value * scale) for value in kept))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(audio_rate)
            wav.writeframes(pcm.tobytes())
        buffer.seek(0)
        stem = str(row["filename"]).rsplit(".", 1)[0]
        return send_file(
            buffer,
            mimetype="audio/wav",
            as_attachment=False,
            download_name=f"{stem}_{channel_param}.wav",
        )

    @app.get("/recordings/<int:recording_id>/miniseed")
    def recording_miniseed(recording_id: int) -> Response:
        """Export one validated recording as miniSEED 2.4 for external tools.

        Channels are EHN/EHE/EHZ with lossless INT32 counts in 512-byte records.
        Sample-sequence gaps and clock steps split the export into separate record
        series, each carrying its own start time, so receivers see truthful timing.
        """
        channel_param = request.args.get("channel", "all")
        wanted = {
            "all": ("north", "east", "vertical"),
            "north": ("north",),
            "east": ("east",),
            "vertical": ("vertical",),
        }.get(channel_param)
        if wanted is None:
            abort(400, description="Unknown channel; use all, north, east, or vertical")
        row = catalog.recording(recording_id, state="active")
        if row is None or row["validation_status"] != "valid":
            abort(404)
        if int(row["sample_count"]) > MAX_MINISEED_SAMPLES:
            abort(409, description="Recording is too large for a single miniSEED export")
        try:
            opened = open_catalog_recording(configured.archive.data_directory, row)
            opened.handle.close()
        except UnsafeFileError as error:
            abort(409, description=str(error))
        candidate = configured.archive.data_directory / str(row["relative_path"])

        collector = SeriesCollector(wanted)
        metadata = read_recording(candidate, collector.add_block)
        if (
            metadata.device,
            metadata.inode,
            metadata.size_bytes,
            metadata.mtime_ns,
        ) != (
            int(row["device"]),
            int(row["inode"]),
            int(row["size_bytes"]),
            int(row["mtime_ns"]),
        ):
            abort(409, description="Recording changed while it was being read")

        station = metadata.station_id.upper()[:5] or "SHAKE"
        buffer = io.BytesIO(encode_all(collector.finish(), station))
        stem = str(row["filename"]).rsplit(".", 1)[0]
        suffix = "" if channel_param == "all" else f"_{channel_param}"
        return send_file(
            buffer,
            mimetype="application/vnd.fdsn.mseed",
            as_attachment=True,
            download_name=f"{stem}{suffix}.miniseed",
        )

    def _analysis_row(recording_id: int) -> Any:
        row = catalog.recording(recording_id, state="active")
        if row is None or row["validation_status"] != "valid":
            abort(404)
        return row

    @app.get("/recordings/<int:recording_id>/hodogram")
    def recording_hodogram(recording_id: int) -> Response | tuple[Response, int]:
        """North-East particle motion for a window, with polarization azimuth."""
        row = _analysis_row(recording_id)
        try:
            start_seconds = float(request.args.get("start", 0.0))
            window_seconds = float(request.args.get("seconds", 60.0))
            max_points = int(request.args.get("max_points", 4000))
        except ValueError:
            abort(400, description="start, seconds, and max_points must be numeric")
        try:
            return jsonify(
                hodogram(
                    configured.archive.data_directory,
                    row,
                    start_seconds=start_seconds,
                    window_seconds=window_seconds,
                    max_points=max_points,
                )
            )
        except (RecordingError, UnsafeFileError) as error:
            return jsonify({"error": str(error)}), 409

    @app.get("/recordings/<int:recording_id>/hv")
    def recording_hv(recording_id: int) -> Response | tuple[Response, int]:
        """H/V ambient-noise spectral ratio for the whole recording."""
        row = _analysis_row(recording_id)
        try:
            return jsonify(hv_ratio(configured.archive.data_directory, row))
        except (RecordingError, UnsafeFileError) as error:
            return jsonify({"error": str(error)}), 409

    @app.post("/recordings/<int:recording_id>/delete")
    def permanently_delete_recording(recording_id: int) -> Response:
        row = catalog.recording(recording_id, state="active")
        if row is None:
            flash("That recording is no longer available.", "error")
            return redirect(url_for("archive"))
        if request.form.get("confirm_filename") != row["filename"]:
            catalog.audit_failure(
                str(session["username"]),
                "permanent_delete_denied",
                recording_id,
                str(row["filename"]),
                "filename confirmation did not match the selected recording",
            )
            flash("Deletion confirmation did not match the selected recording.", "error")
            return redirect(url_for("archive", date=row["utc_day"]))
        try:
            delete_recording(
                catalog,
                configured.archive.data_directory,
                row,
                str(session["username"]),
            )
        except (OSError, LookupError, UnsafeFileError) as error:
            catalog.audit_failure(
                str(session["username"]),
                "permanent_delete_failed",
                recording_id,
                str(row["filename"]),
                str(error),
            )
            flash(str(error), "error")
        else:
            flash(f"{row['filename']} was permanently deleted.", "success")
        return redirect(url_for("archive", date=row["utc_day"]))

    @app.get("/recordings/<int:recording_id>/export")
    def export_recording(recording_id: int) -> Response:
        """Stream one validated recording as timestamped CSV (AnyShake-style export)."""
        row = catalog.recording(recording_id, state="active")
        if row is None or row["validation_status"] != "valid":
            abort(404)
        try:
            opened = open_catalog_recording(configured.archive.data_directory, row)
            opened.handle.close()
        except UnsafeFileError as error:
            abort(409, description=str(error))
        candidate = configured.archive.data_directory / str(row["relative_path"])

        def generate() -> Iterator[str]:
            """Stream CSV rows with bounded memory; a worker thread parses blocks."""
            lines: Queue[object] = Queue(maxsize=64)

            def worker() -> None:
                try:
                    def on_block(block: Block) -> None:
                        stamp_base = block.utc_start_ns
                        lines.put(
                            [
                                "{},{},{},{}\n".format(
                                    datetime.fromtimestamp(
                                        (stamp_base + int(offset)) / 1e9, UTC
                                    ).isoformat(timespec="milliseconds"),
                                    north,
                                    east,
                                    vertical,
                                )
                                for north, east, vertical, offset in _SAMPLE.iter_unpack(
                                    block.payload
                                )
                            ]
                        )

                    read_recording(candidate, on_block)
                except (RecordingError, OSError, UnsafeFileError) as error:
                    lines.put(error)
                lines.put(None)

            Thread(target=worker, name="shakepie-csv-export", daemon=True).start()
            yield "timestamp_utc,north_counts,east_counts,vertical_counts\n"
            while True:
                item = lines.get()
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                assert isinstance(item, list)  # narrowed for the type checker
                yield from item

        download_name = str(row["filename"]).rsplit(".", 1)[0] + ".csv"
        return app.response_class(
            generate(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={download_name}"},
        )

    @app.get("/dayplot")
    def dayplot() -> str:
        days, selected = _selected_day(catalog)
        summary = catalog.day_summary(selected) if selected else None
        return render_template(
            "day.html",
            days=days,
            selected=selected,
            current_day=datetime.now(UTC).date().isoformat(),
            summary=summary,
            interval_minutes=configured.dayplot.interval_minutes,
            cache=catalog.cached_dayplot(selected) if selected else {},
            job=catalog.latest_job(selected) if selected else None,
        )

    @app.post("/dayplot/<utc_day>/generate")
    def request_dayplot(utc_day: str) -> Response:
        try:
            date.fromisoformat(utc_day)
        except ValueError:
            abort(400, description="Invalid UTC date")
        summary = catalog.day_summary(utc_day)
        if not summary["count"]:
            abort(404)
        force = request.form.get("regenerate") == "yes"
        if not summary["complete"] and request.form.get("confirm_incomplete") != "yes":
            flash("Confirm generation for this recovered or incomplete day.", "error")
            return redirect(url_for("dayplot", date=utc_day))
        try:
            job_id, state = catalog.queue_dayplot(utc_day, force=force)
        except (FileExistsError, LookupError) as error:
            flash(str(error), "error")
        else:
            flash(f"Dayplot job {job_id} is {state}.", "success")
        return redirect(url_for("dayplot", date=utc_day))

    @app.post("/dayplot/regenerate-all")
    def regenerate_all_dayplots() -> Response:
        days = catalog.queue_all_dayplots()
        if days:
            flash(
                f"Queued regeneration for {len(days)} day(s); the worker plots them "
                "one by one, oldest first.",
                "success",
            )
        else:
            flash("No indexed recordings are available to regenerate.", "error")
        return redirect(url_for("dayplot"))

    @app.get("/dayplots/<utc_day>/<channel>")
    def dayplot_image(utc_day: str, channel: str) -> Response:
        if channel not in {"north", "east", "vertical"}:
            abort(404)
        row = catalog.cached_dayplot(utc_day).get(channel)
        if row is None:
            abort(404)
        root = configured.dayplot.output_directory.resolve()
        candidate = root / str(row["relative_path"])
        try:
            if candidate.is_symlink():
                abort(404)
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            abort(404)
        if resolved.is_symlink() or not resolved.is_file():
            abort(404)
        return send_file(
            resolved,
            mimetype="image/png" if resolved.suffix == ".png" else "image/jpeg",
            as_attachment=request.args.get("download") == "1",
            download_name=resolved.name,
            conditional=True,
        )

    @app.get("/settings")
    def settings_page() -> str:
        if not configured.recorder_admin.enabled:
            abort(404)
        _, current = _load_recorder_admin(configured)
        return render_template(
            "settings.html",
            fields=FIELDS,
            current=current,
            recorder_conf=str(configured.recorder_admin.recorder_conf_path),
        )

    @app.post("/settings")
    def settings_apply() -> Response:
        if not configured.recorder_admin.enabled:
            abort(404)
        username = str(session.get("username", "unknown"))
        text, current = _load_recorder_admin(configured)
        if text is None:
            flash(
                f"The recorder configuration is not readable at "
                f"{configured.recorder_admin.recorder_conf_path}.",
                "error",
            )
            return redirect(url_for("settings_page"))
        updates = {
            spec.key: value.strip()
            for spec in FIELDS
            if (value := request.form.get(spec.key, "").strip())
            and value.strip() != current.get(spec.key, "")
        }
        if not updates:
            flash("No setting was changed.", "success")
            return redirect(url_for("settings_page"))
        errors = recorder_settings.validate_values(current, updates)
        if errors:
            for message in errors:
                flash(message, "error")
            return redirect(url_for("settings_page"))

        new_group_rate = int(
            updates.get(
                "acquisition.nominal_group_rate_hz",
                current.get("acquisition.nominal_group_rate_hz", "0"),
            )
        )
        rate_changed = (
            "acquisition.nominal_group_rate_hz" in updates or "adc.data_rate_sps" in updates
        )
        services: list[str] = ["seisberry-recorder.service"]
        if rate_changed:
            services += ["shakepie-dayplot-worker.service"]

        try:
            recorder_settings.backup_file(configured.recorder_admin.recorder_conf_path)
            atomic_write(
                configured.recorder_admin.recorder_conf_path,
                recorder_settings.render_recorder_conf(text, updates),
            )
            if rate_changed:
                archive_text = configured.recorder_admin.archive_conf_path.read_text(
                    encoding="utf-8"
                )
                atomic_write(
                    configured.recorder_admin.archive_conf_path,
                    recorder_settings.update_live_rates(archive_text, new_group_rate),
                )
        except OSError as error:
            catalog.audit_event(username, "recorder_settings_failed", str(error))
            flash(f"The configuration could not be written: {error}", "error")
            return redirect(url_for("settings_page"))

        succeeded, failed = recorder_settings.restart_services(tuple(services))
        if rate_changed:
            # Restarting the web service from inside a request would kill the response;
            # let this response finish, then restart through the polkit-allowed unit.
            subprocess.Popen(
                ["/bin/sh", "-c",
                 "sleep 1; exec /usr/bin/systemctl restart shakepie-archive-web.service"],
                start_new_session=True,
            )
        detail = (
            f"updated {', '.join(f'{key}={value}' for key, value in sorted(updates.items()))}; "
            f"restarted {', '.join(succeeded) if succeeded else 'no services'}"
            + (f"; failed: {'; '.join(failed)}" if failed else "")
        )
        catalog.audit_event(username, "recorder_settings_applied", detail)
        if failed:
            flash(
                "The configuration was saved, but a service restart failed: "
                + "; ".join(failed)
                + " Verify the polkit rule for the shakepie-web account.",
                "error",
            )
        else:
            flash(
                "Recorder settings applied"
                + (f" and {', '.join(succeeded)} restarted." if succeeded else ".")
                + " The web interface restarts in a moment when the sample rate changed.",
                "success",
            )
        return redirect(url_for("overview"))

    @app.post("/settings/calibrate")
    def settings_calibrate() -> Response:
        """Ask the recorder to recalibrate its zero level (or clear the offsets).

        The recorder polls the request file and consumes it; recording continues while
        the measurement window runs and the new offsets apply when it completes.
        """
        if not configured.recorder_admin.enabled:
            abort(404)
        mode = request.form.get("mode", "")
        if mode not in {"calibrate", "clear"}:
            abort(400, description="Unknown calibration mode")
        username = str(session.get("username", "unknown"))
        try:
            atomic_write(
                configured.recorder_admin.calibrate_request_path,
                mode + "\n",
            )
        except OSError as error:
            catalog.audit_event(username, "zero_calibration_failed", f"{mode}: {error}")
            flash(
                "The calibration request could not be written. Verify that the recorder "
                "is installed and the request path is writable.",
                "error",
            )
            return redirect(url_for("settings_page"))
        catalog.audit_event(username, "zero_calibration_requested", mode)
        if mode == "calibrate":
            flash(
                "Calibration requested. The recorder measures each channel for a short "
                "window (recording continues) and then records around the new zero "
                "level. Watch the status line below.",
                "success",
            )
        else:
            flash(
                "Clear requested. The recorder stops subtracting offsets; samples are "
                "recorded raw again.",
                "success",
            )
        return redirect(url_for("settings_page"))

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(429)
    def expected_error(error: Any) -> tuple[str, int]:
        return render_template("error.html", error=error), int(error.code)

    @app.errorhandler(Exception)
    def unexpected_error(error: Exception) -> tuple[str, int]:
        app.logger.exception("unhandled archive web error", exc_info=error)
        return render_template("error.html", error=None), 500

    return app
