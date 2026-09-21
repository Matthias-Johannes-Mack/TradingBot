"""Newly signed US federal contract awards from USAspending.gov.

The free API needs no key. Recipient names are matched back to a listed ticker
through the SEC company index, so only awards to public parents are kept, and
only when the name maps to exactly one symbol.
"""

from __future__ import annotations

from datetime import timedelta

from app.sources.base import (
    SEC_USER_AGENT, Signal, SourceContext, SourceError, band_magnitude, fetch_json, parse_iso, utc_now,
)
from app.sources import tickers

SOURCE = "contracts"
SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_URL = "https://www.usaspending.gov/award/{generated_id}"
HEADERS = {"User-Agent": SEC_USER_AGENT, "Content-Type": "application/json"}
LOOKBACK_DAYS = 21
PAGE_SIZE = 100
MIN_AWARD_USD = 1_000_000


def collect(context: SourceContext) -> list[Signal]:
    end = utc_now().date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat(),
                             "date_type": "new_awards_only"}],
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Start Date",
                   "Last Modified Date", "Description"],
        "page": 1, "limit": PAGE_SIZE, "sort": "Award Amount", "order": "desc", "subawards": False,
    }
    response = fetch_json(SEARCH_URL, method="POST", headers=HEADERS, json_body=payload, timeout=60)
    results = response.get("results", []) if isinstance(response, dict) else []
    if not isinstance(results, list):
        raise SourceError("USAspending returned an unexpected award list.")
    signals: list[Signal] = []
    for award in results:
        try:
            value = float(award.get("Award Amount") or 0)
        except (TypeError, ValueError):
            continue
        recipient = str(award.get("Recipient Name") or "")
        symbol = tickers.symbol_for_name(recipient)
        award_id = str(award.get("Award ID") or "")
        if value < MIN_AWARD_USD or not symbol or not award_id:
            continue
        signed_at = parse_iso(award.get("Start Date")) or parse_iso(award.get("Last Modified Date")) or utc_now()
        agency = str(award.get("Awarding Agency") or "a federal agency")
        signals.append(Signal(
            symbol=symbol, source=SOURCE, kind="government_contract", direction=1,
            magnitude=round(band_magnitude(value, MIN_AWARD_USD, 1_000_000_000), 4),
            event_at=min(signed_at, utc_now()),
            headline=(f"{recipient.title()} won a ${value:,.0f} {agency} contract "
                      f"starting {signed_at:%d %b %Y} ({symbol})"),
            dedupe_key=f"{award_id}:{symbol}",
            url=AWARD_URL.format(generated_id=award.get("generated_internal_id", "")),
            detail={"recipient": recipient, "award_id": award_id, "amount_usd": value, "agency": agency,
                    "description": str(award.get("Description") or "")[:240],
                    "materiality": "Award size is not scaled to company size; a large award can still be immaterial."},
        ))
    return signals
