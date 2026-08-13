"""Bark push notifications for detected events.

When the detector finalizes an event, this module forwards a summary to the Bark
iOS app (a single HTTPS GET against api.day.app). Everything runs on a small
background thread so a slow or unreachable Bark server can never stall the event
detector or drop samples. A rate limit keeps a noisy night from flooding the phone:
suppressed triggers are counted and reported in the next delivered message.

The Bark device key is a secret and arrives through the environment
(``SHAKEPIE_NOTIFY_BARK_KEY``), like the other deployment secrets.
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Callable



DEFAULT_BARK_SERVER = "https://api.day.app"
BEIJING_OFFSET = timedelta(hours=8)


def event_message(event: Any) -> tuple[str, str]:
    """Return (title, body) for one detected event."""
    started = datetime.fromtimestamp(event.started_ns / 1e9, UTC).strftime("%m-%d %H:%M:%S")
    duration = max(0.0, (event.ended_ns - event.started_ns) / 1e9)
    title = "ShakePie event detected"
    body = (
        f"{started} UTC · {duration:.1f}s · "
        f"peak {event.peak_amplitude:,} counts · ratio x{event.peak_ratio:.1f}"
    )
    return title, body


def next_beijing_midnight_utc(now_utc: datetime) -> datetime:
    """The next 00:00 Beijing time (UTC+8, no daylight saving) after ``now_utc``."""
    beijing_now = now_utc + BEIJING_OFFSET
    candidate = datetime.combine(
        beijing_now.date() + timedelta(days=1), datetime.min.time(), UTC
    )
    return candidate - BEIJING_OFFSET


def bark_url(bark_server: str, key: str, title: str, body: str) -> str:
    """Build the request URL; message parts are percent-encoded."""
    server = bark_server.rstrip("/")
    return (
        f"{server}/{quote(key, safe='')}/{quote(title, safe='')}/{quote(body, safe='')}"
        "?group=ShakePie&level=timeSensitive&isArchive=1"
    )


class BarkNotifier:
    """Queued, rate-limited Bark sender running on its own daemon thread."""

    def __init__(
        self,
        bark_key: str,
        bark_server: str = DEFAULT_BARK_SERVER,
        rate_limit_seconds: int = 600,
        *,
        timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sender: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._key = bark_key
        self._server = bark_server
        self._rate_limit_seconds = rate_limit_seconds
        self._timeout = timeout
        self._clock = clock
        self._sender = sender or self._default_sender
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=8)
        self._thread: threading.Thread | None = None
        self._last_sent = float("-inf")
        self._suppressed = 0
        self._stopping = False

    # -- public API (called from the detector thread; never blocks long) --------
    @property
    def configured(self) -> bool:
        return bool(self._key)

    def submit(self, event: Any) -> None:
        """Queue one detected event; rate limited like all event traffic."""
        if not self.configured:
            return
        title, body = event_message(event)
        with suppress(queue.Full):
            self._queue.put({"title": title, "body": body, "respect_limit": True})

    def send_now(self, title: str, body: str) -> None:
        """Queue a message that bypasses the event rate limit (daily summaries)."""
        if not self.configured:
            return
        with suppress(queue.Full):
            self._queue.put({"title": title, "body": body, "respect_limit": False})

    def start(self) -> None:
        if self._thread is None and self.configured:
            self._thread = threading.Thread(
                target=self._run, name="shakepie-notify", daemon=True
            )
            self._thread.start()

    def drain(self) -> None:
        """Deliver every queued message without blocking (used by tests and shutdown)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is None or self._stopping:
                return
            self._deliver(item)

    def stop(self) -> None:
        self._stopping = True
        self._queue.put(None)

    # -- worker -----------------------------------------------------------------
    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None or self._stopping:
                return
            self._deliver(item)

    def _deliver(self, message: dict[str, Any]) -> None:
        now = self._clock()
        if message.get("respect_limit", True):
            if now - self._last_sent < self._rate_limit_seconds:
                self._suppressed += 1
                return
            if self._suppressed:
                message["body"] += f" (+{self._suppressed} suppressed)"
                self._suppressed = 0
        self._last_sent = now
        url = bark_url(self._server, self._key, message["title"], message["body"])
        try:
            self._sender(url)
        except Exception as error:  # notification must never crash the worker
            print(f"notify: bark delivery failed: {error}", flush=True)

    @staticmethod
    def _default_sender(url: str) -> dict[str, Any]:
        with urlopen(Request(url, headers={"User-Agent": "ShakePie/1.0"}), timeout=5.0) as reply:
            parsed: dict[str, Any] = json.loads(reply.read().decode("utf-8", errors="replace"))
            return parsed


# ---------------------------------------------------------------------------
# nightly summary (Beijing midnight = 16:00 UTC; China has no daylight saving)


def build_daily_summary(
    catalog: Any,
    data_directory: Path,
    rate_hz: int,
    recorder_state: str,
    temperature: float | None,
    now_utc: datetime,
) -> tuple[str, str]:
    """Compose the nightly report for the Beijing day that just finished."""
    beijing_now = now_utc + BEIJING_OFFSET
    day = beijing_now.date() - timedelta(days=1)
    day_start_utc = datetime.combine(day, datetime.min.time(), UTC) - BEIJING_OFFSET
    day_end_utc = day_start_utc + timedelta(days=1)
    start_ns = int(day_start_utc.timestamp() * 1e9)
    end_ns = int(day_end_utc.timestamp() * 1e9)

    events = catalog.count_events_in_window(start_ns, end_ns)
    removed = catalog.count_removed_in_window(
        day_start_utc.isoformat(timespec="seconds"),
        day_end_utc.isoformat(timespec="seconds"),
    )
    coverage = catalog.coverage_between(start_ns, end_ns)
    hours = coverage / 3.6e12
    coverage_pct = 100 * coverage / 8.64e13

    try:
        usage = shutil.disk_usage(data_directory)
        disk_percent = round(usage.used * 100 / usage.total)
        daily_bytes = max(1.0, rate_hz * 16 * 1.07 * 86_400)
        days_left = usage.free / daily_bytes
    except OSError:
        disk_percent, days_left = -1, -1.0

    title = f"ShakePie daily {day.strftime('%m-%d')}"
    body = (
        f"Events: {events} (removed {removed})\n"
        f"Recorded: {hours:.1f}h · coverage {coverage_pct:.1f}%\n"
        + (
            f"Disk: {disk_percent}% · ~{int(days_left)}d\n"
            if disk_percent >= 0
            else "Disk: unavailable\n"
        )
        + f"State: {recorder_state}"
        + (f" · {temperature:.0f}C" if temperature is not None else "")
    )
    return title, body


class DailySummaryScheduler:
    """Sends the nightly summary at Beijing midnight (UTC+8, no daylight saving)."""

    def __init__(
        self,
        build_summary: Callable[[], tuple[str, str]],
        summary_hour_beijing: int,
        notifier: BarkNotifier,
        *,
        now_fn: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._build_summary = build_summary
        self._summary_hour = summary_hour_beijing
        self._notifier = notifier
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._thread: threading.Thread | None = None
        self._stopping = False

    @staticmethod
    def next_run_utc(now_utc: datetime, summary_hour_beijing: int) -> datetime:
        beijing_now = now_utc + BEIJING_OFFSET
        base_beijing = beijing_now.replace(
            hour=summary_hour_beijing, minute=0, second=0, microsecond=0
        )
        candidate = base_beijing - BEIJING_OFFSET
        if candidate <= now_utc:
            candidate += timedelta(days=1)
        return candidate

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="shakepie-daily-summary", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stopping:
            now = self._now_fn().astimezone(UTC)
            target = self.next_run_utc(now, self._summary_hour)
            remaining = (target - now).total_seconds()
            while remaining > 0 and not self._stopping:
                self._sleep(min(30.0, remaining))
                remaining -= 30.0
            if self._stopping:
                return
            try:
                title, body = self._build_summary()
            except Exception as error:  # a failed report must not kill the thread
                print(f"notify: daily summary failed: {error}", flush=True)
            else:
                self._notifier.send_now(title, body)
            # sleep past the scheduled minute so the same minute cannot fire twice
            self._sleep(60.0)
