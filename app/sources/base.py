"""Shared plumbing for the free research sources.

Every source in this package is a public endpoint that needs no account and no
payment. Each one is allowed to fail on its own: a source raises SourceError,
the research worker records the failure, and the other sources keep running.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

# The SEC asks automated clients to identify themselves with a real contact
# address and rejects placeholder hostnames, so an unusable value is dropped.
_FALLBACK_CONTACT = "guardrail-user@example.com"


def _contact() -> str:
    value = os.getenv("GUARDRAIL_CONTACT", "").strip()
    return value if re.fullmatch(r"[^@\s]+@[^@\s.]+\.[^@\s]+", value) else _FALLBACK_CONTACT


CONTACT = _contact()
SEC_USER_AGENT = f"Guardrail Research {CONTACT}"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
MAX_BYTES = 40 * 1024 * 1024


class SourceError(Exception):
    """A single research source could not be read this cycle."""


@dataclass(frozen=True)
class Signal:
    """One dated, directional piece of evidence about one symbol."""

    symbol: str
    source: str
    kind: str
    direction: int          # +1 supportive, -1 cautionary, 0 informational only
    magnitude: float        # 0..1 strength inside its own source
    event_at: datetime
    headline: str
    dedupe_key: str
    url: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class SourceContext:
    """What one source is allowed to know about the rest of the app."""

    symbols: list[str] = field(default_factory=list)   # symbols worth a per-symbol lookup
    processed: set[str] = field(default_factory=set)   # keys this source already handled
    remember: Callable[[str], None] = lambda _key: None
    budget: int = 25                                   # max expensive sub-fetches per cycle


_host_lock = threading.Lock()
_next_call: dict[str, float] = {}
# Politeness floor between two calls to the same host, in seconds.
_HOST_DELAY = {"www.sec.gov": 0.2, "efts.sec.gov": 0.2, "trends.google.com": 2.0}


def _throttle(url: str) -> None:
    host = urlsplit(url).hostname or ""
    delay = _HOST_DELAY.get(host, 0.35)
    with _host_lock:
        wait = _next_call.get(host, 0.0) - time.monotonic()
        _next_call[host] = time.monotonic() + max(wait, 0.0) + delay
    if wait > 0:
        time.sleep(min(wait, 10.0))


def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: float = 30.0,
    attempts: int = 2,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """One polite HTTP call with retries. Raises SourceError on any failure."""
    last: Exception | None = None
    for attempt in range(attempts):
        _throttle(url)
        try:
            caller = client or httpx.Client(timeout=timeout, follow_redirects=True)
            try:
                response = caller.request(method, url, headers=headers, params=params, json=json_body)
            finally:
                if client is None:
                    caller.close()
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.is_error:
                raise SourceError(f"{urlsplit(url).hostname} returned HTTP {response.status_code}.")
            if len(response.content) > MAX_BYTES:
                raise SourceError(f"{urlsplit(url).hostname} returned an unexpectedly large response.")
            return response
        except httpx.HTTPError as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.0 * (attempt + 1))
    raise SourceError(f"{urlsplit(url).hostname} could not be reached: {type(last).__name__}.")


def fetch_json(url: str, **kwargs) -> dict | list:
    response = fetch(url, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError(f"{urlsplit(url).hostname} did not return JSON.") from exc


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(text: str, pattern: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), pattern).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recent(moment: datetime | None, days: int) -> bool:
    return moment is not None and moment >= utc_now() - timedelta(days=days)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def band_magnitude(value: float, low: float, high: float) -> float:
    """0..1 across a log band: `low` and below score 0, `high` and above score 1."""
    if value <= 0 or low <= 0 or high <= low:
        return 0.0
    return clamp((math.log10(value) - math.log10(low)) / (math.log10(high) - math.log10(low)))
