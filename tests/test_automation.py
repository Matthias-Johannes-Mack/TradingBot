import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.automation import activate_plan, list_plans, pause_plan, run_cycle
from app.strategy import StrategyInput


class SchedulerBroker:
    def __init__(self):
        self.open = True
        self.position_data = {"symbol": "TSLA", "qty": "1", "avg_entry_price": "100"}
        self.price = 100
        self.orders = []
        self.all_orders = []
        self.submissions = []
        self.replacements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def clock(self):
        return {"is_open": self.open}

    def account(self):
        return {"status": "ACTIVE", "trading_blocked": False, "account_blocked": False}

    def position(self, _symbol):
        return self.position_data

    def open_orders(self, _symbol):
        return [order for order in self.orders if order["status"] in {"new", "accepted"}]

    def latest_trade(self, _symbol):
        return {"p": self.price, "t": datetime.now(timezone.utc).isoformat()}

    def submit(self, payload):
        existing = self.order_by_client_id(payload["client_order_id"])
        if existing:
            return existing
        order = {**payload, "id": f"order-{len(self.all_orders) + 1}", "status": "new"}
        self.orders.append(order)
        self.all_orders.append(order)
        self.submissions.append(order)
        return order

    def replace_stop(self, order_id, price):
        order = self.order(order_id)
        order["stop_price"] = f"{price:.2f}"
        self.replacements.append((order_id, order["stop_price"]))
        return order

    def order(self, order_id):
        return next(order for order in self.all_orders if order["id"] == order_id)

    def order_by_client_id(self, client_id):
        return next((order for order in self.all_orders if order["client_order_id"] == client_id), None)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite"
        self.broker = SchedulerBroker()
        self.strategy = StrategyInput(symbol="TSLA", shares_owned=1, entry_price=100, current_price=100,
                                      hard_floor_pct=10, trail_trigger_pct=10, trail_distance_pct=5,
                                      ladder_step_pct=20, ladder_shares=2, ladder_levels=2)

    def test_no_action_before_confirmation_or_when_market_closed(self):
        run_cycle(self.path, lambda: self.broker)
        self.assertEqual(self.broker.submissions, [])
        plan = activate_plan(self.path, self.strategy)
        self.broker.open = False
        run_cycle(self.path, lambda: self.broker)
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(list_plans(self.path)[0]["status"], "market_closed")
        pause_plan(self.path, plan["id"])
        self.broker.open = True
        run_cycle(self.path, lambda: self.broker)
        self.assertEqual(self.broker.submissions, [])

    def test_stop_moves_up_only_then_one_reentry_after_own_fill(self):
        activate_plan(self.path, self.strategy)
        run_cycle(self.path, lambda: self.broker)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.submissions[0]["stop_price"], "90.00")
        self.broker.price = 115
        run_cycle(self.path, lambda: self.broker)
        self.assertEqual(self.broker.replacements[-1][1], "109.25")
        self.broker.price = 110
        run_cycle(self.path, lambda: self.broker)
        self.assertEqual(len(self.broker.replacements), 1)
        self.broker.orders[0]["status"] = "filled"
        self.broker.position_data = None
        self.broker.price = 79
        run_cycle(self.path, lambda: self.broker)
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertEqual(self.broker.submissions[1]["side"], "buy")
        self.assertEqual(self.broker.submissions[1]["limit_price"], "80.00")
        run_cycle(self.path, lambda: self.broker)
        self.assertEqual(len(self.broker.submissions), 2)
        self.broker.orders[1]["status"] = "filled"
        self.broker.position_data = {"symbol": "TSLA", "qty": "2", "avg_entry_price": "80"}
        self.broker.price = 80
        run_cycle(self.path, lambda: self.broker)
        self.assertEqual(len(self.broker.submissions), 3)
        self.assertEqual(self.broker.submissions[2]["stop_price"], "72.00")
        self.assertEqual(list_plans(self.path)[0]["next_reentry_level"], 2)


if __name__ == "__main__":
    unittest.main()
