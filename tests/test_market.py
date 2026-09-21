import unittest
from datetime import datetime, timezone

from app import market
from app.broker import BrokerError


class CalendarBroker:
    def __init__(self, days):
        self.days = days

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def calendar(self, _start, _end):
        return self.days


class MarketHoursTests(unittest.TestCase):
    def hours(self, days, now):
        return market.hours(lambda: CalendarBroker(days), now=now, use_cache=False)

    def test_new_york_times_become_utc_across_daylight_saving(self):
        days = [{"date": "2026-09-22", "open": "09:30", "close": "16:00"},
                {"date": "2026-11-27", "open": "09:30", "close": "13:00"}]
        result = self.hours(days, datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc))
        summer, early = result["sessions"]
        # EDT is UTC-4 in September; EST is UTC-5 after the November switch.
        self.assertEqual((summer["open_at"], summer["close_at"]), ("2026-09-22T13:30:00+00:00", "2026-09-22T20:00:00+00:00"))
        self.assertEqual(early["close_at"], "2026-11-27T18:00:00+00:00")
        self.assertTrue(early["early_close"])
        self.assertFalse(summer["early_close"])
        self.assertEqual(result["source"], "alpaca")

    def test_open_and_closed_state_follows_the_session(self):
        days = [{"date": "2026-09-21", "open": "09:30", "close": "16:00"},
                {"date": "2026-09-22", "open": "09:30", "close": "16:00"}]
        during = self.hours(days, datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc))
        self.assertTrue(during["is_open"])
        self.assertEqual(during["current"]["date"], "2026-09-21")
        self.assertEqual(during["next"]["date"], "2026-09-22")
        after = self.hours(days, datetime(2026, 9, 21, 20, 30, tzinfo=timezone.utc))
        self.assertFalse(after["is_open"])
        self.assertIsNone(after["current"])
        self.assertEqual(after["next"]["date"], "2026-09-22")

    def test_without_broker_keys_the_standard_week_is_shown(self):
        def no_keys():
            raise BrokerError("Add APCA_API_KEY_ID and APCA_API_SECRET_KEY.", 503)
        # Saturday: the next standard session is Monday 09:30 New York.
        result = market.hours(no_keys, now=datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc), use_cache=False)
        self.assertEqual(result["source"], "schedule")
        self.assertFalse(result["is_open"])
        self.assertEqual(result["next"]["date"], "2026-09-28")
        self.assertIn("holidays", result["note"])


if __name__ == "__main__":
    unittest.main()
