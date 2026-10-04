from datetime import datetime

from squid_core.events import emit_event


def test_no_callback_does_nothing() -> None:
    emit_event(None, {"type": "run_started"})


def test_callback_receives_event() -> None:
    events = []

    emit_event(events.append, {"type": "run_started"})

    assert events[0]["type"] == "run_started"


def test_timestamp_is_added() -> None:
    events = []

    emit_event(events.append, {"type": "run_started"})

    assert "timestamp" in events[0]
    assert datetime.fromisoformat(events[0]["timestamp"]).tzinfo is not None


def test_existing_timestamp_is_preserved() -> None:
    events = []

    emit_event(events.append, {"type": "run_started", "timestamp": "already-here"})

    assert events[0]["timestamp"] == "already-here"


def test_callback_exception_does_not_crash_emission() -> None:
    def raise_error(event: dict) -> None:
        raise RuntimeError("boom")

    emit_event(raise_error, {"type": "run_started"})
