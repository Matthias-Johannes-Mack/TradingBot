"""US-listed symbol universe built from the SEC's free company_tickers file.

Used to reject junk symbols scraped out of PDFs and social posts, and to map
company names (government contract recipients, trending search terms) back to
a tradable ticker.
"""

from __future__ import annotations

import re
import threading
from datetime import timedelta

from app.sources.base import SEC_USER_AGENT, SourceError, fetch_json, utc_now

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
REFRESH_AFTER = timedelta(hours=24)

# Corporate noise removed before a name is used as a lookup key.
_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES", "LLC", "LP", "LLP",
    "PLC", "LTD", "LIMITED", "HOLDING", "HOLDINGS", "GROUP", "THE", "NV", "SA", "AG", "AB", "SE",
    "TRUST", "REIT", "CLASS", "COM", "NEW", "&",
}
# Tickers that collide with ordinary words in PDFs and social posts.
_AMBIGUOUS = {
    "A", "ALL", "AN", "ANY", "ARE", "AS", "AT", "BE", "BIG", "BY", "CAN", "CEO", "DD", "DO", "EOD",
    "EPS", "ETF", "EU", "FOR", "GO", "HAS", "HE", "IF", "IN", "IPO", "IS", "IT", "ITS", "LOVE", "NOW",
    "ON", "ONE", "OR", "OUT", "PM", "PR", "PT", "RH", "SEC", "SEE", "SO", "TA", "TO", "UK", "US",
    "USA", "WHO", "WSB", "YOLO", "OPEN", "GOOD", "BEST", "REAL", "TRUE", "NEXT", "CASH", "GAIN",
}
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_lock = threading.Lock()
_cache: dict | None = None
_fetched_at = None


def _normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9 ]+", " ", name.upper())
    words = [word for word in cleaned.split() if word and word not in _SUFFIXES]
    return " ".join(words)


def _build(raw: dict | list) -> dict:
    rows = raw.values() if isinstance(raw, dict) else raw
    names: dict[str, str] = {}
    by_name: dict[str, str] = {}
    collisions: set[str] = set()
    for row in rows:
        symbol = str(row.get("ticker", "")).strip().upper()
        title = str(row.get("title", "")).strip()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            continue
        names.setdefault(symbol, title)
        key = _normalize_name(title)
        if len(key) < 4:
            continue
        if key in by_name and by_name[key] != symbol:
            collisions.add(key)
        else:
            by_name[key] = symbol
    for key in collisions:
        by_name.pop(key, None)
    return {"names": names, "by_name": by_name}


def index() -> dict:
    """Symbol -> company name, plus a normalized-name -> symbol lookup."""
    global _cache, _fetched_at
    with _lock:
        if _cache is not None and _fetched_at is not None and utc_now() - _fetched_at < REFRESH_AFTER:
            return _cache
    raw = fetch_json(COMPANY_TICKERS_URL, headers={"User-Agent": SEC_USER_AGENT}, timeout=40)
    built = _build(raw)
    if len(built["names"]) < 1000:
        raise SourceError("The SEC company ticker file looks incomplete.")
    with _lock:
        _cache, _fetched_at = built, utc_now()
    return built


def known(symbol: str) -> bool:
    symbol = symbol.strip().upper()
    if symbol in _AMBIGUOUS or not _SYMBOL_PATTERN.match(symbol):
        return False
    try:
        return symbol in index()["names"]
    except SourceError:
        return False


def company_name(symbol: str) -> str | None:
    try:
        return index()["names"].get(symbol.strip().upper())
    except SourceError:
        return None


def symbol_for_name(name: str) -> str | None:
    """Best-effort company name -> ticker. Returns None when it is not unambiguous."""
    if not name or len(name.strip()) < 4:
        return None
    try:
        lookup = index()["by_name"]
    except SourceError:
        return None
    key = _normalize_name(name)
    if not key:
        return None
    if key in lookup:
        return lookup[key]
    # Contract recipients often carry a trailing division name; try the leading words.
    words = key.split()
    for size in (3, 2):
        if len(words) > size:
            shortened = " ".join(words[:size])
            if len(shortened) >= 6 and shortened in lookup:
                return lookup[shortened]
    return None


def search_phrase(symbol: str) -> str:
    """A Google Trends keyword that means the company, not the letters."""
    name = company_name(symbol) or symbol
    trimmed = " ".join(word for word in re.split(r"[\s,/]+", name) if _normalize_name(word))
    short = " ".join(trimmed.split()[:3]).strip(" .,&")
    return f"{short or symbol} stock"
