"""Short-Term Average / Long-Term Average ground-motion event detection.

Inspired by the classic STA/LTA triggers used throughout amateur seismology (rsudp-style
alerts, AnyShake's event detection), reimplemented from scratch for ShakePie: a bounded,
constant-memory mean-ratio trigger over one channel's raw counts, with hysteresis, a
cooldown between stored events, and resilience to recorder restarts (sequence resets clear
the windows without inventing events).

With ``minimum_components >= 2`` the worker switches to the N-of-M coincidence trigger:
each component runs an independent STA/LTA and an event is stored only while at least
that many components are in trigger at the same moment, which removes single-channel
bumps and electrical spikes from the event list.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shakepie_archive.archive_reader import _SAMPLE, RecordingError
from shakepie_archive.live_data import LiveFrameParser

if TYPE_CHECKING:
    from shakepie_archive.catalog import Catalog


@dataclass(frozen=True, slots=True)
class DetectedEvent:
    """One finished event, ready to be persisted."""

    started_ns: int
    ended_ns: int
    peak_amplitude: int
    peak_ratio: float


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    sta_seconds: float = 1.0
    lta_seconds: float = 30.0
    threshold: float = 4.0
    off_seconds: float = 5.0
    cooldown_seconds: float = 30.0
    maximum_events: int = 1000
    minimum_components: int = 1

    def __post_init__(self) -> None:
        if not 0.05 <= self.sta_seconds <= 30:
            raise ValueError("sta_seconds must be between 0.05 and 30")
        if self.lta_seconds < self.sta_seconds * 4:
            raise ValueError("lta_seconds must be at least four times sta_seconds")
        if not 1.2 <= self.threshold <= 100:
            raise ValueError("threshold must be between 1.2 and 100")
        if not 0.5 <= self.off_seconds <= 300:
            raise ValueError("off_seconds must be between 0.5 and 300")
        if not 0 <= self.cooldown_seconds <= 3600:
            raise ValueError("cooldown_seconds must be between 0 and 3600")
        if not 10 <= self.maximum_events <= 100_000:
            raise ValueError("maximum_events must be between 10 and 100000")
        if not 1 <= self.minimum_components <= 3:
            raise ValueError("minimum_components must be between 1 and 3")


class StaLtaDetector:
    """Pure single-channel trigger; feed one sample at a time in chronological order."""

    def __init__(self, config: DetectorConfig, sample_rate_hz: float) -> None:
        if not 1 <= sample_rate_hz <= 10_000:
            raise ValueError("sample_rate_hz must be between 1 and 10000")
        self._threshold = config.threshold
        self._off_samples = max(1, round(config.off_seconds * sample_rate_hz))
        self._cooldown_ns = int(config.cooldown_seconds * 1e9)
        self._sta_samples = max(1, round(config.sta_seconds * sample_rate_hz))
        self._lta_samples = max(self._sta_samples + 1, round(config.lta_seconds * sample_rate_hz))
        self._window: list[int] = []
        self._window_sum = 0
        self._sta_sum = 0
        self._quiet_run = 0
        self._last_event_end_ns = 0
        self._event_open = False
        self._event_start_ns = 0
        self._event_peak = 0
        self._event_peak_ratio = 0.0

    def reset(self) -> None:
        """Drop all buffered state (recorder restart, rate change, or clock step)."""
        self._window.clear()
        self._window_sum = 0
        self._sta_sum = 0
        self._quiet_run = 0
        self._event_open = False
        self._event_peak = 0
        self._event_peak_ratio = 0.0

    @property
    def warmed_up(self) -> bool:
        return len(self._window) >= self._lta_samples

    def feed(self, timestamp_ns: int, value: int) -> DetectedEvent | None:
        """Consume one sample; returns a finished event exactly when one closes."""
        magnitude = abs(value)
        self._window.append(value)
        self._window_sum += magnitude
        self._sta_sum += magnitude
        if len(self._window) > self._lta_samples:
            self._window_sum -= abs(self._window.pop(0))
        if len(self._window) > self._sta_samples:
            self._sta_sum -= abs(self._window[-self._sta_samples - 1])

        if not self.warmed_up:
            return None

        lta_mean = self._window_sum / self._lta_samples
        current_ratio = self._sta_sum / self._sta_samples / lta_mean if lta_mean > 0 else 0.0
        event: DetectedEvent | None = None
        if self._event_open:
            if magnitude > self._event_peak:
                self._event_peak = magnitude
            if current_ratio > self._event_peak_ratio:
                self._event_peak_ratio = current_ratio
            if current_ratio >= self._threshold:
                self._quiet_run = 0
            else:
                self._quiet_run += 1
                if self._quiet_run >= self._off_samples:
                    event = self._close(timestamp_ns)
                    self._last_event_end_ns = timestamp_ns
        elif current_ratio >= self._threshold:
            if timestamp_ns - self._last_event_end_ns >= self._cooldown_ns:
                self._event_open = True
                self._event_start_ns = timestamp_ns
                self._event_peak = magnitude
                self._event_peak_ratio = current_ratio
            else:
                # Inside the cooldown window: swallow the trigger but keep the window
                # fresh so a sustained disturbance does not queue a parade of events.
                self._last_event_end_ns = timestamp_ns
        return event

    def feed_sample(
        self, timestamp_ns: int, north: int, east: int, vertical: int
    ) -> DetectedEvent | None:
        """Common per-group entry point; the single-channel trigger uses vertical only."""
        return self.feed(timestamp_ns, vertical)

    def _close(self, ended_ns: int) -> DetectedEvent:
        self._event_open = False
        self._quiet_run = 0
        event = DetectedEvent(
            started_ns=self._event_start_ns,
            ended_ns=ended_ns,
            peak_amplitude=self._event_peak,
            peak_ratio=round(self._event_peak_ratio, 2),
        )
        self._event_peak = 0
        self._event_peak_ratio = 0.0
        return event


class _ComponentTrigger:
    """Per-channel STA/LTA state that reports open/close transitions.

    Uses the same ratio machinery as StaLtaDetector, but instead of assembling
    events it exposes segment boundaries and per-segment peaks so the
    coincidence gate can combine several components into one event.
    """

    def __init__(self, config: DetectorConfig, sample_rate_hz: float) -> None:
        self._threshold = config.threshold
        self._off_samples = max(1, round(config.off_seconds * sample_rate_hz))
        self._cooldown_ns = int(config.cooldown_seconds * 1e9)
        self._sta_samples = max(1, round(config.sta_seconds * sample_rate_hz))
        self._lta_samples = max(self._sta_samples + 1, round(config.lta_seconds * sample_rate_hz))
        self._window: list[int] = []
        self._window_sum = 0
        self._sta_sum = 0
        self._quiet_run = 0
        self._last_end_ns = 0
        self._open = False
        self._start_ns = 0
        self._peak = 0
        self._peak_ratio = 0.0

    def reset(self) -> None:
        self._window.clear()
        self._window_sum = 0
        self._sta_sum = 0
        self._quiet_run = 0
        self._open = False
        self._peak = 0
        self._peak_ratio = 0.0

    @property
    def warmed_up(self) -> bool:
        return len(self._window) >= self._lta_samples

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def segment_start_ns(self) -> int:
        return self._start_ns

    @property
    def segment_peak(self) -> int:
        return self._peak

    @property
    def segment_peak_ratio(self) -> float:
        return self._peak_ratio

    def feed(self, timestamp_ns: int, value: int) -> str | None:
        """Consume one sample; returns "open" or "close" on trigger transitions."""
        magnitude = abs(value)
        self._window.append(value)
        self._window_sum += magnitude
        self._sta_sum += magnitude
        if len(self._window) > self._lta_samples:
            self._window_sum -= abs(self._window.pop(0))
        if len(self._window) > self._sta_samples:
            self._sta_sum -= abs(self._window[-self._sta_samples - 1])
        if not self.warmed_up:
            return None
        lta_mean = self._window_sum / self._lta_samples
        ratio = self._sta_sum / self._sta_samples / lta_mean if lta_mean > 0 else 0.0
        transition: str | None = None
        if self._open:
            if magnitude > self._peak:
                self._peak = magnitude
            if ratio > self._peak_ratio:
                self._peak_ratio = ratio
            if ratio >= self._threshold:
                self._quiet_run = 0
            else:
                self._quiet_run += 1
                if self._quiet_run >= self._off_samples:
                    self._open = False
                    self._quiet_run = 0
                    self._last_end_ns = timestamp_ns
                    transition = "close"
        elif ratio >= self._threshold:
            if timestamp_ns - self._last_end_ns >= self._cooldown_ns:
                self._open = True
                self._start_ns = timestamp_ns
                self._peak = magnitude
                self._peak_ratio = ratio
                transition = "open"
            else:
                # Inside the cooldown window: swallow the trigger but keep the
                # window fresh so a sustained disturbance does not re-open early.
                self._last_end_ns = timestamp_ns
        return transition


class CoincidenceDetector:
    """N-of-M coincidence trigger across the three components.

    Every component runs an independent STA/LTA trigger (its own hysteresis and
    cooldown). A stored event exists only while at least ``minimum_components``
    triggers are open at the same moment, so a bump to one geophone or an
    electrical spike on one wire no longer reaches the event list. With
    ``minimum_components == 1`` only the vertical channel is fed, matching the
    legacy single-channel behaviour exactly.
    """

    def __init__(self, config: DetectorConfig, sample_rate_hz: float) -> None:
        if not 1 <= config.minimum_components <= 3:
            raise ValueError("minimum_components must be between 1 and 3")
        count = 3 if config.minimum_components >= 2 else 1
        self._minimum = config.minimum_components
        self._triggers = tuple(
            _ComponentTrigger(config, sample_rate_hz) for _ in range(count)
        )
        self._cooldown_ns = int(config.cooldown_seconds * 1e9)
        self._group_open = False
        self._group_start_ns = 0
        self._group_peak = 0
        self._group_peak_ratio = 0.0
        self._last_event_end_ns = 0

    def reset(self) -> None:
        for trigger in self._triggers:
            trigger.reset()
        self._group_open = False
        self._group_peak = 0
        self._group_peak_ratio = 0.0

    @property
    def warmed_up(self) -> bool:
        return all(trigger.warmed_up for trigger in self._triggers)

    def feed_sample(
        self, timestamp_ns: int, north: int, east: int, vertical: int
    ) -> DetectedEvent | None:
        """Consume one three-component sample; returns a finished event on close."""
        values = (vertical,) if len(self._triggers) == 1 else (north, east, vertical)
        for trigger, value in zip(self._triggers, values, strict=True):
            trigger.feed(timestamp_ns, value)
        open_count = sum(trigger.is_open for trigger in self._triggers)
        if not self._group_open:
            if open_count < self._minimum:
                return None
            if timestamp_ns - self._last_event_end_ns >= self._cooldown_ns:
                self._group_open = True
                self._group_start_ns = min(
                    trigger.segment_start_ns
                    for trigger in self._triggers
                    if trigger.is_open
                )
                self._group_peak = 0
                self._group_peak_ratio = 0.0
            else:
                # Inside the group cooldown: swallow the trigger but keep the gate
                # fresh so a sustained disturbance does not queue a parade of events.
                self._last_event_end_ns = timestamp_ns
                return None
        for trigger, value in zip(self._triggers, values, strict=True):
            if abs(value) > self._group_peak:
                self._group_peak = abs(value)
            if trigger.is_open and trigger.segment_peak_ratio > self._group_peak_ratio:
                self._group_peak_ratio = trigger.segment_peak_ratio
        if open_count >= self._minimum:
            return None
        self._group_open = False
        self._last_event_end_ns = timestamp_ns
        return DetectedEvent(
            started_ns=self._group_start_ns,
            ended_ns=timestamp_ns,
            peak_amplitude=self._group_peak,
            peak_ratio=round(self._group_peak_ratio, 2),
        )


def store_event(
    catalog: Catalog, event: DetectedEvent, station_id: str, maximum_events: int
) -> None:
    """Persist one finished event and trim the catalog table to its bound."""
    catalog.store_event(
        started_ns=event.started_ns,
        ended_ns=event.ended_ns,
        peak_amplitude=event.peak_amplitude,
        peak_ratio=event.peak_ratio,
        station_id=station_id,
        maximum_events=maximum_events,
    )


class EventDetectorWorker:
    """Background thread: one persistent recorder-stream client feeding the trigger.

    Unlike the on-demand live view reader, this connection runs for the lifetime of the
    web process so no disturbance is missed. It stores finished events through the
    catalog and tolerates recorder restarts (sequence resets clear the trigger state).
    """

    def __init__(
        self,
        config: DetectorConfig,
        host: str,
        port: int,
        catalog: Catalog,
        station_provider: Callable[[], str],
        maximum_events: int,
        notifier: Any | None = None,
    ) -> None:
        self._config = config
        self._host = host
        self._port = port
        self._catalog = catalog
        self._station_provider = station_provider
        self._maximum_events = maximum_events
        self._notifier = notifier
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = "starting"
        self._last_error = ""
        self._stored_count = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="shakepie-event-detector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "error": self._last_error,
                "events_stored": self._stored_count,
                "minimum_components": self._config.minimum_components,
            }

    def _store(self, event: DetectedEvent) -> None:
        store_event(self._catalog, event, self._station_provider(), self._maximum_events)
        if self._notifier is not None:
            # Bark push: queued on the notifier's own thread and rate limited, so a slow
            # network or a trigger storm cannot affect detection or storage.
            self._notifier.submit(event)
        with self._lock:
            self._stored_count += 1

    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._last_error = error[:200]

    def _make_detector(self, sample_rate_hz: float) -> StaLtaDetector | CoincidenceDetector:
        if self._config.minimum_components >= 2:
            return CoincidenceDetector(self._config, sample_rate_hz)
        return StaLtaDetector(self._config, sample_rate_hz)

    def _run(self) -> None:
        parser = LiveFrameParser()
        detector: StaLtaDetector | CoincidenceDetector | None = None
        last_sequence: int | None = None
        sample_rate_millihz: int | None = None
        while not self._stop.is_set():
            self._set_state("connecting")
            try:
                with socket.create_connection(
                    (self._host, self._port), timeout=5.0
                ) as connection:
                    connection.settimeout(5.0)
                    self._set_state("listening")
                    parser = LiveFrameParser()
                    last_sequence = None
                    while not self._stop.is_set():
                        try:
                            chunk = connection.recv(64 * 1024)
                        except TimeoutError:
                            continue
                        if not chunk:
                            raise ConnectionError("recorder closed the stream")
                        for block in parser.feed(chunk):
                            if (
                                last_sequence is not None
                                and block.first_sample_sequence <= last_sequence
                            ) or (
                                sample_rate_millihz is not None
                                and block.nominal_group_rate_millihz != sample_rate_millihz
                            ):
                                detector = None  # recorder restart or rate change
                            sample_rate_millihz = block.nominal_group_rate_millihz
                            if detector is None:
                                detector = self._make_detector(
                                    block.nominal_group_rate_millihz / 1000.0
                                )
                            last_sequence = (
                                block.first_sample_sequence + block.sample_count - 1
                            )
                            for north, east, vertical, offset in _SAMPLE.iter_unpack(
                                block.payload
                            ):
                                event = detector.feed_sample(
                                    block.utc_start_ns + int(offset),
                                    north,
                                    east,
                                    vertical,
                                )
                                if event is not None:
                                    self._store(event)
            except (OSError, RecordingError) as error:
                self._set_state("unavailable", str(error))
                detector = None
                last_sequence = None
                self._stop.wait(5.0)
