"""Retail attention: r/wallstreetbets mention counts and StockTwits sentiment.

Both feeds are free and unauthenticated. They are read as a crowding gauge
rather than a buy list: moderate, rising chatter is treated as mildly
supportive, while a mention or sentiment blow-off is treated as a warning,
which is the direction the published research on retail attention points.
"""

from __future__ import annotations

from app.sources.base import (
    BROWSER_USER_AGENT, Signal, SourceContext, SourceError, clamp, fetch_json, parse_iso, utc_now,
)
from app.sources import tickers

SOURCE = "retail"
APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/{page}"
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
HEADERS = {"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"}
PAGES = 2
MIN_MENTIONS = 5
CROWDED_MENTIONS = 25
CROWDED_GROWTH = 2.0
MIN_TAGGED_MESSAGES = 8


def _mentions() -> list[Signal]:
    signals: list[Signal] = []
    now = utc_now()
    failures = 0
    for page in range(1, PAGES + 1):
        try:
            payload = fetch_json(APEWISDOM_URL.format(page=page), headers=HEADERS, timeout=30)
        except SourceError:
            failures += 1
            continue
        for row in (payload.get("results", []) if isinstance(payload, dict) else []):
            symbol = str(row.get("ticker", "")).strip().upper()
            try:
                mentions = int(row.get("mentions") or 0)
                previous = int(row.get("mentions_24h_ago") or 0)
                upvotes = int(row.get("upvotes") or 0)
            except (TypeError, ValueError):
                continue
            if mentions < MIN_MENTIONS or not tickers.known(symbol):
                continue
            growth = (mentions - previous) / max(previous, 3)
            crowded = mentions >= CROWDED_MENTIONS and growth >= CROWDED_GROWTH
            if not crowded and growth < 0.25:
                continue
            signals.append(Signal(
                symbol=symbol, source=SOURCE,
                kind="retail_crowding" if crowded else "retail_buzz",
                direction=-1 if crowded else 1,
                magnitude=round(0.4 + clamp((growth - CROWDED_GROWTH) / 4) * 0.6 if crowded
                                else clamp(growth / 2) * 0.5, 4),
                event_at=now,
                headline=(f"r/wallstreetbets mentions of {symbol} "
                          f"{'spiked' if crowded else 'rose'} to {mentions} from {previous} in 24h"
                          f" ({upvotes} upvotes)" + (" - crowded trade risk" if crowded else "")),
                dedupe_key=f"wsb:{symbol}:{now:%Y-%m-%dT%H}",
                url="https://apewisdom.io/wallstreetbets/",
                detail={"mentions": mentions, "mentions_24h_ago": previous, "upvotes": upvotes,
                        "growth": round(growth, 3), "rank": row.get("rank")},
            ))
    if failures == PAGES:
        raise SourceError("The r/wallstreetbets mention feed could not be read.")
    return signals


def _sentiment(symbol: str) -> Signal | None:
    payload = fetch_json(STOCKTWITS_URL.format(symbol=symbol), headers=HEADERS, timeout=30)
    messages = payload.get("messages", []) if isinstance(payload, dict) else []
    bullish = bearish = 0
    newest = None
    for message in messages:
        tag = ((message.get("entities") or {}).get("sentiment") or {}).get("basic")
        newest = newest or parse_iso(message.get("created_at"))
        if tag == "Bullish":
            bullish += 1
        elif tag == "Bearish":
            bearish += 1
    tagged = bullish + bearish
    if tagged < MIN_TAGGED_MESSAGES:
        return None
    share = bullish / tagged
    if 0.35 <= share <= 0.75:
        return None
    # A near-unanimous board is a crowding warning, not a confirmation.
    crowded = share >= 0.9 and tagged >= 15
    direction = -1 if (crowded or share < 0.35) else 1
    kind = "retail_crowding" if crowded else "retail_sentiment"
    return Signal(
        symbol=symbol, source=SOURCE, kind=kind, direction=direction,
        magnitude=round(clamp(abs(share - 0.5) * 2) * (0.8 if crowded else 0.5), 4),
        event_at=newest or utc_now(),
        headline=(f"StockTwits {symbol}: {bullish} bullish vs {bearish} bearish tags in the latest "
                  f"{tagged} tagged posts" + (" - one-sided, crowded" if crowded else "")),
        dedupe_key=f"stocktwits:{symbol}:{utc_now():%Y-%m-%dT%H}",
        url=f"https://stocktwits.com/symbol/{symbol}",
        detail={"bullish": bullish, "bearish": bearish, "bullish_share": round(share, 3)},
    )


def collect(context: SourceContext) -> list[Signal]:
    signals = _mentions()
    for symbol in context.symbols[: context.budget]:
        try:
            found = _sentiment(symbol)
        except SourceError:
            continue
        if found is not None:
            signals.append(found)
    return signals
