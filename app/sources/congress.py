"""US House periodic transaction reports, straight from the Clerk of the House.

This is the free primary source behind every paid "congress trading" feed: a
yearly ZIP index of filings plus one PDF per report. The Senate's own search
site blocks automated clients, so only House filings are covered here.
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import timedelta

from pypdf import PdfReader

from app.sources.base import (
    SEC_USER_AGENT, Signal, SourceContext, SourceError, band_magnitude, clamp, fetch, parse_date, utc_now,
)
from app.sources import tickers

SOURCE = "congress"
INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
REPORT_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
HEADERS = {"User-Agent": SEC_USER_AGENT}
LOOKBACK_DAYS = 60

# One transaction row: ticker and asset code, then optional owner code, action,
# trade date, notification date and the disclosed USD range.
_ROW = re.compile(
    r"\(([A-Z][A-Z0-9.\-]{0,9})\)\s*\[(ST|OP|CS|OT)\]"
    r"[^\n]*\n?\s*(?:(?:SP|DC|JT)\s+)?"
    r"(P|S \(partial\)|S|E)\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+"
    r"\$?([\d,]+)\s*-\s*\$?\s*([\d,]+)"
)
_ACTIONS = {"P": 1, "S": -1, "S (partial)": -1, "E": 0}


def _index(year: int) -> list[dict]:
    response = fetch(INDEX_URL.format(year=year), headers=HEADERS, timeout=45)
    try:
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        name = next(item for item in archive.namelist() if item.lower().endswith(".xml"))
        root = ET.fromstring(archive.read(name))
    except (zipfile.BadZipFile, StopIteration, ET.ParseError, KeyError) as exc:
        raise SourceError("The House disclosure index could not be read.") from exc
    filings = []
    for member in root:
        row = {child.tag: (child.text or "").strip() for child in member}
        if row.get("FilingType") != "P" or not row.get("DocID", "").isdigit():
            continue
        filed_at = parse_date(row.get("FilingDate", ""), "%m/%d/%Y")
        if filed_at is None:
            continue
        # The Clerk repeats a middle name across the First and Last fields for
        # some members, so collapse words that would otherwise appear twice.
        words: list[str] = []
        for part in (row.get("First"), row.get("Last"), row.get("Suffix")):
            for word in (part or "").split():
                if not words or word.lower() != words[-1].lower():
                    words.append(word)
        filings.append({
            "doc": row["DocID"], "year": year, "filed_at": filed_at,
            "member": " ".join(words), "district": row.get("StateDst", ""),
        })
    return filings


def _report_text(filing: dict) -> str:
    response = fetch(REPORT_URL.format(year=filing["year"], doc=filing["doc"]), headers=HEADERS, timeout=45)
    if not response.content.startswith(b"%PDF"):
        raise SourceError("The House Clerk returned a report that is not a PDF.")
    try:
        pages = PdfReader(io.BytesIO(response.content)).pages
        # The Clerk's small-caps headings decode to NULs, which only add noise.
        return "\n".join((page.extract_text() or "") for page in pages).replace("\x00", "")
    except Exception as exc:  # pypdf raises a wide range of parse errors
        raise SourceError(f"A House report PDF could not be parsed: {type(exc).__name__}.") from exc


def signals_from(filing: dict, text: str) -> list[Signal]:
    """Normalized signals for one report. `event_at` is the disclosure date,
    because that is when the information could first be acted on."""
    signals: list[Signal] = []
    for symbol, asset_code, action, traded, _notified, low, high in _ROW.findall(text):
        direction = _ACTIONS.get(action, 0)
        traded_at = parse_date(traded, "%m/%d/%Y")
        if direction == 0 or traded_at is None or not tickers.known(symbol):
            continue
        try:
            midpoint = (int(low.replace(",", "")) + int(high.replace(",", ""))) / 2
        except ValueError:
            continue
        # A report may be filed up to 45 days after the trade; stale news is worth less.
        lag_days = max((filing["filed_at"] - traded_at).days, 0)
        magnitude = band_magnitude(midpoint, 1_000, 5_000_000) * clamp(1 - lag_days / 60, 0.25, 1.0)
        if asset_code == "OP":
            magnitude *= 0.6
        side = "bought" if direction > 0 else "sold"
        signals.append(Signal(
            symbol=symbol, source=SOURCE, kind=f"congress_{'buy' if direction > 0 else 'sell'}",
            direction=direction, magnitude=round(magnitude, 4), event_at=filing["filed_at"],
            headline=(f"Rep. {filing['member']} ({filing['district']}) {side} about ${midpoint:,.0f} "
                      f"of {symbol} on {traded_at:%d %b %Y}, disclosed {filing['filed_at']:%d %b %Y}"),
            dedupe_key=f"{filing['doc']}:{symbol}:{action}:{traded}:{low}",
            url=REPORT_URL.format(year=filing["year"], doc=filing["doc"]),
            detail={"member": filing["member"], "district": filing["district"], "asset_code": asset_code,
                    "amount_low_usd": low, "amount_high_usd": high, "traded_at": traded_at.isoformat(),
                    "disclosure_lag_days": lag_days},
        ))
    return signals


def collect(context: SourceContext) -> list[Signal]:
    cutoff = utc_now() - timedelta(days=LOOKBACK_DAYS)
    today = utc_now()
    years = {today.year} | ({today.year - 1} if today.month <= 2 else set())
    filings: list[dict] = []
    errors: list[str] = []
    for year in sorted(years, reverse=True):
        try:
            filings.extend(item for item in _index(year) if item["filed_at"] >= cutoff)
        except SourceError as exc:
            errors.append(str(exc))
    if not filings and errors:
        raise SourceError(errors[0])
    filings.sort(key=lambda item: item["filed_at"], reverse=True)
    signals: list[Signal] = []
    fetched = 0
    for filing in filings:
        if fetched >= context.budget:
            break
        if filing["doc"] in context.processed:
            continue
        fetched += 1
        try:
            text = _report_text(filing)
        except SourceError as exc:
            errors.append(str(exc))
            continue
        signals.extend(signals_from(filing, text))
        context.remember(filing["doc"])
    if fetched and len(errors) >= fetched:
        raise SourceError(errors[0])
    return signals
