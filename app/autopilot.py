"""Autopilot: turns watchlist evidence into Alpaca paper positions, within limits.

What it does, in order, every automation cycle while it is switched on:
  1. reconciles orders it sent earlier (fills, rejections, crash recovery);
  2. while the US market is open, and at most every ENTRY_INTERVAL seconds:
     * exits an autopilot position when the evidence behind it turns bearish;
     * buys at most one new candidate that clears every gate below.

Every position it opens is handed to a normal automatic plan (app.automation)
with re-entry switched off, so the same stop management protects it: a hard
floor below the fill, and a floor that trails up after a gain. The autopilot
never uses the LLM, never trades outside the paper endpoint, and records the
evidence it acted on for every order it sends.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import research, watchlist
from app.automation import activate_plan, list_plans, set_plan_active
from app.broker import AlpacaPaperBroker, BrokerError, decimal_text, ecb_rate, require_recent_trade
from app.sources import tickers
from app.sources.base import utc_now
from app.strategy import StrategyInput

ENTRY_INTERVAL_SECONDS = 300
OPEN_STATES = {"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding", "pending_replace", "held"}
DEAD_STATES = {"canceled", "expired", "rejected", "done_for_day", "stopped", "suspended"}
ACTION_ORDER = {"SELL": 0, "HOLD": 1, "BUY": 2, "BUY_BLOCKED": 3, "WATCH": 4, "AVOID": 5, "MUTED": 6}


class AutopilotSettings(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    # Entry gates.
    min_score: float = Field(default=68, ge=55, le=95)
    min_sources: int = Field(default=2, ge=1, le=5)
    strong_score: float = Field(default=80, ge=60, le=99)
    block_on_caution: bool = True
    min_price_usd: Decimal = Field(default=Decimal("3"), ge=1, le=10000)
    min_iex_dollar_volume_usd: Decimal = Field(default=Decimal("500000"), ge=0)
    cooldown_days: int = Field(default=10, ge=0, le=90)
    # Size and exposure.
    budget_eur_per_position: Decimal = Field(default=Decimal("500"), ge=10, le=100000)
    max_positions: int = Field(default=5, ge=1, le=20)
    max_new_per_day: int = Field(default=2, ge=1, le=10)
    # Protection handed to the automatic plan for every position.
    hard_floor_pct: float = Field(default=8, gt=0, lt=50)
    trail_trigger_pct: float = Field(default=8, gt=0, le=100)
    trail_distance_pct: float = Field(default=6, gt=0, lt=50)
    # Evidence-based exit.
    exit_on_bearish_evidence: bool = True
    exit_score: float = Field(default=38, ge=5, le=49)

    @model_validator(mode="after")
    def ordered_thresholds(self) -> "AutopilotSettings":
        if self.strong_score < self.min_score:
            raise ValueError("The single-source score must be at least the normal buy score.")
        return self


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
    watchlist.init_db(path)
    list_plans(path)  # Creates or migrates the plans table.
    with _connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS autopilot (
            id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL DEFAULT 0,
            settings_json TEXT NOT NULL, activated_at TEXT, updated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'off', message TEXT, last_scan_at TEXT, last_checked_at TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS decisions (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, symbol TEXT NOT NULL,
            action TEXT NOT NULL, state TEXT NOT NULL, score REAL, reason TEXT NOT NULL, qty INTEGER,
            price_usd TEXT, order_id TEXT, client_order_id TEXT, plan_id TEXT, evidence_json TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS decisions_time ON decisions(created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS decisions_symbol ON decisions(symbol, created_at DESC)")
        if db.execute("SELECT 1 FROM autopilot WHERE id = 1").fetchone() is None:
            db.execute("INSERT INTO autopilot (id, enabled, settings_json, updated_at) VALUES (1, 0, ?, ?)",
                       (AutopilotSettings().model_dump_json(), utc_now().isoformat()))


def state(path: Path) -> dict:
    init_db(path)
    with _connect(path) as db:
        row = dict(db.execute("SELECT * FROM autopilot WHERE id = 1").fetchone())
    row["enabled"] = bool(row["enabled"])
    row["settings"] = json.loads(row.pop("settings_json"))
    return row


def settings_of(path: Path) -> AutopilotSettings:
    return AutopilotSettings.model_validate(state(path)["settings"])


def _set_state(path: Path, **fields: object) -> None:
    allowed = {"enabled", "settings_json", "activated_at", "status", "message", "last_scan_at", "last_checked_at"}
    if not fields.keys() <= allowed:
        raise ValueError("Unexpected autopilot field")
    fields["updated_at"] = utc_now().isoformat()
    setters = ", ".join(f"{key} = ?" for key in fields)
    with _connect(path) as db:
        db.execute(f"UPDATE autopilot SET {setters} WHERE id = 1", tuple(fields.values()))


def activate(path: Path, settings: AutopilotSettings) -> dict:
    init_db(path)
    now = utc_now().isoformat()
    _set_state(path, enabled=1, settings_json=settings.model_dump_json(), activated_at=now,
               status="armed", message="Switched on. Waiting for the next check while the market is open.")
    _decision(path, symbol="*", action="autopilot_on", state="done", score=None,
              reason="Autopilot switched on after preview and explicit confirmation.")
    return state(path)


def pause(path: Path) -> dict:
    init_db(path)
    _set_state(path, enabled=0, status="off",
               message="Switched off. Stops on existing positions stay managed by their automatic plans.")
    _decision(path, symbol="*", action="autopilot_off", state="done", score=None,
              reason="Autopilot switched off. No new buys or evidence exits will be sent.")
    return state(path)


def _decision(path: Path, *, symbol: str, action: str, state: str, score: float | None, reason: str,
              qty: int | None = None, price_usd: str | None = None, client_order_id: str | None = None,
              plan_id: str | None = None, evidence: list[dict] | None = None) -> str:
    decision_id = uuid4().hex
    now = utc_now().isoformat()
    with _connect(path) as db:
        db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                   (decision_id, now, now, symbol, action, state, score, reason, qty, price_usd,
                    client_order_id, plan_id, json.dumps(evidence or [])))
    return decision_id


def _update_decision(path: Path, decision_id: str, **fields: object) -> None:
    allowed = {"state", "reason", "order_id", "price_usd", "qty"}
    if not fields.keys() <= allowed:
        raise ValueError("Unexpected decision field")
    fields["updated_at"] = utc_now().isoformat()
    setters = ", ".join(f"{key} = ?" for key in fields)
    with _connect(path) as db:
        db.execute(f"UPDATE decisions SET {setters} WHERE id = ?", (*fields.values(), decision_id))


def decisions(path: Path, *, limit: int = 30, symbol: str | None = None) -> list[dict]:
    init_db(path)
    with _connect(path) as db:
        if symbol:
            rows = db.execute("SELECT * FROM decisions WHERE symbol = ? ORDER BY created_at DESC LIMIT ?", (symbol, limit))
        else:
            rows = db.execute("SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,))
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
            result.append(item)
        return result


def _pending(path: Path) -> list[dict]:
    with _connect(path) as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM decisions WHERE action IN ('buy', 'exit') AND state IN ('submitting', 'submitted')")]


def _buys_today(path: Path) -> int:
    start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with _connect(path) as db:
        return db.execute("SELECT COUNT(*) AS count FROM decisions WHERE action = 'buy' AND created_at >= ? "
                          "AND state NOT IN ('failed', 'not_filled')", (start,)).fetchone()["count"]


def _cooldowns(path: Path, days: int) -> dict[str, str]:
    """Symbols the autopilot closed recently, mapped to when they may be bought again."""
    if days <= 0:
        return {}
    since = (utc_now() - timedelta(days=days)).isoformat()
    result: dict[str, str] = {}
    with _connect(path) as db:
        for row in db.execute("SELECT symbol, MAX(created_at) AS at FROM decisions WHERE action IN ('exit', 'stopped_out') "
                              "AND state NOT IN ('failed', 'not_filled') AND created_at >= ? GROUP BY symbol", (since,)):
            result[row["symbol"]] = (datetime.fromisoformat(row["at"]) + timedelta(days=days)).isoformat()
    return result


def qualifies(row: dict, settings: AutopilotSettings) -> bool:
    return row["score"] >= settings.strong_score or (
        row["score"] >= settings.min_score and row["conviction"] >= settings.min_sources)


def classify(row: dict, settings: AutopilotSettings, context: dict) -> tuple[str, str, list[str]]:
    """What the autopilot intends to do with one symbol, and why, in plain words."""
    symbol = row["symbol"]
    score, conviction = row["score"], row["conviction"]
    cautions = row.get("caution_kinds", [])
    plan = context["autopilot_plans"].get(symbol)
    if plan:
        if settings.exit_on_bearish_evidence and score < settings.exit_score:
            return "SELL", (f"Evidence turned bearish (score {score:.0f} < exit {settings.exit_score:.0f}). "
                            "The next check during market hours cancels the stop and sells."), []
        return "HOLD", (f"Held by autopilot plan ({plan['status'].replace('_', ' ')}). "
                        f"Hard floor {settings.hard_floor_pct:g}%, trails {settings.trail_distance_pct:g}% "
                        f"after +{settings.trail_trigger_pct:g}%."), []
    if context["muted"].get(symbol):
        return "MUTED", "Muted: shown for reference, never bought automatically.", []
    if score <= 40:
        return "AVOID", f"Net bearish evidence (score {score:.0f}).", []
    if not qualifies(row, settings):
        need = (f"needs score {settings.min_score:.0f}+ from {settings.min_sources}+ sources, "
                f"or {settings.strong_score:.0f}+ from one")
        return "WATCH", f"Watching: {need}; now {score:.0f} from {conviction} source{'s' if conviction != 1 else ''}.", []
    if settings.block_on_caution and cautions:
        readable = ", ".join(kind.replace("_", " ") for kind in cautions)
        return "WATCH", f"Score {score:.0f} qualifies, but held back by caution flags: {readable}.", []
    blockers = []
    if not context["enabled"]:
        blockers.append("autopilot is off")
    if symbol in context["manual_plans"]:
        blockers.append("a manual automatic plan already manages this symbol")
    if symbol in context["cooldowns"]:
        blockers.append(f"cooling down after a recent exit until {context['cooldowns'][symbol][:10]}")
    if context["capacity_left"] <= 0:
        blockers.append(f"all {settings.max_positions} position slots are in use")
    if context["buys_left_today"] <= 0:
        blockers.append(f"today's limit of {settings.max_new_per_day} new buys is reached")
    if blockers:
        return "BUY_BLOCKED", f"Buy candidate (score {score:.0f}, {conviction} sources), but " + "; ".join(blockers) + ".", blockers
    return "BUY", (f"Buy candidate: score {score:.0f} from {conviction} source{'s' if conviction != 1 else ''}. "
                   f"Next market-hours check buys about EUR {settings.budget_eur_per_position} after price and liquidity checks."), []


def _context(path: Path, settings: AutopilotSettings, enabled: bool) -> dict:
    plans = list_plans(path)
    autopilot_plans = {plan["symbol"]: plan for plan in plans if plan["active"] and plan.get("origin") == "autopilot"}
    return {
        "enabled": enabled,
        "autopilot_plans": autopilot_plans,
        "manual_plans": {plan["symbol"] for plan in plans if plan["active"] and plan.get("origin") != "autopilot"},
        "muted": {symbol: bool(entry["muted"]) for symbol, entry in watchlist.entries(path).items()},
        "cooldowns": _cooldowns(path, settings.cooldown_days),
        "capacity_left": settings.max_positions - len(autopilot_plans),
        "buys_left_today": settings.max_new_per_day - _buys_today(path),
    }


def board(path: Path) -> dict:
    """The watchlist as the user sees it: every symbol, its evidence, and what the autopilot intends to do."""
    init_db(path)
    current = state(path)
    settings = AutopilotSettings.model_validate(current["settings"])
    scored = research.score_symbols(path, limit=400)
    context = _context(path, settings, current["enabled"])
    watchlist.sync_auto(path, scored, keep=set(context["autopilot_plans"]))
    entries = watchlist.entries(path)
    context["muted"] = {symbol: bool(entry["muted"]) for symbol, entry in entries.items()}
    by_symbol = {row["symbol"]: row for row in scored}
    symbols = set(entries) | set(context["autopilot_plans"])
    radar = {row["symbol"] for row in watchlist.radar(scored, symbols)}
    rows = []
    for symbol in symbols | radar:
        row = by_symbol.get(symbol) or {"symbol": symbol, "score": 50.0, "conviction": 0, "sources": {},
                                         "caution_kinds": [], "evidence": [], "net_evidence": 0.0,
                                         "last_event_at": None, "supporting_count": 0, "cautionary_count": 0}
        action, reason, blockers = classify(row, settings, context)
        entry = entries.get(symbol, {})
        plan = context["autopilot_plans"].get(symbol)
        rows.append({
            **{key: row[key] for key in ("symbol", "score", "conviction", "sources", "caution_kinds",
                                         "net_evidence", "last_event_at", "supporting_count", "cautionary_count")},
            "name": tickers.company_name(symbol),
            "evidence": row["evidence"][:4],
            "origin": "radar" if symbol in radar else entry.get("origin", "autopilot"),
            "note": entry.get("note"), "muted": bool(entry.get("muted")),
            "action": action, "reason": reason, "blockers": blockers,
            "plan": {"id": plan["id"], "status": plan["status"], "last_error": plan.get("last_error"),
                     "last_entry_usd": plan.get("last_entry_usd"), "high_water_usd": plan.get("high_water_usd")} if plan else None,
        })
    rows.sort(key=lambda item: (ACTION_ORDER.get(item["action"], 9), -item["score"]))
    public = {key: current[key] for key in ("enabled", "settings", "activated_at", "status", "message",
                                            "last_scan_at", "last_checked_at")}
    public.update({"positions_used": len(context["autopilot_plans"]), "buys_today": _buys_today(path),
                   "entry_interval_seconds": ENTRY_INTERVAL_SECONDS})
    return {"generated_at": utc_now().isoformat(), "rows": rows, "autopilot": public,
            "decisions": decisions(path, limit=25),
            "thresholds": {"watch_score": watchlist.WATCH_SCORE, "drop_score": watchlist.DROP_SCORE}}


# --- Broker-side screening -------------------------------------------------

_screen_lock = threading.Lock()
_screen_cache: dict[tuple[str, str], str] = {}


def _screen(broker: AlpacaPaperBroker, symbol: str, settings: AutopilotSettings) -> tuple[str | None, Decimal | None]:
    """Returns (reason it fails, None) or (None, latest IEX price). Daily failures are cached."""
    day = utc_now().date().isoformat()
    with _screen_lock:
        cached = _screen_cache.get((symbol, day))
    if cached:
        return cached, None

    def fail(reason: str, cache: bool = True) -> tuple[str, None]:
        if cache:
            with _screen_lock:
                _screen_cache[(symbol, day)] = reason
        return reason, None

    try:
        asset = broker.asset(symbol)
    except BrokerError:
        return fail("Alpaca does not list this symbol")
    if asset.get("class") != "us_equity" or not asset.get("tradable") or asset.get("status") != "active":
        return fail("not an active, tradable US equity at Alpaca")
    if broker.position(symbol):
        return fail("an Alpaca position already exists outside the autopilot", cache=False)
    if broker.open_orders(symbol):
        return fail("open Alpaca orders already exist for this symbol", cache=False)
    trade = broker.latest_trade(symbol)
    try:
        require_recent_trade(trade)
    except BrokerError:
        return fail("no IEX trade in the last 5 minutes; too illiquid for stop management", cache=False)
    price = Decimal(str(trade["p"]))
    if price < settings.min_price_usd:
        return fail(f"price ${decimal_text(price)} is below the ${settings.min_price_usd} minimum")
    if settings.min_iex_dollar_volume_usd > 0:
        bars = broker.candles(symbol, "1Day")[-20:]
        if len(bars) < 10:
            return fail("not enough daily history on IEX")
        volume = sum(Decimal(str(bar.get("c", 0))) * Decimal(str(bar.get("v", 0))) for bar in bars) / len(bars)
        if volume < settings.min_iex_dollar_volume_usd:
            return fail(f"average IEX dollar volume ${volume:,.0f} is below ${settings.min_iex_dollar_volume_usd:,.0f}")
    return None, price


# --- The cycle ---------------------------------------------------------------

def _reconcile(path: Path, broker: AlpacaPaperBroker, plans_by_id: dict[str, dict]) -> None:
    for item in _pending(path):
        order = broker.order_by_client_id(item["client_order_id"]) if item["client_order_id"] else None
        plan = plans_by_id.get(item["plan_id"] or "")
        if order is None:
            age = utc_now() - datetime.fromisoformat(item["created_at"])
            if item["state"] == "submitting" and age > timedelta(minutes=2):
                _update_decision(path, item["id"], state="failed",
                                 reason=item["reason"] + " | The order never reached Alpaca.")
                if item["action"] == "buy" and plan and plan["active"]:
                    set_plan_active(path, plan["id"], False, status="autopilot_buy_failed",
                                    message="Autopilot buy never reached Alpaca; plan paused.")
                elif item["action"] == "exit" and plan and not plan["active"]:
                    set_plan_active(path, plan["id"], True, status="armed",
                                    message="Autopilot exit never reached Alpaca; stop management resumed.")
            continue
        status = order.get("status")
        if status == "filled":
            _update_decision(path, item["id"], state="filled", order_id=order.get("id"),
                             price_usd=order.get("filled_avg_price"), qty=int(float(order.get("filled_qty") or item["qty"] or 0)))
            if item["action"] == "exit" and plan and plan["status"] == "autopilot_exit":
                set_plan_active(path, plan["id"], False, status="closed_by_evidence",
                                message=f"Evidence exit filled at ${order.get('filled_avg_price')}; plan closed.")
        elif status in DEAD_STATES:
            _update_decision(path, item["id"], state="not_filled", order_id=order.get("id"),
                             reason=item["reason"] + f" | Alpaca status: {status}.")
            if item["action"] == "buy" and plan and plan["active"]:
                set_plan_active(path, plan["id"], False, status="autopilot_buy_not_filled",
                                message=f"Autopilot buy ended as {status}; plan paused.")
            elif item["action"] == "exit" and plan and not plan["active"]:
                set_plan_active(path, plan["id"], True, status="armed",
                                message=f"Autopilot exit ended as {status}; stop management resumed.")
        elif item["state"] == "submitting":
            _update_decision(path, item["id"], state="submitted", order_id=order.get("id"))
    # A plan-owned protective exit filled: the automatic plan reports its
    # re-entry levels as exhausted, because the autopilot switched re-entry off.
    for plan in plans_by_id.values():
        if plan["active"] and plan.get("origin") == "autopilot" and plan["status"] == "reentry_levels_exhausted":
            set_plan_active(path, plan["id"], False, status="closed_by_stop",
                            message="Protective stop filled; autopilot closed this plan.")
            _decision(path, symbol=plan["symbol"], action="stopped_out", state="done", score=None,
                      reason="The protective stop filled. The symbol is on cooldown before any new buy.",
                      plan_id=plan["id"])


def _exit(path: Path, broker: AlpacaPaperBroker, plan: dict, row: dict, settings: AutopilotSettings) -> None:
    symbol = plan["symbol"]
    position = broker.position(symbol)
    if not position:
        return
    qty = Decimal(str(position.get("qty", "0")))
    if qty <= 0 or qty != qty.to_integral_value():
        return
    owner = f"guardrail-auto-{plan['id'][:12]}-"
    sells = [order for order in broker.open_orders(symbol) if order.get("side") == "sell"]
    if any(not str(order.get("client_order_id", "")).startswith(owner) for order in sells):
        _decision(path, symbol=symbol, action="exit", state="failed", score=row["score"],
                  reason="Bearish evidence, but a sell order the autopilot does not own exists. Review Alpaca orders.")
        return
    # Pause first so the plan cannot place a fresh stop between cancel and sell.
    set_plan_active(path, plan["id"], False, status="autopilot_exit",
                    message=f"Evidence turned bearish (score {row['score']:.0f}); autopilot is exiting.")
    for order in sells:
        broker.cancel(order["id"])
        for _ in range(10):
            current = broker.order(order["id"])
            if current.get("status") == "canceled":
                break
            if current.get("status") == "filled":
                _decision(path, symbol=symbol, action="stopped_out", state="done", score=row["score"],
                          reason="The stop filled while the autopilot was exiting.", plan_id=plan["id"])
                return
            time.sleep(0.5)
        else:
            set_plan_active(path, plan["id"], True, status="armed",
                            message="Could not confirm the stop was canceled; stop management resumed.")
            return
    client_id = "guardrail-autopilot-exit-" + uuid4().hex[:16]
    reason = (f"Sell {int(qty)} {symbol}: evidence score fell to {row['score']:.0f} "
              f"(exit below {settings.exit_score:.0f}).")
    decision_id = _decision(path, symbol=symbol, action="exit", state="submitting", score=row["score"],
                            reason=reason, qty=int(qty), client_order_id=client_id, plan_id=plan["id"],
                            evidence=row["evidence"][:3])
    try:
        order = broker.submit({"symbol": symbol, "qty": str(int(qty)), "side": "sell", "type": "market",
                               "time_in_force": "day", "client_order_id": client_id})
    except BrokerError as exc:
        if broker.order_by_client_id(client_id) is None:
            _update_decision(path, decision_id, state="failed", reason=f"{reason} | {exc}")
            # Re-arm the plan: its next check puts a protective stop back.
            set_plan_active(path, plan["id"], True, status="armed",
                            message="Autopilot exit failed; stop management resumed.")
        return
    _update_decision(path, decision_id, state="submitted", order_id=order.get("id"))


def _enter(path: Path, broker: AlpacaPaperBroker, row: dict, settings: AutopilotSettings,
           usd_per_eur: Decimal, account: dict) -> bool:
    symbol = row["symbol"]
    try:
        failure, price = _screen(broker, symbol, settings)
    except BrokerError as exc:
        failure, price = f"Alpaca check failed: {exc}", None
    if failure or price is None:
        recent = decisions(path, limit=1, symbol=symbol)
        if not recent or recent[0]["action"] != "skip" or recent[0]["reason"] != failure:
            _decision(path, symbol=symbol, action="skip", state="done", score=row["score"],
                      reason=failure or "no usable price", evidence=row["evidence"][:3])
        return False
    budget_usd = settings.budget_eur_per_position * usd_per_eur
    qty = int(budget_usd // price)
    if qty < 1:
        _decision(path, symbol=symbol, action="skip", state="done", score=row["score"],
                  reason=f"One share at ${decimal_text(price)} exceeds the EUR {settings.budget_eur_per_position} budget.")
        return False
    cost = price * qty
    buying_power = Decimal(str(account.get("buying_power") or account.get("cash") or "0"))
    if buying_power < cost * Decimal("1.02"):
        _decision(path, symbol=symbol, action="skip", state="done", score=row["score"],
                  reason=f"Buying power ${decimal_text(buying_power)} is below the ${decimal_text(cost)} order.")
        return False
    price_eur = float(price / usd_per_eur)
    strategy = StrategyInput(
        symbol=symbol, shares_owned=qty, entry_price=price_eur, current_price=price_eur,
        hard_floor_pct=settings.hard_floor_pct, trail_trigger_pct=settings.trail_trigger_pct,
        trail_distance_pct=settings.trail_distance_pct,
        ladder_step_pct=min(settings.hard_floor_pct * 2, 90), ladder_shares=qty, ladder_levels=1,
    )
    headline = row["evidence"][0]["headline"] if row["evidence"] else "combined evidence"
    try:
        plan = activate_plan(path, strategy, origin="autopilot", reentry=False,
                             note=f"Autopilot opened this plan for a {qty}-share buy (score {row['score']:.0f}).")
    except BrokerError as exc:
        _decision(path, symbol=symbol, action="skip", state="done", score=row["score"], reason=str(exc))
        return False
    client_id = "guardrail-autopilot-buy-" + uuid4().hex[:16]
    reason = (f"Buy {qty} {symbol} at about ${decimal_text(price)} (EUR {price_eur * qty:,.2f}): "
              f"score {row['score']:.0f} from {row['conviction']} source(s). Lead evidence: {headline}")
    # Durable intent before the external write, so a crash cannot double-buy.
    decision_id = _decision(path, symbol=symbol, action="buy", state="submitting", score=row["score"],
                            reason=reason, qty=qty, price_usd=decimal_text(price), client_order_id=client_id,
                            plan_id=plan["id"], evidence=row["evidence"][:4])
    try:
        order = broker.submit({"symbol": symbol, "qty": str(qty), "side": "buy", "type": "market",
                               "time_in_force": "day", "client_order_id": client_id})
    except BrokerError as exc:
        if broker.order_by_client_id(client_id) is None:
            _update_decision(path, decision_id, state="failed", reason=f"{reason} | {exc}")
            set_plan_active(path, plan["id"], False, status="autopilot_buy_failed",
                            message=f"Autopilot buy was rejected: {exc}")
        return False
    _update_decision(path, decision_id, state="submitted", order_id=order.get("id"))
    return True


def run_cycle(path: Path, broker_factory=AlpacaPaperBroker, rate_provider=ecb_rate) -> None:
    init_db(path)
    current = state(path)
    if not current["enabled"]:
        return
    settings = AutopilotSettings.model_validate(current["settings"])
    now = utc_now()
    _set_state(path, last_checked_at=now.isoformat())
    try:
        with broker_factory() as broker:
            plans = {plan["id"]: plan for plan in list_plans(path)}
            _reconcile(path, broker, plans)
            if not broker.clock().get("is_open"):
                _set_state(path, status="waiting_for_market", message="US market closed; no buys or exits until it opens.")
                return
            last_scan = current.get("last_scan_at")
            if last_scan and (now - datetime.fromisoformat(last_scan)).total_seconds() < ENTRY_INTERVAL_SECONDS:
                return
            _set_state(path, last_scan_at=now.isoformat())
            account = broker.account()
            if account.get("status") != "ACTIVE" or account.get("trading_blocked") or account.get("account_blocked"):
                raise BrokerError("The Alpaca paper account is not active for trading.", 409)
            if account.get("currency") != "USD":
                raise BrokerError("The autopilot expects a USD Alpaca paper account.", 409)
            scored = research.score_symbols(path, limit=400)
            by_symbol = {row["symbol"]: row for row in scored}
            context = _context(path, settings, True)
            if settings.exit_on_bearish_evidence:
                for symbol, plan in context["autopilot_plans"].items():
                    row = by_symbol.get(symbol)
                    if row and row["score"] < settings.exit_score:
                        _exit(path, broker, plan, row, settings)
                context = _context(path, settings, True)
            candidates = [row for row in scored if classify(row, settings, context)[0] == "BUY"]
            if not candidates:
                reason = ("All position slots are in use." if context["capacity_left"] <= 0 else
                          "Daily buy limit reached." if context["buys_left_today"] <= 0 else
                          "No symbol clears every buy gate right now.")
                _set_state(path, status="scanning", message=reason)
                return
            usd_per_eur = Decimal(str(rate_provider()["usd_per_eur"]))
            for row in candidates[:5]:
                if _enter(path, broker, row, settings, usd_per_eur, account):
                    _set_state(path, status="bought", message=f"Submitted a paper buy for {row['symbol']}.")
                    return  # At most one new position per scan spreads entries out.
            _set_state(path, status="scanning", message="Candidates found, but none passed the price and liquidity checks.")
    except BrokerError as exc:
        _set_state(path, status="attention", message=str(exc))


def preview(path: Path, settings: AutopilotSettings, broker_factory=AlpacaPaperBroker,
            rate_provider=ecb_rate) -> dict:
    init_db(path)
    with broker_factory() as broker:
        account = broker.account()
        clock = broker.clock()
    if account.get("currency") != "USD":
        raise BrokerError("The autopilot expects a USD Alpaca paper account.", 409)
    if account.get("status") != "ACTIVE" or account.get("trading_blocked") or account.get("account_blocked"):
        raise BrokerError("The Alpaca paper account is not active for trading.", 409)
    rate = rate_provider()
    usd_per_eur = Decimal(str(rate["usd_per_eur"]))
    scored = research.score_symbols(path, limit=400)
    context = _context(path, settings, True)
    candidates = [row["symbol"] for row in scored if classify(row, settings, context)[0] == "BUY"]
    exposure_eur = settings.budget_eur_per_position * settings.max_positions
    worst_case_eur = exposure_eur * Decimal(str(settings.hard_floor_pct)) / 100
    return {
        "settings": json.loads(settings.model_dump_json()),
        "account_equity_usd": account.get("equity"), "buying_power_usd": account.get("buying_power"),
        "market_open_now": bool(clock.get("is_open")), "fx": rate,
        "budget_usd_per_position": decimal_text(settings.budget_eur_per_position * usd_per_eur),
        "max_exposure_eur": decimal_text(exposure_eur),
        "stop_loss_at_full_exposure_eur": decimal_text(worst_case_eur),
        "candidates_now": candidates[:10],
        "warnings": [
            "Paper account only. The autopilot places real orders at Alpaca's paper endpoint, with no further confirmation, every "
            f"{ENTRY_INTERVAL_SECONDS // 60} minutes while the US market is open, even when this browser is closed.",
            f"It buys at most {settings.max_new_per_day} new positions a day and holds at most {settings.max_positions}, "
            f"each about EUR {settings.budget_eur_per_position}, in whole shares, with market orders that can fill away from the last price.",
            f"Each position gets a protective stop {settings.hard_floor_pct:g}% below the fill. Gaps can fill a stop well below it; "
            f"at the stop, full exposure loses about EUR {decimal_text(worst_case_eur)} before fees and taxes.",
            "The signals are public, free and delayed: House reports can be up to 45 days old, insider filings up to 2 business days. "
            "They are evidence, not a forecast, and the weights are research-based priors, not a tested edge.",
            "Pausing the autopilot stops new buys and evidence exits. Existing positions keep their automatic stop plans until you pause those too.",
        ],
    }

