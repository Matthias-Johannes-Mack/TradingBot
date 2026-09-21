import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main
from app import autopilot, research, watchlist
from app.automation import list_plans, run_cycle
from app.autopilot import AutopilotSettings
from app.broker import BrokerError
from tests.test_research import signal, use_fake_universe

RATE = {"usd_per_eur": "1.10", "reference_date": "2026-09-18", "source": "ECB daily reference rate"}


class FakeBroker:
    """Just enough of the Alpaca paper API for the autopilot and plan worker."""

    def __init__(self):
        self.open = True
        self.prices = {"NVDA": 100.0, "TPC": 40.0, "LMT": 450.0}
        self.positions: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.submissions: list[dict] = []
        self.cancels: list[str] = []
        self.daily_volume = 50_000
        self.reject_next = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def clock(self):
        return {"is_open": self.open}

    def account(self):
        return {"status": "ACTIVE", "currency": "USD", "trading_blocked": False, "account_blocked": False,
                "equity": "100000", "buying_power": "100000"}

    def asset(self, symbol):
        return {"class": "us_equity", "tradable": True, "status": "active", "symbol": symbol}

    def position(self, symbol):
        return self.positions.get(symbol)

    def open_orders(self, symbol):
        return [order for order in self.orders if order["symbol"] == symbol and order["status"] in {"new", "accepted"}]

    def latest_trade(self, symbol):
        return {"p": self.prices[symbol], "t": datetime.now(timezone.utc).isoformat()}

    def candles(self, symbol, _timeframe):
        return [{"c": self.prices[symbol], "v": self.daily_volume} for _ in range(20)]

    def order_by_client_id(self, client_id):
        return next((order for order in self.orders if order["client_order_id"] == client_id), None)

    def order(self, order_id):
        return next(order for order in self.orders if order["id"] == order_id)

    def submit(self, payload):
        if self.reject_next:
            self.reject_next = False
            raise BrokerError("Alpaca: insufficient qty", 502)
        order = {**payload, "id": f"order-{len(self.orders) + 1}", "status": "accepted"}
        if payload["type"] == "market":
            order.update(status="filled", filled_qty=payload["qty"], filled_avg_price=str(self.prices[payload["symbol"]]))
            qty = float(payload["qty"]) * (1 if payload["side"] == "buy" else -1)
            held = float(self.positions.get(payload["symbol"], {}).get("qty", 0)) + qty
            if held > 0:
                self.positions[payload["symbol"]] = {"symbol": payload["symbol"], "qty": str(int(held)),
                                                     "avg_entry_price": str(self.prices[payload["symbol"]])}
            else:
                self.positions.pop(payload["symbol"], None)
        self.orders.append(order)
        self.submissions.append(payload)
        return order

    def cancel(self, order_id):
        self.cancels.append(order_id)
        self.order(order_id)["status"] = "canceled"

    def replace_stop(self, order_id, stop_price):
        order = self.order(order_id)
        order["stop_price"] = str(stop_price)
        return order

    def fill_stop(self, symbol):
        stop = next(order for order in self.orders if order["symbol"] == symbol and order["type"] == "stop")
        stop.update(status="filled", filled_avg_price=stop["stop_price"])
        self.positions.pop(symbol, None)


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        use_fake_universe(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite"
        self.broker = FakeBroker()
        autopilot.init_db(self.path)
        with autopilot._screen_lock:
            autopilot._screen_cache.clear()

    def strong(self, symbol, key=""):
        research.store(self.path, [
            signal(symbol, "insiders", 1, 0.9, key=f"{symbol}i{key}"),
            signal(symbol, "congress", 1, 0.8, key=f"{symbol}c{key}"),
            signal(symbol, "contracts", 1, 0.8, key=f"{symbol}k{key}"),
        ])

    def cycle(self):
        autopilot.run_cycle(self.path, broker_factory=lambda: self.broker, rate_provider=lambda: RATE)

    def force_scan(self):
        autopilot._set_state(self.path, last_scan_at=None)

    def test_nothing_happens_while_switched_off(self):
        self.strong("NVDA")
        self.cycle()
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(autopilot.board(self.path)["rows"][0]["action"], "BUY_BLOCKED")

    def test_buys_a_qualified_candidate_and_hands_it_to_a_stop_plan(self):
        self.strong("NVDA")
        autopilot.activate(self.path, AutopilotSettings(budget_eur_per_position=500))
        self.cycle()
        self.assertEqual(len(self.broker.submissions), 1)
        buy = self.broker.submissions[0]
        # EUR 500 at 1.10 USD/EUR is USD 550, which buys 5 whole shares at USD 100.
        self.assertEqual((buy["symbol"], buy["side"], buy["type"], buy["qty"]), ("NVDA", "buy", "market", "5"))
        self.assertTrue(buy["client_order_id"].startswith("guardrail-autopilot-buy-"))
        plan = next(plan for plan in list_plans(self.path) if plan["symbol"] == "NVDA")
        self.assertEqual((plan["origin"], plan["active"]), ("autopilot", True))
        decision = autopilot.decisions(self.path, symbol="NVDA")[0]
        self.assertEqual((decision["action"], decision["state"]), ("buy", "submitted"))
        self.assertTrue(decision["evidence"])
        # The normal plan worker then places the protective stop 8% below the fill.
        run_cycle(self.path, broker_factory=lambda: self.broker)
        stop = self.broker.submissions[-1]
        self.assertEqual((stop["type"], stop["stop_price"], stop["qty"]), ("stop", "92.00", "5"))
        self.cycle()
        self.assertEqual(autopilot.decisions(self.path, symbol="NVDA")[0]["state"], "filled")
        self.assertEqual(autopilot.board(self.path)["rows"][0]["action"], "HOLD")

    def test_one_buy_per_scan_and_a_daily_limit(self):
        for symbol in ("NVDA", "TPC", "LMT"):
            self.strong(symbol)
        autopilot.activate(self.path, AutopilotSettings(max_new_per_day=2))
        self.cycle()
        self.cycle()  # Within the scan interval: no second buy.
        self.assertEqual(len([s for s in self.broker.submissions if s["side"] == "buy"]), 1)
        self.force_scan()
        self.cycle()
        self.force_scan()
        self.cycle()
        buys = [s["symbol"] for s in self.broker.submissions if s["side"] == "buy"]
        self.assertEqual(len(buys), 2)
        remaining = next(row for row in autopilot.board(self.path)["rows"] if row["symbol"] not in buys)
        self.assertEqual(remaining["action"], "BUY_BLOCKED")
        self.assertIn("today's limit", remaining["reason"])

    def test_gates_single_weak_source_and_crowded_names(self):
        research.store(self.path, [signal("NVDA", "congress", 1, 0.9)])
        research.store(self.path, [signal("TPC", "insiders", 1, 0.9), signal("TPC", "contracts", 1, 0.9),
                                   signal("TPC", "trends", -1, 0.4, kind="trends_spike")])
        autopilot.activate(self.path, AutopilotSettings())
        self.cycle()
        self.assertEqual(self.broker.submissions, [])
        actions = {row["symbol"]: row for row in autopilot.board(self.path)["rows"]}
        self.assertEqual(actions["TPC"]["action"], "WATCH")
        self.assertIn("caution", actions["TPC"]["reason"])

    def test_illiquid_or_cheap_names_are_skipped_with_a_reason(self):
        self.strong("NVDA")
        self.broker.daily_volume = 100
        autopilot.activate(self.path, AutopilotSettings())
        self.cycle()
        self.assertEqual(self.broker.submissions, [])
        skip = autopilot.decisions(self.path, symbol="NVDA")[0]
        self.assertEqual(skip["action"], "skip")
        self.assertIn("dollar volume", skip["reason"])

    def test_rejected_buy_pauses_its_plan(self):
        self.strong("NVDA")
        self.broker.reject_next = True
        autopilot.activate(self.path, AutopilotSettings())
        self.cycle()
        decision = autopilot.decisions(self.path, symbol="NVDA")[0]
        self.assertEqual(decision["state"], "failed")
        plan = next(plan for plan in list_plans(self.path) if plan["symbol"] == "NVDA")
        self.assertFalse(plan["active"])

    def test_crash_before_submit_is_reconciled_without_a_double_buy(self):
        self.strong("NVDA")
        autopilot.activate(self.path, AutopilotSettings())
        with patch.object(self.broker, "submit", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cycle()
        with autopilot._connect(self.path) as db:
            db.execute("UPDATE decisions SET created_at = ? WHERE action = 'buy'",
                       ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
        self.cycle()
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(autopilot.decisions(self.path, symbol="NVDA")[0]["state"], "failed")

    def test_stop_out_closes_the_plan_and_starts_a_cooldown(self):
        self.strong("NVDA")
        autopilot.activate(self.path, AutopilotSettings(cooldown_days=10))
        self.cycle()
        run_cycle(self.path, broker_factory=lambda: self.broker)  # Places the stop.
        self.broker.fill_stop("NVDA")
        run_cycle(self.path, broker_factory=lambda: self.broker)  # Plan sees its own exit filled.
        self.cycle()
        plan = next(plan for plan in list_plans(self.path) if plan["symbol"] == "NVDA")
        self.assertEqual((plan["active"], plan["status"]), (False, "closed_by_stop"))
        self.assertEqual(autopilot.decisions(self.path, symbol="NVDA")[0]["action"], "stopped_out")
        self.force_scan()
        self.cycle()
        self.assertEqual(len([s for s in self.broker.submissions if s["side"] == "buy"]), 1)
        row = next(row for row in autopilot.board(self.path)["rows"] if row["symbol"] == "NVDA")
        self.assertIn("cooling down", row["reason"])

    def test_bearish_evidence_cancels_the_stop_then_sells(self):
        self.strong("NVDA")
        autopilot.activate(self.path, AutopilotSettings())
        self.cycle()
        run_cycle(self.path, broker_factory=lambda: self.broker)
        stop = next(order for order in self.broker.orders if order["type"] == "stop")
        research.store(self.path, [signal("NVDA", "insiders", -1, 1.0, key=f"sell{i}") for i in range(6)]
                       + [signal("NVDA", "congress", -1, 1.0, key=f"csell{i}") for i in range(6)])
        self.force_scan()
        self.cycle()
        self.assertEqual(self.broker.cancels, [stop["id"]])
        sell = self.broker.submissions[-1]
        self.assertEqual((sell["side"], sell["type"], sell["qty"]), ("sell", "market", "5"))
        self.assertNotIn("NVDA", self.broker.positions)
        self.cycle()
        plan = next(plan for plan in list_plans(self.path) if plan["symbol"] == "NVDA")
        self.assertEqual((plan["active"], plan["status"]), (False, "closed_by_evidence"))

    def test_waits_while_market_is_closed(self):
        self.strong("NVDA")
        self.broker.open = False
        autopilot.activate(self.path, AutopilotSettings())
        self.cycle()
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(autopilot.state(self.path)["status"], "waiting_for_market")


class WatchlistApiTests(unittest.TestCase):
    def setUp(self):
        use_fake_universe(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite"
        research.init_db(self.path)
        self.broker = FakeBroker()
        for target, value in (("AUTOMATION_DB", self.path), ("AlpacaPaperBroker", lambda: self.broker),
                              ("ecb_rate", lambda: RATE)):
            patcher = patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def test_board_lists_radar_manual_and_auto_entries(self):
        research.store(self.path, [signal("TPC", "contracts", 1, 0.5)])
        research.store(self.path, [signal("NVDA", "insiders", 1, 0.9), signal("NVDA", "congress", 1, 0.6)])
        self.assertEqual(self.client.post("/api/watchlist", json={"symbol": "lmt"}).status_code, 201)
        rows = {row["symbol"]: row for row in self.client.get("/api/watchlist").json()["rows"]}
        self.assertEqual((rows["LMT"]["origin"], rows["NVDA"]["origin"], rows["TPC"]["origin"]), ("manual", "auto", "radar"))
        self.assertEqual(self.client.post("/api/watchlist", json={"symbol": "ZZZZ"}).status_code, 422)
        self.assertEqual(self.client.delete("/api/watchlist/NVDA").status_code, 200)
        self.assertTrue(watchlist.entries(self.path)["NVDA"]["muted"])
        evidence = self.client.get("/api/watchlist/NVDA/evidence").json()
        self.assertEqual(len(evidence["evidence"]), 2)

    def test_autopilot_needs_a_fresh_preview_and_explicit_acknowledgement(self):
        preview = self.client.post("/api/autopilot/preview", json={"budget_eur_per_position": 250, "max_positions": 3})
        self.assertEqual(preview.status_code, 200)
        summary = preview.json()["summary"]
        self.assertEqual(summary["max_exposure_eur"], "750.00")
        token = preview.json()["preview_token"]
        missing_ack = self.client.post("/api/autopilot/activate", json={"preview_token": token})
        self.assertEqual(missing_ack.status_code, 422)
        activated = self.client.post("/api/autopilot/activate", json={"preview_token": token, "acknowledge_auto_orders": True})
        self.assertTrue(activated.json()["autopilot"]["enabled"])
        self.assertEqual(activated.json()["autopilot"]["settings"]["max_positions"], 3)
        reused = self.client.post("/api/autopilot/activate", json={"preview_token": token, "acknowledge_auto_orders": True})
        self.assertEqual(reused.status_code, 409)
        self.assertFalse(self.client.post("/api/autopilot/pause").json()["autopilot"]["enabled"])

    def test_invalid_settings_are_rejected(self):
        response = self.client.post("/api/autopilot/preview", json={"min_score": 90, "strong_score": 70})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
