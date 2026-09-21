"""Signal store and scoring for the free research sources.

Each source contributes dated, directional evidence. This module keeps that
evidence in SQLite, decays it with age, caps how far any single source can push
a symbol, and turns the result into one transparent 0-100 score per symbol.

The score is an evidence summary, not a prediction. Every number a user sees
can be traced back to the filing or feed row it came from.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.sources import congress, contracts, insiders, retail, trends
from app.sources.base import Signal, SourceContext, SourceError, utc_now

# Weights are priors about how much each free source has historically been
# worth, not fitted parameters. Insider open-market buying is the best
# documented of the group; crowd attention is the weakest and is capped hardest.
SOURCES: dict[str, dict] = {
    "insiders": {"module": insiders, "weight": 1.00, "half_life_days": 21, "interval": 900,
                 "budget": 120, "cap": 1.8, "label": "SEC Form 4 insider trades",
                 "home": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4"},
    "congress": {"module": congress, "weight": 0.70, "half_life_days": 30, "interval": 3600,
                 "budget": 40, "cap": 1.4, "label": "US House trade disclosures",
                 "home": "https://disclosures-clerk.house.gov/PublicDisclosure"},
    "contracts": {"module": contracts, "weight": 0.40, "half_life_days": 45, "interval": 21600,
                  "budget": 1, "cap": 1.0, "label": "Federal contract awards",
                  "home": "https://www.usaspending.gov/search"},
    "trends": {"module": trends, "weight": 0.35, "half_life_days": 7, "interval": 21600,
               "budget": 10, "cap": 0.9, "label": "Google Trends search interest",
               "home": "https://trends.google.com/trending"},
    "retail": {"module": retail, "weight": 0.30, "half_life_days": 3, "interval": 1800,
               "budget": 12, "cap": 0.8, "label": "r/wallstreetbets and StockTwits",
               "home": "https://apewisdom.io/wallstreetbets/"},
}
HORIZON_DAYS = 60
CAUTION_KINDS = {"retail_crowding", "trends_spike", "trends_fading", "insider_sell", "congress_sell"}
RESEARCH_INTERVAL_SECONDS = 300
# Held while sources run, so a manual refresh never overlaps the worker.
RUN_LOCK = threading.Lock()


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, and always close (Windows keeps
    an open SQLite file locked)."""
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS signals (
            dedupe_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
            direction INTEGER NOT NULL, magnitude REAL NOT NULL, event_at TEXT NOT NULL,
            observed_at TEXT NOT NULL, headline TEXT NOT NULL, url TEXT, detail_json TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS signals_symbol_time ON signals(symbol, event_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS signals_time ON signals(event_at DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS source_runs (
            source TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT NOT NULL DEFAULT 'idle',
            message TEXT, new_signals INTEGER NOT NULL DEFAULT 0, total_signals INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS source_cursor (
            source TEXT NOT NULL, key TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY (source, key)
        )""")


def store(path: Path, signals: list[Signal]) -> int:
    """Insert new evidence. A repeated dedupe key is ignored, never double counted."""
    if not signals:
        return 0
    observed = utc_now().isoformat()
    with _connect(path) as db:
        before = db.total_changes
        db.executemany(
            "INSERT OR IGNORE INTO signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(signal.dedupe_key, signal.symbol, signal.source, signal.kind, signal.direction,
              float(signal.magnitude), signal.event_at.astimezone(timezone.utc).isoformat(), observed,
              signal.headline, signal.url, json.dumps(signal.detail, default=str)) for signal in signals],
        )
        return db.total_changes - before


def prune(path: Path, *, keep_days: int = 180) -> None:
    cutoff = (utc_now() - timedelta(days=keep_days)).isoformat()
    with _connect(path) as db:
        db.execute("DELETE FROM signals WHERE event_at < ?", (cutoff,))
        db.execute("DELETE FROM source_cursor WHERE seen_at < ?", (cutoff,))


def _cursor(db: sqlite3.Connection, source: str) -> set[str]:
    return {row["key"] for row in db.execute("SELECT key FROM source_cursor WHERE source = ?", (source,))}


def due_sources(path: Path, *, now: datetime | None = None) -> list[str]:
    now = now or utc_now()
    init_db(path)
    with _connect(path) as db:
        runs = {row["source"]: row for row in db.execute("SELECT * FROM source_runs")}
    due = []
    for name, config in SOURCES.items():
        run = runs.get(name)
        started = run["started_at"] if run else None
        if not started:
            due.append(name)
            continue
        try:
            last = datetime.fromisoformat(started)
        except ValueError:
            due.append(name)
            continue
        if (now - last).total_seconds() >= config["interval"]:
            due.append(name)
    return due


def run_source(path: Path, name: str, symbols: list[str]) -> dict:
    """Run one source and record what happened. Never raises for source failures."""
    config = SOURCES[name]
    init_db(path)
    started = utc_now().isoformat()
    with _connect(path) as db:
        db.execute("INSERT INTO source_runs (source, started_at, status) VALUES (?, ?, 'running') "
                   "ON CONFLICT(source) DO UPDATE SET started_at = ?, status = 'running'",
                   (name, started, started))
        processed = _cursor(db, name)
    fresh: list[str] = []
    context = SourceContext(symbols=list(symbols), processed=processed,
                            remember=fresh.append, budget=config["budget"])
    status, message, signals = "ok", None, []
    try:
        signals = config["module"].collect(context)
    except SourceError as exc:
        status, message = "unavailable", str(exc)
    except Exception as exc:  # A broken source must not take the worker down.
        logging.exception("Research source %s failed", name)
        status, message = "error", f"{type(exc).__name__}: {exc}"[:240]
    new_count = store(path, signals)
    seen_at = utc_now().isoformat()
    with _connect(path) as db:
        if fresh:
            db.executemany("INSERT OR IGNORE INTO source_cursor VALUES (?, ?, ?)",
                           [(name, key, seen_at) for key in fresh])
        total = db.execute("SELECT COUNT(*) AS count FROM signals WHERE source = ?", (name,)).fetchone()["count"]
        db.execute("UPDATE source_runs SET finished_at = ?, status = ?, message = ?, new_signals = ?, "
                   "total_signals = ? WHERE source = ?", (seen_at, status, message, new_count, total, name))
    return {"source": name, "status": status, "message": message, "new_signals": new_count,
            "collected": len(signals)}


def refresh(path: Path, symbols: list[str], *, only: str | None = None, force: bool = False) -> list[dict]:
    names = [only] if only else (list(SOURCES) if force else due_sources(path))
    if only and only not in SOURCES:
        raise KeyError(only)
    return [run_source(path, name, symbols) for name in names]


def status(path: Path) -> dict:
    init_db(path)
    with _connect(path) as db:
        runs = {row["source"]: dict(row) for row in db.execute("SELECT * FROM source_runs")}
        counted = db.execute(
            "SELECT COUNT(*) AS signals, COUNT(DISTINCT symbol) AS symbols FROM signals WHERE event_at >= ?",
            ((utc_now() - timedelta(days=HORIZON_DAYS)).isoformat(),)).fetchone()
    sources = []
    for name, config in SOURCES.items():
        run = runs.get(name, {})
        sources.append({
            "source": name, "label": config["label"], "home": config["home"],
            "weight": config["weight"], "half_life_days": config["half_life_days"],
            "interval_seconds": config["interval"], "status": run.get("status", "idle"),
            "message": run.get("message"), "last_started_at": run.get("started_at"),
            "last_finished_at": run.get("finished_at"), "new_signals": run.get("new_signals", 0),
            "total_signals": run.get("total_signals", 0),
        })
    return {"sources": sources, "horizon_days": HORIZON_DAYS,
            "signals_in_horizon": counted["signals"], "symbols_in_horizon": counted["symbols"],
            "cost": "Every source is a free public endpoint. No paid data feed is used."}


def _weighted(row: sqlite3.Row, now: datetime) -> float:
    config = SOURCES.get(row["source"])
    if config is None:
        return 0.0
    try:
        event_at = datetime.fromisoformat(row["event_at"])
    except ValueError:
        return 0.0
    age_days = max((now - event_at).total_seconds() / 86400, 0.0)
    decay = 0.5 ** (age_days / config["half_life_days"])
    return config["weight"] * row["direction"] * row["magnitude"] * decay


def score_symbols(path: Path, *, symbols: list[str] | None = None, limit: int = 50,
                  horizon_days: int = HORIZON_DAYS) -> list[dict]:
    """Rank symbols by decayed, per-source-capped evidence."""
    init_db(path)
    now = utc_now()
    query = "SELECT * FROM signals WHERE event_at >= ?"
    params: list = [(now - timedelta(days=horizon_days)).isoformat()]
    if symbols:
        query += f" AND symbol IN ({','.join('?' * len(symbols))})"
        params.extend(symbol.upper() for symbol in symbols)
    with _connect(path) as db:
        rows = db.execute(query, params).fetchall()
    grouped: dict[str, dict[str, list[tuple[float, sqlite3.Row]]]] = {}
    for row in rows:
        contribution = _weighted(row, now)
        grouped.setdefault(row["symbol"], {}).setdefault(row["source"], []).append((contribution, row))
    results = []
    for symbol, by_source in grouped.items():
        total = 0.0
        per_source = {}
        evidence: list[tuple[float, sqlite3.Row]] = []
        cautions: set[str] = set()
        for source, entries in by_source.items():
            cap = SOURCES[source]["cap"]
            raw = sum(value for value, _ in entries)
            # tanh keeps a single noisy source from running away with the score.
            capped = cap * math.tanh(raw / cap) if cap else 0.0
            per_source[source] = round(capped, 4)
            total += capped
            evidence.extend(entries)
            cautions.update(row["kind"] for _, row in entries if row["kind"] in CAUTION_KINDS)
        evidence.sort(key=lambda item: abs(item[0]), reverse=True)
        supporting = [row for value, row in evidence if value > 0]
        results.append({
            "symbol": symbol,
            "score": round(100 / (1 + math.exp(-1.4 * total)), 1),
            "net_evidence": round(total, 4),
            "sources": per_source,
            "conviction": sum(1 for value in per_source.values() if abs(value) >= 0.05),
            "supporting_count": len(supporting),
            "cautionary_count": len(evidence) - len(supporting),
            "caution_kinds": sorted(cautions),
            "last_event_at": max(row["event_at"] for _, row in evidence),
            "evidence": [{
                "source": row["source"], "kind": row["kind"], "direction": row["direction"],
                "contribution": round(value, 4), "headline": row["headline"], "url": row["url"],
                "event_at": row["event_at"], "detail": json.loads(row["detail_json"]),
            } for value, row in evidence[:8]],
        })
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:limit]


def evidence_for(path: Path, symbol: str, *, limit: int = 40) -> list[dict]:
    init_db(path)
    now = utc_now()
    with _connect(path) as db:
        rows = db.execute("SELECT * FROM signals WHERE symbol = ? ORDER BY event_at DESC LIMIT ?",
                          (symbol.upper(), limit)).fetchall()
    return [{"source": row["source"], "kind": row["kind"], "direction": row["direction"],
             "magnitude": row["magnitude"], "contribution": round(_weighted(row, now), 4),
             "headline": row["headline"], "url": row["url"], "event_at": row["event_at"],
             "detail": json.loads(row["detail_json"])} for row in rows]


class ResearchWorker:
    """Background refresh of the free sources, independent of any trading."""

    def __init__(self, path: Path, symbols: Callable[[], list[str]] | None = None):
        self.path = path
        self.symbols = symbols or (lambda: [])
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        init_db(self.path)
        self.thread = threading.Thread(target=self._loop, name="research", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                names = due_sources(self.path)
                if names:
                    with RUN_LOCK:
                        focus = self.symbols()
                        for name in names:
                            if self.stop_event.is_set():
                                break
                            run_source(self.path, name, focus)
                        prune(self.path)
            except Exception:
                logging.exception("Research refresh failed; will retry")
            self.stop_event.wait(RESEARCH_INTERVAL_SECONDS)
