"""The watchlist: symbols the research pulled in plus symbols the user added.

Auto entries come and go with the evidence. Manual entries stay until the user
removes them. A muted symbol stays visible but is never bought automatically.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app import research
from app.sources import tickers
from app.sources.base import utc_now

# A symbol joins the list automatically at this score with two independent
# sources behind it, and leaves again once the evidence fades below DROP_SCORE.
WATCH_SCORE = 58.0
WATCH_SOURCES = 2
STRONG_WATCH_SCORE = 63.0
DROP_SCORE = 53.0
MAX_AUTO_ENTRIES = 30
FOCUS_LIMIT = 12
# The radar shows the strongest symbols that have not earned a watchlist spot
# yet, so the user can see what is building before it qualifies.
RADAR_LIMIT = 15
RADAR_MIN_SCORE = 52.0
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class WatchlistError(ValueError):
    pass


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db(path: Path) -> None:
    research.init_db(path)
    with _connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY, origin TEXT NOT NULL, added_at TEXT NOT NULL, note TEXT,
            muted INTEGER NOT NULL DEFAULT 0
        )""")


def normalize(symbol: str) -> str:
    value = (symbol or "").strip().upper()
    if not _SYMBOL.match(value):
        raise WatchlistError("Enter a valid US stock symbol.")
    return value


def entries(path: Path) -> dict[str, dict]:
    init_db(path)
    with _connect(path) as db:
        return {row["symbol"]: dict(row) for row in db.execute("SELECT * FROM watchlist")}


def add(path: Path, symbol: str, note: str | None = None) -> dict:
    symbol = normalize(symbol)
    if not tickers.known(symbol):
        raise WatchlistError(f"{symbol} is not in the SEC list of US-listed companies.")
    init_db(path)
    with _connect(path) as db:
        db.execute("INSERT INTO watchlist (symbol, origin, added_at, note, muted) VALUES (?, 'manual', ?, ?, 0) "
                   "ON CONFLICT(symbol) DO UPDATE SET origin = 'manual', note = COALESCE(?, note), muted = 0",
                   (symbol, utc_now().isoformat(), note, note))
    return entries(path)[symbol]


def remove(path: Path, symbol: str) -> None:
    """Manual entries are deleted; auto entries are muted so the research does
    not immediately add them back."""
    symbol = normalize(symbol)
    init_db(path)
    with _connect(path) as db:
        row = db.execute("SELECT origin FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
        if row is None:
            raise WatchlistError(f"{symbol} is not on the watchlist.")
        if row["origin"] == "manual":
            db.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
        else:
            db.execute("UPDATE watchlist SET muted = 1 WHERE symbol = ?", (symbol,))


def set_muted(path: Path, symbol: str, muted: bool) -> dict:
    symbol = normalize(symbol)
    init_db(path)
    with _connect(path) as db:
        if db.execute("SELECT 1 FROM watchlist WHERE symbol = ?", (symbol,)).fetchone() is None:
            if muted:
                db.execute("INSERT INTO watchlist (symbol, origin, added_at, muted) VALUES (?, 'manual', ?, 1)",
                           (symbol, utc_now().isoformat()))
            else:
                raise WatchlistError(f"{symbol} is not on the watchlist.")
        db.execute("UPDATE watchlist SET muted = ? WHERE symbol = ?", (1 if muted else 0, symbol))
    return entries(path)[symbol]


def sync_auto(path: Path, scored: list[dict] | None = None, *, keep: set[str] | None = None) -> None:
    """Add symbols with enough independent evidence and drop auto entries whose
    evidence faded. `keep` protects symbols that still have an open plan."""
    init_db(path)
    scored = scored if scored is not None else research.score_symbols(path, limit=200)
    by_symbol = {row["symbol"]: row for row in scored}
    keep = keep or set()
    qualifying = [row for row in scored if row["score"] >= STRONG_WATCH_SCORE
                  or (row["score"] >= WATCH_SCORE and row["conviction"] >= WATCH_SOURCES)]
    now = utc_now().isoformat()
    with _connect(path) as db:
        current = {row["symbol"]: dict(row) for row in db.execute("SELECT * FROM watchlist")}
        for row in qualifying[:MAX_AUTO_ENTRIES]:
            if row["symbol"] not in current:
                db.execute("INSERT INTO watchlist (symbol, origin, added_at, muted) VALUES (?, 'auto', ?, 0)",
                           (row["symbol"], now))
        for symbol, entry in current.items():
            if entry["origin"] != "auto" or entry["muted"] or symbol in keep:
                continue
            score = by_symbol.get(symbol, {}).get("score", 50.0)
            if score < DROP_SCORE:
                db.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))


def radar(scored: list[dict], listed: set[str]) -> list[dict]:
    return [row for row in scored if row["symbol"] not in listed and row["score"] >= RADAR_MIN_SCORE][:RADAR_LIMIT]


def focus_symbols(path: Path, *, extra: list[str] | None = None) -> list[str]:
    """Symbols that deserve per-symbol lookups (Google Trends, StockTwits):
    manual entries first, then the strongest evidence, so emerging candidates
    get confirmed or contradicted before they can be bought."""
    scored = research.score_symbols(path, limit=500)
    try:
        sync_auto(path, scored)
    except sqlite3.Error:
        pass
    current = {symbol: entry for symbol, entry in entries(path).items() if not entry["muted"]}
    scores = {row["symbol"]: row["score"] for row in scored}
    manual = [symbol for symbol, entry in current.items() if entry["origin"] == "manual"]
    ranked = sorted((symbol for symbol in current if symbol not in manual),
                    key=lambda symbol: scores.get(symbol, 50.0), reverse=True)
    ordered = list(extra or []) + manual + ranked + [row["symbol"] for row in radar(scored, set(current))]
    unique = list(dict.fromkeys(ordered))
    return unique[:FOCUS_LIMIT]
