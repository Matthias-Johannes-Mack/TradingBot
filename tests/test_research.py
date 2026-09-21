import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import research
from app.sources import congress, insiders, tickers, trends
from app.sources.base import Signal, utc_now

COMPANIES = [
    {"ticker": "CCI", "title": "Crown Castle Inc."}, {"ticker": "ACN", "title": "Accenture plc"},
    {"ticker": "NVDA", "title": "NVIDIA CORP"}, {"ticker": "TPC", "title": "Tutor Perini Corp"},
    {"ticker": "LMT", "title": "LOCKHEED MARTIN CORP"}, {"ticker": "IT", "title": "Gartner Inc"},
]


def use_fake_universe(test: unittest.TestCase) -> None:
    previous = (tickers._cache, tickers._fetched_at)
    tickers._cache, tickers._fetched_at = tickers._build(COMPANIES), utc_now()

    def restore():
        tickers._cache, tickers._fetched_at = previous
    test.addCleanup(restore)


HOUSE_REPORT = """Name: Hon. Robert J. Wittman
Status: Member
State/District: VA01
ID Owner Asset Transaction
Type
Date Notification
Date
Amount Cap.
Gains >
$200?
Crown Castle Inc. Common Stock
(CCI) [ST]
S 06/30/2026 07/02/2026 $1,001 - $15,000
Accenture plc Class A Ordinary Shares
(ACN) [ST]
SP P 07/01/2026 07/02/2026 $250,001 -
$500,000
Treasury Bill (3-Month, Matures
10/15/2026) [GS]
P 07/13/2026 07/13/2026 $15,001 -
$50,000
Made-up Holdings (ZZZZ) [ST]
P 07/01/2026 07/02/2026 $1,001 - $15,000
"""

FORM4 = """<SEC-DOCUMENT>
<XML>
<ownershipDocument>
  <issuer><issuerCik>0001045810</issuerCik><issuerTradingSymbol>nvda</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-17</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>150.00</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-17</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>9000</value></transactionShares>
        <transactionPricePerShare><value>10.00</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
</XML>
</SEC-DOCUMENT>"""


def signal(symbol, source, direction, magnitude, *, days_ago=0, kind=None, key=None):
    return Signal(symbol=symbol, source=source, kind=kind or f"{source}_{'buy' if direction > 0 else 'sell'}",
                  direction=direction, magnitude=magnitude, event_at=utc_now() - timedelta(days=days_ago),
                  headline=f"{symbol} {source}", dedupe_key=key or f"{symbol}:{source}:{days_ago}:{magnitude}:{kind}")


class SourceParsingTests(unittest.TestCase):
    def setUp(self):
        use_fake_universe(self)

    def test_house_report_rows_become_dated_directional_signals(self):
        filing = {"doc": "20034916", "year": 2026, "filed_at": datetime(2026, 7, 2, tzinfo=timezone.utc),
                  "member": "Robert J. Wittman", "district": "VA01"}
        signals = congress.signals_from(filing, HOUSE_REPORT)
        by_symbol = {item.symbol: item for item in signals}
        # Treasury bills and symbols outside the SEC universe are dropped.
        self.assertEqual(set(by_symbol), {"CCI", "ACN"})
        self.assertEqual(by_symbol["CCI"].direction, -1)
        self.assertEqual(by_symbol["ACN"].direction, 1)
        # A wrapped amount range still parses, and a larger trade weighs more.
        self.assertEqual(by_symbol["ACN"].detail["amount_high_usd"], "500,000")
        self.assertGreater(by_symbol["ACN"].magnitude, by_symbol["CCI"].magnitude)
        self.assertEqual(by_symbol["ACN"].event_at, filing["filed_at"])

    def test_form4_keeps_open_market_purchase_and_ignores_option_exercise(self):
        signals = insiders.signals_from("1045810", "0001234567-26-000001", FORM4)
        self.assertEqual(len(signals), 1)
        only = signals[0]
        self.assertEqual((only.symbol, only.direction, only.kind), ("NVDA", 1, "insider_buy"))
        self.assertIn("CFO", only.headline)
        self.assertAlmostEqual(only.detail["value_usd"], 300000.0)

    def test_name_matching_rejects_ambiguous_words_and_strips_suffixes(self):
        self.assertEqual(tickers.symbol_for_name("LOCKHEED MARTIN CORPORATION"), "LMT")
        self.assertEqual(tickers.symbol_for_name("Tutor Perini Corporation"), "TPC")
        self.assertIsNone(tickers.symbol_for_name("Unknown Contractor LLC"))
        self.assertFalse(tickers.known("IT"))
        self.assertTrue(tickers.known("NVDA"))

    def test_trends_rise_is_supportive_but_a_spike_is_a_warning(self):
        flat = [20.0] * 30
        rising = trends.momentum_signal("NVDA", "NVIDIA stock", flat + [30.0] * 7)
        spike = trends.momentum_signal("NVDA", "NVIDIA stock", flat + [90.0] * 7)
        quiet = trends.momentum_signal("NVDA", "NVIDIA stock", flat + [21.0] * 7)
        self.assertEqual((rising.kind, rising.direction), ("trends_rising", 1))
        self.assertEqual((spike.kind, spike.direction), ("trends_spike", -1))
        self.assertIsNone(quiet)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite"
        research.init_db(self.path)

    def test_duplicate_evidence_is_counted_once(self):
        item = signal("NVDA", "insiders", 1, 0.8, key="same")
        self.assertEqual(research.store(self.path, [item, item]), 1)
        self.assertEqual(research.store(self.path, [item]), 0)

    def test_old_evidence_decays_and_bearish_evidence_lowers_the_score(self):
        research.store(self.path, [
            signal("NEW", "insiders", 1, 0.8, days_ago=1),
            signal("OLD", "insiders", 1, 0.8, days_ago=50),
            signal("BAD", "insiders", -1, 0.8, days_ago=1),
        ])
        scores = {row["symbol"]: row["score"] for row in research.score_symbols(self.path)}
        self.assertGreater(scores["NEW"], scores["OLD"])
        self.assertGreater(scores["OLD"], 50)
        self.assertLess(scores["BAD"], 50)

    def test_one_noisy_source_is_capped_but_independent_sources_add_up(self):
        research.store(self.path, [signal("CROWD", "retail", 1, 1.0, key=f"r{i}") for i in range(30)])
        research.store(self.path, [
            signal("BOTH", "insiders", 1, 0.5), signal("BOTH", "congress", 1, 0.5), signal("BOTH", "contracts", 1, 0.5),
        ])
        rows = {row["symbol"]: row for row in research.score_symbols(self.path)}
        self.assertLessEqual(rows["CROWD"]["sources"]["retail"], research.SOURCES["retail"]["cap"])
        self.assertEqual(rows["BOTH"]["conviction"], 3)
        self.assertGreater(rows["BOTH"]["score"], rows["CROWD"]["score"])

    def test_caution_kinds_are_reported(self):
        research.store(self.path, [signal("HOT", "trends", -1, 0.6, kind="trends_spike"),
                                   signal("HOT", "insiders", 1, 0.9)])
        row = research.score_symbols(self.path)[0]
        self.assertEqual(row["caution_kinds"], ["trends_spike"])

    def test_failing_source_is_recorded_without_raising(self):
        class Broken:
            @staticmethod
            def collect(_context):
                raise research.SourceError("feed down")
        original = research.SOURCES["retail"]["module"]
        research.SOURCES["retail"]["module"] = Broken
        self.addCleanup(lambda: research.SOURCES["retail"].__setitem__("module", original))
        result = research.run_source(self.path, "retail", [])
        self.assertEqual((result["status"], result["message"]), ("unavailable", "feed down"))
        status = {item["source"]: item for item in research.status(self.path)["sources"]}
        self.assertEqual(status["retail"]["status"], "unavailable")
        self.assertNotIn("retail", research.due_sources(self.path))


if __name__ == "__main__":
    unittest.main()
