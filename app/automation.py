"""Opt-in, persistent scheduler for one paper strategy per symbol.

The worker only uses Alpaca's paper API and only sends orders for a plan the
user separately previewed and confirmed. It never uses LLM output to trade.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.broker import AlpacaPaperBroker, BrokerError, decimal_text, money, require_recent_trade
from app.strategy import StrategyInput

INTERVAL_SECONDS = 30


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, and always close (Windows keeps
    an open SQLite file locked)."""
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY, symbol TEXT NOT NULL, strategy_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            last_checked_at TEXT, status TEXT NOT NULL DEFAULT 'armed', last_error TEXT,
            high_water_usd TEXT, last_entry_usd TEXT, last_stop_order_id TEXT,
            reentry_armed INTEGER NOT NULL DEFAULT 0, next_reentry_level INTEGER NOT NULL DEFAULT 1,
            pending_reentry_client_id TEXT, pending_exit_client_id TEXT
        )""")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(plans)")}
        if "pending_exit_client_id" not in columns:
            db.execute("ALTER TABLE plans ADD COLUMN pending_exit_client_id TEXT")
        if "origin" not in columns:
            db.execute("ALTER TABLE plans ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, created_at TEXT NOT NULL,
            kind TEXT NOT NULL, message TEXT NOT NULL, order_id TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS events_plan_time ON events(plan_id, created_at DESC)")


def _event(db: sqlite3.Connection, plan_id: str, kind: str, message: str, order_id: str | None = None) -> None:
    db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", (uuid4().hex, plan_id, now_text(), kind, message, order_id))


def _update(db: sqlite3.Connection, plan_id: str, **fields: object) -> None:
    allowed = {"active", "last_checked_at", "status", "last_error", "high_water_usd", "last_entry_usd",
               "last_stop_order_id", "reentry_armed", "next_reentry_level", "pending_reentry_client_id", "pending_exit_client_id"}
    if not fields.keys() <= allowed:
        raise ValueError("Unexpected plan update field")
    setters = ", ".join(f"{key} = ?" for key in fields)
    db.execute(f"UPDATE plans SET {setters} WHERE id = ?", (*fields.values(), plan_id))


def list_plans(path: Path) -> list[dict]:
    init_db(path)
    with _connect(path) as db:
        rows = db.execute("SELECT * FROM plans ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["active"] = bool(item["active"])
            item["reentry_armed"] = bool(item["reentry_armed"])
            item["strategy"] = json.loads(item.pop("strategy_json"))
            item["events"] = [dict(event) for event in db.execute(
                "SELECT created_at, kind, message, order_id FROM events WHERE plan_id = ? ORDER BY created_at DESC LIMIT 12", (item["id"],)
            )]
            result.append(item)
        return result


def activate_plan(path: Path, strategy: StrategyInput, *, origin: str = "manual", reentry: bool = True,
                  note: str | None = None) -> dict:
    init_db(path)
    plan_id = uuid4().hex
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM plans WHERE symbol = ? AND active = 1", (strategy.symbol,)).fetchone():
            raise BrokerError("An automatic paper plan is already active for this symbol. Pause it before activating another.", 409)
        # Starting past the last ladder level switches dip re-entry off entirely.
        next_level = 1 if reentry else strategy.ladder_levels + 1
        db.execute("INSERT INTO plans (id, symbol, strategy_json, created_at, origin, next_reentry_level) VALUES (?, ?, ?, ?, ?, ?)",
                   (plan_id, strategy.symbol, strategy.model_dump_json(), now_text(), origin, next_level))
        _event(db, plan_id, "activated", note or "User confirmed the automatic paper strategy. No order was sent by activation.")
    return next(plan for plan in list_plans(path) if plan["id"] == plan_id)


def set_plan_active(path: Path, plan_id: str, active: bool, *, status: str, message: str) -> None:
    init_db(path)
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        _update(db, plan_id, active=1 if active else 0, status=status)
        _event(db, plan_id, status, message)


def pause_plan(path: Path, plan_id: str) -> dict:
    init_db(path)
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id, active FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise BrokerError("Automatic plan not found.", 404)
        if row["active"]:
            _update(db, plan_id, active=0, status="paused")
            _event(db, plan_id, "paused", "Automatic checks paused. Existing Alpaca orders were left unchanged; review them separately.")
    return next(plan for plan in list_plans(path) if plan["id"] == plan_id)


def _plan_error(db: sqlite3.Connection, plan_id: str, message: str) -> None:
    prior = db.execute("SELECT kind, message FROM events WHERE plan_id = ? ORDER BY created_at DESC LIMIT 1", (plan_id,)).fetchone()
    _update(db, plan_id, last_checked_at=now_text(), status="attention", last_error=message)
    if prior is None or prior["kind"] != "attention" or prior["message"] != message:
        _event(db, plan_id, "attention", message)


def _evaluate(db: sqlite3.Connection, plan: sqlite3.Row, broker: AlpacaPaperBroker) -> None:
    strategy = StrategyInput.model_validate_json(plan["strategy_json"])
    symbol = strategy.symbol
    position = broker.position(symbol)
    orders = broker.open_orders(symbol)
    buys = [order for order in orders if order.get("side") == "buy"]
    sells = [order for order in orders if order.get("side") == "sell"]
    trade = broker.latest_trade(symbol)
    require_recent_trade(trade)
    price = Decimal(str(trade["p"]))
    owner = f"guardrail-auto-{plan['id'][:12]}-"
    _update(db, plan["id"], last_checked_at=now_text(), last_error=None)
    if plan["pending_exit_client_id"]:
        pending_exit = broker.order_by_client_id(plan["pending_exit_client_id"])
        if pending_exit:
            _update(db, plan["id"], last_stop_order_id=pending_exit.get("id"), pending_exit_client_id=None)
            db.commit()
            plan = db.execute("SELECT * FROM plans WHERE id = ?", (plan["id"],)).fetchone()

    if position and Decimal(str(position.get("qty", "0"))) > 0:
        qty = Decimal(str(position["qty"]))
        if qty != qty.to_integral_value():
            _plan_error(db, plan["id"], "Fractional position found; automatic whole-share stop management is paused for this check.")
            return
        entry = Decimal(str(position["avg_entry_price"]))
        reset_high = plan["last_entry_usd"] is None or Decimal(plan["last_entry_usd"]) != entry or plan["pending_reentry_client_id"] is not None
        previous_high = price if reset_high or not plan["high_water_usd"] else Decimal(plan["high_water_usd"])
        high = max(price, previous_high)
        _update(db, plan["id"], last_entry_usd=str(entry), high_water_usd=str(high),
                pending_reentry_client_id=None, reentry_armed=0)
        if plan["pending_reentry_client_id"]:
            _update(db, plan["id"], next_reentry_level=plan["next_reentry_level"] + 1)
            _event(db, plan["id"], "reentry_filled", f"Re-entry position detected: {position['qty']} {symbol} at broker average ${decimal_text(entry)}.")
        floor = entry * (1 - Decimal(str(strategy.hard_floor_pct)) / 100)
        trigger = entry * (1 + Decimal(str(strategy.trail_trigger_pct)) / 100)
        if high >= trigger:
            floor = max(floor, high * (1 - Decimal(str(strategy.trail_distance_pct)) / 100))
        desired = money(floor)
        if len(sells) > 1 or (sells and not str(sells[0].get("client_order_id", "")).startswith(owner)):
            _plan_error(db, plan["id"], "An unrelated or multiple sell order exists. Scheduler will not replace or add a stop; review Alpaca orders.")
            return
        if sells:
            existing = sells[0]
            if existing.get("type") != "stop" or Decimal(str(existing.get("qty", "0"))) != qty or not existing.get("stop_price"):
                _plan_error(db, plan["id"], "Automatic stop type or quantity no longer matches the full position; review broker orders.")
                return
            current_stop = Decimal(str(existing["stop_price"]))
            _update(db, plan["id"], last_stop_order_id=existing["id"])
            if desired > current_stop and desired < price:
                replaced = broker.replace_stop(existing["id"], desired)
                _update(db, plan["id"], last_stop_order_id=replaced.get("id", existing["id"]), status="floor_raised")
                _event(db, plan["id"], "floor_raised", f"Raised full-position {symbol} stop from ${decimal_text(current_stop)} to ${decimal_text(desired)} after a new high of ${decimal_text(high)}.", replaced.get("id"))
            else:
                _update(db, plan["id"], status="stop_active")
            return
        if plan["last_stop_order_id"]:
            prior_stop = broker.order(plan["last_stop_order_id"])
            if prior_stop.get("status") in {"accepted", "new", "pending_new", "pending_replace", "partially_filled"}:
                _update(db, plan["id"], status="waiting_for_stop_visibility")
                return
        if price <= desired:
            client_id = plan["pending_exit_client_id"] or owner + "emergency-" + uuid4().hex[:8]
            _update(db, plan["id"], pending_exit_client_id=client_id, status="emergency_exit_submitting")
            db.commit()
            order = broker.submit({"symbol": symbol, "qty": str(int(qty)), "side": "sell", "type": "market",
                                   "time_in_force": "day", "client_order_id": client_id})
            _update(db, plan["id"], last_stop_order_id=order.get("id"), pending_exit_client_id=None, status="emergency_exit_submitted")
            _event(db, plan["id"], "emergency_exit", f"No protective sell order existed and IEX price ${decimal_text(price)} was at/below floor ${decimal_text(desired)}; submitted full-position paper market exit.", order.get("id"))
            return
        client_id = plan["pending_exit_client_id"] or owner + "stop-" + uuid4().hex[:8]
        _update(db, plan["id"], pending_exit_client_id=client_id, status="stop_submitting")
        db.commit()
        order = broker.submit({"symbol": symbol, "qty": str(int(qty)), "side": "sell", "type": "stop",
                               "time_in_force": "gtc", "stop_price": decimal_text(desired), "client_order_id": client_id})
        _update(db, plan["id"], last_stop_order_id=order.get("id"), pending_exit_client_id=None, status="stop_active")
        _event(db, plan["id"], "stop_created", f"Created full-position paper stop for {int(qty)} {symbol} at ${decimal_text(desired)}.", order.get("id"))
        return

    # No position: do not infer that a manual close is permission to re-enter.
    _update(db, plan["id"], high_water_usd=None)
    if plan["pending_exit_client_id"]:
        _plan_error(db, plan["id"], "Protective exit status is uncertain while no position is visible. Review the Alpaca account before re-entry.")
        return
    if buys:
        _update(db, plan["id"], status="waiting_for_buy_fill")
        return
    if sells:
        _plan_error(db, plan["id"], "Sell order exists without a position; review broker state before re-entry.")
        return
    if plan["pending_reentry_client_id"]:
        pending = broker.order_by_client_id(plan["pending_reentry_client_id"])
        if pending and pending.get("status") in {"accepted", "new", "pending_new", "partially_filled"}:
            _update(db, plan["id"], status="waiting_for_reentry_fill")
            return
        _plan_error(db, plan["id"], "Re-entry order is no longer open but no position is visible. Automatic re-entry is paused for review.")
        _update(db, plan["id"], active=0)
        return
    if not plan["last_stop_order_id"] or not plan["last_entry_usd"]:
        _update(db, plan["id"], status="waiting_for_first_position")
        return
    if not plan["reentry_armed"]:
        prior_stop = broker.order(plan["last_stop_order_id"])
        if prior_stop.get("status") != "filled":
            _plan_error(db, plan["id"], "Position disappeared without a confirmed fill of the plan's protective exit. Automatic re-entry paused.")
            _update(db, plan["id"], active=0)
            return
        _update(db, plan["id"], reentry_armed=1)
        _event(db, plan["id"], "reentry_armed", "Plan-owned protective exit filled; dip re-entry is now eligible.", prior_stop.get("id"))
    level = plan["next_reentry_level"]
    if level > strategy.ladder_levels:
        _update(db, plan["id"], status="reentry_levels_exhausted")
        return
    threshold = Decimal(plan["last_entry_usd"]) * (1 - Decimal(str(strategy.ladder_step_pct)) * level / 100)
    if threshold <= 0:
        _update(db, plan["id"], status="reentry_level_invalid")
        return
    if price > threshold:
        _update(db, plan["id"], status="waiting_for_reentry_level")
        return
    client_id = owner + f"reentry-{level}-" + uuid4().hex[:6]
    # Durable intent before the external write prevents a second buy after a crash.
    _update(db, plan["id"], pending_reentry_client_id=client_id, status="reentry_submitting")
    db.commit()
    order = broker.submit({"symbol": symbol, "qty": str(strategy.ladder_shares), "side": "buy", "type": "limit",
                           "time_in_force": "gtc", "limit_price": decimal_text(money(threshold)), "client_order_id": client_id})
    _update(db, plan["id"], status="waiting_for_reentry_fill")
    _event(db, plan["id"], "reentry_submitted", f"Submitted level {level} paper re-entry limit for {strategy.ladder_shares} {symbol} at ${decimal_text(money(threshold))}.", order.get("id"))


def run_cycle(path: Path, broker_factory=AlpacaPaperBroker) -> None:
    init_db(path)
    with _connect(path) as db:
        plans = db.execute("SELECT * FROM plans WHERE active = 1 ORDER BY created_at").fetchall()
        if not plans:
            return
        try:
            with broker_factory() as broker:
                clock = broker.clock()
                if not clock.get("is_open"):
                    for plan in plans:
                        _update(db, plan["id"], last_checked_at=now_text(), status="market_closed")
                    return
                account = broker.account()
                if account.get("status") != "ACTIVE" or account.get("trading_blocked") or account.get("account_blocked"):
                    raise BrokerError("Alpaca paper account is not active for trading.", 409)
                for plan in plans:
                    try:
                        _evaluate(db, plan, broker)
                    except (BrokerError, ValueError, KeyError, ArithmeticError) as exc:
                        _plan_error(db, plan["id"], str(exc))
        except BrokerError as exc:
            for plan in plans:
                _plan_error(db, plan["id"], str(exc))


class AutomationWorker:
    def __init__(self, path: Path, after_cycle=None):
        self.path = path
        # Runs in this same thread, so it never races the stop management above.
        self.after_cycle = after_cycle
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        init_db(self.path)
        self.thread = threading.Thread(target=self._loop, name="paper-automation", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                run_cycle(self.path)
            except Exception:
                # Do not kill a long-running monitor on an unexpected error.
                logging.exception("Automatic paper-plan check failed; will retry")
            if self.after_cycle is not None:
                try:
                    self.after_cycle(self.path)
                except Exception:
                    logging.exception("Autopilot check failed; will retry")
            self.stop_event.wait(INTERVAL_SECONDS)
