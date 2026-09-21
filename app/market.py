"""US regular trading hours for the dashboard.

Alpaca's market calendar already knows exchange holidays and early closes. Its
times are New York local, so they are converted to UTC here and the browser
shows them in the viewer's own time zone. Without broker keys the standard
weekday schedule is used instead, and the response says so.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.broker import AlpacaPaperBroker

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
CACHE_SECONDS = 600
CALENDAR_DAYS = 14
UPCOMING = 6

_lock = threading.Lock()
_cache: dict | None = None
_cached_at: datetime | None = None


def _session(day: date, opens: time, closes: time) -> dict:
    open_at = datetime.combine(day, opens, NEW_YORK)
    close_at = datetime.combine(day, closes, NEW_YORK)
    return {
        "date": day.isoformat(),
        "open_at": open_at.astimezone(timezone.utc).isoformat(),
        "close_at": close_at.astimezone(timezone.utc).isoformat(),
        "open_new_york": opens.strftime("%H:%M"), "close_new_york": closes.strftime("%H:%M"),
        "early_close": closes < REGULAR_CLOSE,
    }


def _clock_time(text: str) -> time:
    hour, minute = (int(part) for part in str(text).split(":"))
    return time(hour, minute)


def _standard_week(today: date) -> list[dict]:
    sessions, day = [], today
    while len(sessions) < UPCOMING + 1:
        if day.weekday() < 5:
            sessions.append(_session(day, REGULAR_OPEN, REGULAR_CLOSE))
        day += timedelta(days=1)
    return sessions


def _sessions(broker_factory, today: date) -> tuple[list[dict], str]:
    try:
        with broker_factory() as broker:
            calendar = broker.calendar(today, today + timedelta(days=CALENDAR_DAYS))
        sessions = [_session(date.fromisoformat(item["date"]), _clock_time(item["open"]), _clock_time(item["close"]))
                    for item in calendar]
        if sessions:
            return sessions, "alpaca"
    except Exception:  # Display-only: any broker problem falls back to the standard week.
        logging.info("Alpaca market calendar unavailable; showing the standard schedule", exc_info=True)
    return _standard_week(today), "schedule"


def hours(broker_factory=AlpacaPaperBroker, *, now: datetime | None = None, use_cache: bool = True) -> dict:
    global _cache, _cached_at
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(NEW_YORK).date()
    with _lock:
        cached = _cache if use_cache and _cached_at and (now - _cached_at).total_seconds() < CACHE_SECONDS else None
    if cached is None or cached["new_york_date"] != today.isoformat():
        sessions, source = _sessions(broker_factory, today)
        cached = {"new_york_date": today.isoformat(), "sessions": sessions, "source": source}
        with _lock:
            _cache, _cached_at = cached, now
    sessions = [item for item in cached["sessions"] if datetime.fromisoformat(item["close_at"]) > now]
    current = next((item for item in sessions if datetime.fromisoformat(item["open_at"]) <= now), None)
    upcoming = [item for item in sessions if item is not current]
    return {
        "now": now.isoformat(),
        "is_open": current is not None,
        "current": current,
        "next": upcoming[0] if upcoming else None,
        "sessions": sessions[:UPCOMING],
        "regular_new_york": f"{REGULAR_OPEN:%H:%M}–{REGULAR_CLOSE:%H:%M}",
        "source": cached["source"],
        "note": ("Alpaca market calendar: exchange holidays and early closes included."
                 if cached["source"] == "alpaca" else
                 "Standard weekday hours. Add Alpaca paper keys to include holidays and early closes."),
    }
