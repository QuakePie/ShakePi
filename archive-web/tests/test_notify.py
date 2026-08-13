from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from shakepie_archive.detector import DetectedEvent
from shakepie_archive.notify import BarkNotifier, bark_url, event_message


def sample_event() -> DetectedEvent:
    return DetectedEvent(
        started_ns=1_735_689_600_000_000_000,
        ended_ns=1_735_689_601_200_000_000,
        peak_amplitude=18_530,
        peak_ratio=6.2,
    )


def test_event_message_contains_key_facts() -> None:
    title, body = event_message(sample_event())
    assert title == "ShakePie event detected"
    assert "01-01 00:00:00 UTC" in body
    assert "1.2s" in body
    assert "peak 18,530 counts" in body
    assert "x6.2" in body


def test_bark_url_quotes_and_sets_parameters() -> None:
    url = bark_url("https://api.day.app", "K3y/x", "ti tle", "bo dy/a")
    assert url.startswith("https://api.day.app/K3y%2Fx/ti%20tle/bo%20dy%2Fa")
    assert "group=ShakePie" in url
    assert "level=timeSensitive" in url
    assert "isArchive=1" in url


def test_notifier_sends_first_and_suppresses_within_limit() -> None:
    sent: list[str] = []

    class Clock:
        def __init__(self) -> None:
            self.now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()

    def fake_sender(url: str) -> dict[str, Any]:
        sent.append(url)
        return {"code": 200}

    notifier = BarkNotifier(
        "testkey",
        "https://api.day.app",
        rate_limit_seconds=600,
        clock=clock,
        sender=fake_sender,
    )
    notifier.submit(sample_event())
    notifier.submit(sample_event())  # arrives inside the rate-limit window
    notifier.drain()

    assert len(sent) == 1
    assert notifier._suppressed == 1  # the second event was counted as suppressed

    clock.now += 601.0
    notifier.submit(sample_event())
    notifier.drain()

    assert len(sent) == 2
    assert "+1 suppressed" in unquote(sent[1])


def test_notifier_without_key_stays_inert() -> None:
    sent: list[str] = []
    notifier = BarkNotifier("", sender=sent.append)
    notifier.submit(sample_event())
    notifier.drain()
    assert sent == []
    assert not notifier.configured
