import unittest
import os
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main
from app.broker import AlpacaPaperBroker, BrokerError
from app.strategy import StrategyInput, build_preview
from app.tax import TaxSettings, analysis, amount


RATE = {"usd_per_eur": "1.1500", "reference_date": "2026-09-18", "source": "ECB daily reference rate"}
STRATEGY = {
    "symbol": "TSLA", "shares_owned": 10, "entry_price": 100, "current_price": 100,
    "hard_floor_pct": 10, "trail_trigger_pct": 10, "trail_distance_pct": 5,
    "ladder_step_pct": 20, "ladder_shares": 20, "ladder_levels": 3,
}


class FakeBroker:
    def __init__(self):
        self.orders = []
        self.submissions = []
        self.position_data = {"symbol": "TSLA", "qty": "10", "avg_entry_price": "100.00"}
        self.price = "115.00"
        self.canceled = set()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def account(self):
        return {"status": "ACTIVE", "currency": "USD", "trading_blocked": False, "account_blocked": False, "equity": "1000"}

    def asset(self, _symbol):
        return {"class": "us_equity", "tradable": True, "status": "active"}

    def position(self, _symbol):
        return self.position_data

    def open_orders(self, _symbol):
        return [order for order in self.orders if order["id"] not in self.canceled]

    def latest_trade(self, _symbol):
        return {"p": self.price, "t": datetime.now(timezone.utc).isoformat()}

    def candles(self, _symbol, _timeframe):
        return [{"t": "2026-09-18T19:55:00Z", "o": 110, "h": 116, "l": 109, "c": 115, "v": 100}]

    def latest_bar(self, _symbol):
        return {"t": "2026-09-18T19:55:00Z", "o": 110, "h": 116, "l": 109, "c": 115, "v": 100}

    def order_by_client_id(self, client_order_id):
        return next((order for order in self.submissions if order["client_order_id"] == client_order_id), None)

    def submit(self, payload):
        existing = self.order_by_client_id(payload["client_order_id"])
        if existing:
            return existing
        order = {**payload, "id": str(len(self.submissions) + 1), "status": "new", "filled_qty": "0"}
        self.submissions.append(order)
        return order

    def cancel(self, order_id):
        self.canceled.add(order_id)

    def order(self, order_id):
        original = next(order for order in self.orders if order["id"] == order_id)
        return {**original, "status": "canceled" if order_id in self.canceled else "new"}


class BrokerTests(unittest.TestCase):
    def setUp(self):
        main.PENDING_BROKER_ORDERS.clear()
        self.fake = FakeBroker()
        self.client = TestClient(main.app)
        self.broker_patch = patch.object(main, "AlpacaPaperBroker", return_value=self.fake)
        self.rate_patch = patch.object(main, "ecb_rate", return_value=RATE)
        self.broker_patch.start()
        self.rate_patch.start()
        self.addCleanup(self.broker_patch.stop)
        self.addCleanup(self.rate_patch.stop)

    def test_euro_preview_and_usd_paper_market_order(self):
        local = build_preview(StrategyInput(**STRATEGY))
        self.assertEqual(local.currency, "EUR")
        self.assertIn("€90.00", local.orders[0].condition)
        preview = self.client.post("/api/broker/order-preview", json={"strategy": STRATEGY, "kind": "market_buy"})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["summary"]["reference_price_eur"], "100.00")
        token = preview.json()["preview_token"]
        placed = self.client.post("/api/broker/orders", json={"preview_token": token})
        self.assertEqual(placed.status_code, 200, placed.text)
        self.assertEqual(placed.json()["order"]["status"], "new")
        self.assertEqual(self.fake.submissions[0]["type"], "market")
        self.assertEqual(self.fake.submissions[0]["qty"], "10")
        self.client.post("/api/broker/orders", json={"preview_token": token})
        self.assertEqual(len(self.fake.submissions), 1)

    def test_full_position_stop_uses_broker_usd_entry(self):
        preview = self.client.post("/api/broker/order-preview", json={"strategy": STRATEGY, "kind": "hard_stop"})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["summary"]["price_usd"], "90.00")
        self.assertEqual(preview.json()["summary"]["price_eur_estimate"], "78.26")
        placed = self.client.post("/api/broker/orders", json={"preview_token": preview.json()["preview_token"]})
        self.assertEqual(placed.status_code, 200, placed.text)
        self.assertEqual(self.fake.submissions[0]["stop_price"], "90.00")

    def test_unreachable_ladder_and_share_mismatch_are_blocked(self):
        ladder = self.client.post("/api/broker/order-preview", json={"strategy": STRATEGY, "kind": "ladder_buy", "level": 1})
        self.assertEqual(ladder.status_code, 409)
        changed = {**STRATEGY, "shares_owned": 9}
        stop = self.client.post("/api/broker/order-preview", json={"strategy": changed, "kind": "hard_stop"})
        self.assertEqual(stop.status_code, 409)
        self.assertEqual(len(self.fake.submissions), 0)

    def test_trailing_switch_replaces_guardrail_stop(self):
        self.fake.orders = [{"id": "existing-stop", "symbol": "TSLA", "side": "sell", "type": "stop", "qty": "10", "stop_price": "90.00", "client_order_id": "guardrail-original", "status": "new"}]
        preview = self.client.post("/api/broker/order-preview", json={"strategy": STRATEGY, "kind": "trailing_stop"})
        self.assertEqual(preview.status_code, 200, preview.text)
        placed = self.client.post("/api/broker/orders", json={"preview_token": preview.json()["preview_token"]})
        self.assertEqual(placed.status_code, 200, placed.text)
        self.assertIn("existing-stop", self.fake.canceled)
        self.assertEqual(self.fake.submissions[0]["type"], "trailing_stop")

    def test_failed_trailing_submission_restores_hard_stop(self):
        self.fake.orders = [{"id": "existing-stop", "symbol": "TSLA", "side": "sell", "type": "stop", "qty": "10", "stop_price": "90.00", "client_order_id": "guardrail-original", "status": "new"}]
        original_submit = self.fake.submit

        def reject_trailing(payload):
            if payload["type"] == "trailing_stop":
                raise BrokerError("Simulated Alpaca rejection")
            return original_submit(payload)

        self.fake.submit = reject_trailing
        preview = self.client.post("/api/broker/order-preview", json={"strategy": STRATEGY, "kind": "trailing_stop"})
        self.assertEqual(preview.status_code, 200, preview.text)
        placed = self.client.post("/api/broker/orders", json={"preview_token": preview.json()["preview_token"]})
        self.assertEqual(placed.status_code, 502)
        self.assertIn("restored", placed.json()["detail"])
        self.assertEqual(self.fake.submissions[0]["type"], "stop")

    def test_live_endpoint_is_rejected(self):
        with patch.dict(os.environ, {"ALPACA_API_URL": "https://api.alpaca.markets/v2", "APCA_API_KEY_ID": "fake", "APCA_API_SECRET_KEY": "fake"}):
            with self.assertRaises(BrokerError):
                AlpacaPaperBroker()

    def test_tax_on_gains_only_and_bw_church_tax(self):
        settings = TaxSettings(church_tax_rate_pct=8)
        result = analysis(basis_total_eur=amount(100), quantity=1, settings=settings,
                          scenarios={"gain": amount(110), "loss": amount(90)}, basis_source="test")
        self.assertEqual(result["scenarios"]["gain"]["total_tax_eur"], "2.78")
        self.assertEqual(result["scenarios"]["gain"]["net_profit_eur"], "7.22")
        self.assertEqual(result["scenarios"]["loss"]["total_tax_eur"], "0.00")
        self.assertEqual(result["scenarios"]["loss"]["net_profit_eur"], "-10.00")

    def test_default_tax_rate_is_26375_percent_without_church_tax(self):
        settings = TaxSettings()
        self.assertEqual(settings.church_tax_rate_pct, 0)
        result = analysis(basis_total_eur=amount(100), quantity=1, settings=settings,
                          scenarios={"gain": amount(200)}, basis_source="test")
        self.assertEqual(result["marginal_tax_rate_pct"], "26.38")
        self.assertEqual(result["scenarios"]["gain"]["capital_gains_tax_eur"], "25.00")
        self.assertEqual(result["scenarios"]["gain"]["solidarity_eur"], "1.38")
        self.assertEqual(result["scenarios"]["gain"]["church_tax_eur"], "0.00")
        preview = self.client.post("/api/broker/order-preview", json={"strategy": STRATEGY, "kind": "market_buy"})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["summary"]["tax_analysis"]["marginal_tax_rate_pct"], "26.38")

    def test_profit_taking_gate_and_protective_exemption(self):
        protected = self.client.post("/api/broker/order-preview", json={"strategy": STRATEGY, "kind": "hard_stop"})
        self.assertEqual(protected.status_code, 200, protected.text)
        strategy = {**STRATEGY, "tax": {"broker_cost_basis_eur_per_share": 100, "church_tax_rate_pct": 8}}
        losing = self.client.post("/api/broker/order-preview", json={"strategy": strategy, "kind": "take_profit_limit", "target_exit_eur_per_share": 90})
        self.assertEqual(losing.status_code, 409)
        profitable = self.client.post("/api/broker/order-preview", json={"strategy": strategy, "kind": "take_profit_limit", "target_exit_eur_per_share": 110})
        self.assertEqual(profitable.status_code, 200, profitable.text)
        self.assertTrue(profitable.json()["summary"]["tax_analysis"]["scenarios"]["Profit-taking limit"]["meets_minimum"])
        with patch.object(main, "ecb_rate", return_value={**RATE, "usd_per_eur": "1.5"}):
            changed_fx = self.client.post("/api/broker/orders", json={"preview_token": profitable.json()["preview_token"]})
        self.assertEqual(changed_fx.status_code, 409)
        self.assertEqual(len(self.fake.submissions), 0)

    def test_chart_api_returns_iex_candles_without_credentials(self):
        chart = self.client.get("/api/broker/chart?symbol=TSLA&timeframe=5Min")
        self.assertEqual(chart.status_code, 200, chart.text)
        self.assertEqual(chart.json()["feed"], "IEX")
        self.assertEqual(chart.json()["bars"][0]["c"], 115)


if __name__ == "__main__":
    unittest.main()
