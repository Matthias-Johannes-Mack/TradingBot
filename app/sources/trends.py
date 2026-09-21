"""Google Trends search interest, read through the same public endpoints the
Trends website itself calls. No key, no third-party library.

Two different jobs:
  * discovery - the daily trending-search feed surfaces companies the public is
    suddenly looking up, which is how a symbol first reaches the watchlist;
  * confirmation - per-symbol interest over time, where a moderate rise is read
    as supportive and a blow-off is read as a crowding warning, since sharp
    attention spikes are associated with short-horizon reversals.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from statistics import fmean

import httpx

from app.sources.base import (
    BROWSER_USER_AGENT, Signal, SourceContext, SourceError, clamp, fetch, utc_now,
)
from app.sources import tickers

SOURCE = "trends"
HOME_URL = "https://trends.google.com/trends/explore?geo={geo}"
EXPLORE_URL = "https://trends.google.com/trends/api/explore"
MULTILINE_URL = "https://trends.google.com/trends/api/widgetdata/multiline"
TRENDING_RSS = "https://trends.google.com/trending/rss?geo={geo}"
HEADERS = {"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
GEO = "US"
TIMEFRAME = "today 3-m"
RECENT_DAYS = 7
BASELINE_DAYS = 30
SPIKE_RATIO = 1.5


def _payload(text: str) -> dict:
    """Google prefixes these responses with an anti-JSON-hijacking guard."""
    body = text[text.index("{"):] if "{" in text else ""
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SourceError("Google Trends returned an unreadable response.") from exc


def interest_over_time(keyword: str, *, client: httpx.Client) -> list[float]:
    """Daily interest values for one keyword, queried alone so its own scale is kept."""
    request = json.dumps({"comparisonItem": [{"keyword": keyword, "geo": GEO, "time": TIMEFRAME}],
                          "category": 0, "property": ""})
    explore = fetch(EXPLORE_URL, headers=HEADERS, client=client, timeout=30,
                    params={"hl": "en-US", "tz": "0", "req": request})
    widgets = _payload(explore.text).get("widgets", [])
    timeseries = next((widget for widget in widgets if widget.get("id") == "TIMESERIES"), None)
    if not timeseries:
        raise SourceError("Google Trends did not return a time series for this keyword.")
    data = fetch(MULTILINE_URL, headers=HEADERS, client=client, timeout=30, params={
        "hl": "en-US", "tz": "0", "req": json.dumps(timeseries["request"]), "token": timeseries["token"],
    })
    points = _payload(data.text).get("default", {}).get("timelineData", [])
    values: list[float] = []
    for point in points:
        # The final bucket is usually a partial day and would understate today.
        if point.get("isPartial"):
            continue
        try:
            values.append(float(point["value"][0]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return values


def momentum_signal(symbol: str, keyword: str, values: list[float]) -> Signal | None:
    if len(values) < RECENT_DAYS + BASELINE_DAYS:
        return None
    recent = fmean(values[-RECENT_DAYS:])
    baseline = fmean(values[-(RECENT_DAYS + BASELINE_DAYS):-RECENT_DAYS])
    if recent < 5:
        return None  # Too little search volume for the ratio to mean anything.
    # Google's 0-100 scale makes tiny baselines wildly unstable, so floor the divisor.
    change = recent / max(baseline, 2.0) - 1
    if change > SPIKE_RATIO:
        direction, kind = -1, "trends_spike"
        magnitude = 0.4 + clamp((change - SPIKE_RATIO) / 3) * 0.6
        note = "a spike this size usually fades"
    elif change >= 0.15:
        direction, kind = 1, "trends_rising"
        magnitude = clamp(change / SPIKE_RATIO) * 0.8
        note = "steadily rising interest"
    elif change <= -0.3:
        direction, kind = -1, "trends_fading"
        magnitude = 0.3
        note = "attention draining away"
    else:
        return None
    return Signal(
        symbol=symbol, source=SOURCE, kind=kind, direction=direction, magnitude=round(magnitude, 4),
        event_at=utc_now(),
        headline=(f"Google searches for \"{keyword}\" are {change:+.0%} vs the prior "
                  f"{BASELINE_DAYS} days ({note})"),
        dedupe_key=f"trends:{symbol}:{utc_now():%Y-%m-%d}",
        url=f"https://trends.google.com/trends/explore?geo={GEO}&q={keyword.replace(' ', '%20')}",
        detail={"keyword": keyword, "recent_mean": round(recent, 2), "baseline_mean": round(baseline, 2),
                "change": round(change, 4), "geo": GEO, "timeframe": TIMEFRAME},
    )


def trending_now() -> list[Signal]:
    """Companies showing up in today's trending searches, used for discovery."""
    response = fetch(TRENDING_RSS.format(geo=GEO), headers=HEADERS, timeout=30)
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise SourceError("The Google Trends daily feed could not be parsed.") from exc
    signals: list[Signal] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        traffic = (item.findtext("{https://trends.google.com/trending/rss}approx_traffic") or "").strip()
        symbol = tickers.symbol_for_name(title)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = utc_now()
        volume = int(re.sub(r"[^\d]", "", traffic) or 0)
        signals.append(Signal(
            symbol=symbol, source=SOURCE, kind="trends_trending", direction=1,
            magnitude=round(clamp(volume / 500_000) * 0.3 + 0.1, 4), event_at=min(published, utc_now()),
            headline=f"\"{title}\" is a trending US search today{f' ({traffic})' if traffic else ''} -> {symbol}",
            dedupe_key=f"trending:{symbol}:{utc_now():%Y-%m-%d}",
            url=f"https://trends.google.com/trending?geo={GEO}",
            detail={"search_term": title, "approx_traffic": traffic},
        ))
    return signals


def collect(context: SourceContext) -> list[Signal]:
    signals: list[Signal] = []
    errors: list[str] = []
    try:
        signals.extend(trending_now())
    except SourceError as exc:
        errors.append(str(exc))
    checked = 0
    with httpx.Client(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        try:
            client.get(HOME_URL.format(geo=GEO))  # Sets the cookies the API expects.
        except httpx.HTTPError:
            pass
        for symbol in context.symbols[: context.budget]:
            keyword = tickers.search_phrase(symbol)
            checked += 1
            try:
                found = momentum_signal(symbol, keyword, interest_over_time(keyword, client=client))
            except SourceError as exc:
                errors.append(str(exc))
                # Google rate-limits aggressively; stop early rather than hammer it.
                if len(errors) >= 3:
                    break
                continue
            if found is not None:
                signals.append(found)
    if not signals and errors:
        raise SourceError(errors[0])
    return signals
