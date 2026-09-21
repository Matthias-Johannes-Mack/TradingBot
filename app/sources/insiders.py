"""Corporate insider transactions from SEC EDGAR Form 4 filings.

EDGAR publishes an Atom feed of filings accepted in the last few hours and a
daily form index for every business day. Together they give complete coverage:
the feed for freshness, the index to backfill anything a cycle missed. Each
complete-submission text file carries the ownership XML, including the issuer's
own ticker, so no CIK lookup is needed. Only open-market purchases (code P) and
open-market sales (code S) are kept; option exercises, grants and gifts say far
less about what an insider expects the price to do.
"""

from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from app.sources.base import (
    SEC_USER_AGENT, Signal, SourceContext, SourceError, band_magnitude, fetch, parse_iso, utc_now,
)
from app.sources import tickers

SOURCE = "insiders"
FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/form.{day:%Y%m%d}.idx"
BACKFILL_BUSINESS_DAYS = 5
SUBMISSION_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{plain}/{accession}.txt"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{plain}/{accession}-index.htm"
HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
ATOM = {"a": "http://www.w3.org/2005/Atom"}
_ACCESSION = re.compile(r"/Archives/edgar/data/(\d+)/(\d{18})/(\d{10}-\d{2}-\d{6})")
_INDEX_ROW = re.compile(r"^4\s+.*?\s(\d+)\s+(\d{8})\s+edgar/data/\d+/(\d{10}-\d{2}-\d{6})\.txt\s*$")
# Open-market buy and sell. Everything else is compensation plumbing.
_CODES = {"P": 1, "S": -1}


def _recent_filings(count: int) -> list[tuple[str, str]]:
    response = fetch(FEED_URL, headers=HEADERS, params={
        "action": "getcurrent", "type": "4", "company": "", "dateb": "", "owner": "include",
        "count": str(count), "output": "atom",
    }, timeout=30)
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise SourceError("The EDGAR Form 4 feed could not be parsed.") from exc
    filings: list[tuple[str, str]] = []
    for entry in root.findall("a:entry", ATOM):
        link = entry.find("a:link", ATOM)
        match = _ACCESSION.search(link.get("href", "") if link is not None else "")
        if match and (match.group(1), match.group(3)) not in filings:
            filings.append((match.group(1), match.group(3)))
    return filings


_index_lock = threading.Lock()
_index_cache: dict[date, list[tuple[str, str]]] = {}


def _business_days(count: int) -> list[date]:
    days: list[date] = []
    cursor = utc_now().date()
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def _daily_filings(day: date) -> list[tuple[str, str]]:
    """Form 4 accessions filed on one day. Past days never change, so they are cached."""
    today = utc_now().date()
    with _index_lock:
        if day < today and day in _index_cache:
            return _index_cache[day]
    url = DAILY_INDEX_URL.format(year=day.year, quarter=(day.month - 1) // 3 + 1, day=day)
    text = fetch(url, headers=HEADERS, timeout=45).text
    filings: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = _INDEX_ROW.match(line)
        # Issuer and reporting owner both list the same accession; keep one.
        if match and match.group(3) not in seen:
            seen.add(match.group(3))
            filings.append((match.group(1), match.group(3)))
    filings.reverse()  # Later accession numbers were accepted later in the day.
    with _index_lock:
        if day < today:
            _index_cache[day] = filings
            for stale in [cached for cached in _index_cache if cached < today - timedelta(days=14)]:
                del _index_cache[stale]
    return filings


def _text(node: ET.Element | None) -> str:
    """Form 4 wraps most fields in an optional <value> child."""
    if node is None:
        return ""
    value = node.find("value")
    return ((value.text if value is not None else node.text) or "").strip()


def _role(owner: ET.Element) -> tuple[str, float]:
    """Readable role plus a weight: officers and directors know the business;
    a fund that merely crossed 10% ownership mostly knows its own portfolio."""
    relationship = owner.find("reportingOwnerRelationship")
    if relationship is None:
        return "insider", 0.6
    title = _text(relationship.find("officerTitle"))
    roles = []
    if _text(relationship.find("isDirector")) in {"1", "true"}:
        roles.append("director")
    if _text(relationship.find("isOfficer")) in {"1", "true"}:
        roles.append(title or "officer")
    operating = bool(roles)
    if _text(relationship.find("isTenPercentOwner")) in {"1", "true"}:
        roles.append("10% owner")
    return ", ".join(roles) or "insider", 1.0 if operating else 0.6


def signals_from(cik: str, accession: str, body: str) -> list[Signal]:
    match = re.search(r"<ownershipDocument>.*?</ownershipDocument>", body, re.DOTALL)
    if not match:
        return []
    try:
        document = ET.fromstring(match.group(0))
    except ET.ParseError:
        return []
    symbol = _text(document.find("issuer/issuerTradingSymbol")).upper()
    if not tickers.known(symbol):
        return []
    owner = document.find("reportingOwner")
    name = _text(owner.find("reportingOwnerId/rptOwnerName")) if owner is not None else ""
    role, role_weight = _role(owner) if owner is not None else ("insider", 0.6)
    plain = accession.replace("-", "")
    url = FILING_URL.format(cik=cik, plain=plain, accession=accession)
    signals: list[Signal] = []
    for index, transaction in enumerate(document.findall("nonDerivativeTable/nonDerivativeTransaction")):
        coding = transaction.find("transactionCoding")
        code = _text(coding.find("transactionCode")) if coding is not None else ""
        direction = _CODES.get(code, 0)
        traded_at = parse_iso(_text(transaction.find("transactionDate")))
        amounts = transaction.find("transactionAmounts")
        if direction == 0 or traded_at is None or amounts is None:
            continue
        try:
            shares = float(_text(amounts.find("transactionShares")) or 0)
            price = float(_text(amounts.find("transactionPricePerShare")) or 0)
        except ValueError:
            continue
        value = shares * price
        if value <= 0:
            continue
        # Insider buying is the well-documented signal; insider selling has many
        # innocent reasons, so it is scaled down before it ever reaches the score.
        magnitude = band_magnitude(value, 10_000, 5_000_000) * (1.0 if direction > 0 else 0.7) * role_weight
        signals.append(Signal(
            symbol=symbol, source=SOURCE, kind=f"insider_{'buy' if direction > 0 else 'sell'}",
            direction=direction, magnitude=round(magnitude, 4), event_at=traded_at,
            headline=(f"{name or 'An insider'} ({role}) {'bought' if direction > 0 else 'sold'} "
                      f"{shares:,.0f} {symbol} at ${price:,.2f} (${value:,.0f}) on {traded_at:%d %b %Y}"),
            dedupe_key=f"{accession}:{index}",
            url=url,
            detail={"owner": name, "role": role, "shares": shares, "price_usd": price,
                    "value_usd": round(value, 2), "transaction_code": code},
        ))
    return signals


def collect(context: SourceContext) -> list[Signal]:
    filings: list[tuple[str, str]] = []
    try:
        filings.extend(_recent_filings(100))
    except SourceError:
        pass
    for day in _business_days(BACKFILL_BUSINESS_DAYS):
        try:
            filings.extend(_daily_filings(day))
        except SourceError:
            continue  # Today's index may not exist yet, and holidays have none.
    unique: dict[str, tuple[str, str]] = {}
    for cik, accession in filings:
        unique.setdefault(accession, (cik, accession))
    filings = list(unique.values())
    if not filings:
        raise SourceError("Neither the EDGAR Form 4 feed nor the daily index returned filings.")
    signals: list[Signal] = []
    errors = 0
    fetched = 0
    for cik, accession in filings:
        if fetched >= context.budget:
            break
        if accession in context.processed:
            continue
        fetched += 1
        plain = accession.replace("-", "")
        try:
            body = fetch(SUBMISSION_URL.format(cik=cik, plain=plain, accession=accession),
                         headers=HEADERS, timeout=30).text
        except SourceError:
            errors += 1
            continue
        signals.extend(signals_from(cik, accession, body))
        context.remember(accession)
    if fetched and errors >= fetched:
        raise SourceError("No EDGAR Form 4 submission could be downloaded this cycle.")
    return signals

