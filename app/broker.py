"""Small, paper-only Alpaca Trading API adapter.

Every URL is fixed to Alpaca's paper endpoint. Credentials are read from the
container environment and never returned to the browser.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from typing import Any

import httpx


PAPER_API_URL = "https://paper-api.alpaca.markets/v2"
DATA_API_URL = "https://data.alpaca.markets/v2"
ECB_RATE_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?lastNObservations=1&format=csvdata"


class BrokerError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def money(value: Decimal | str | float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal | str | float) -> str:
    return format(money(value), ".2f")


@lru_cache(maxsize=2)
def _ecb_rate_for_day(_utc_day: str) -> dict[str, str]:
    try:
        response = httpx.get(ECB_RATE_URL, timeout=10)
        response.raise_for_status()
        row = next(csv.DictReader(io.StringIO(response.text)))
        rate = Decimal(row["OBS_VALUE"])
        if rate <= 0 or row["CURRENCY"] != "USD" or row["CURRENCY_DENOM"] != "EUR":
            raise ValueError("Unexpected ECB currency pair")
        return {"usd_per_eur": str(rate), "reference_date": row["TIME_PERIOD"], "source": "ECB daily reference rate"}
    except (httpx.HTTPError, StopIteration, KeyError, ValueError) as exc:
        raise BrokerError("The ECB EUR/USD reference rate is unavailable. Try again later.", 503) from exc


def ecb_rate() -> dict[str, str]:
    return _ecb_rate_for_day(datetime.now(timezone.utc).date().isoformat())


def require_recent_trade(trade: dict, *, max_age: timedelta = timedelta(minutes=5)) -> None:
    try:
        observed_at = datetime.fromisoformat(str(trade["t"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise BrokerError("Alpaca did not provide a valid IEX trade timestamp.", 503) from exc
    if observed_at.tzinfo is None:
        raise BrokerError("Alpaca did not provide a timezone for its IEX trade timestamp.", 503)
    age = datetime.now(timezone.utc) - observed_at
    if age > max_age or age < -timedelta(minutes=1):
        raise BrokerError("The latest IEX trade is too old to activate a trailing stop. Try again while the market is trading.", 409)


class AlpacaPaperBroker:
    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        configured_url = os.getenv("ALPACA_API_URL", PAPER_API_URL).rstrip("/")
        if configured_url != PAPER_API_URL:
            raise BrokerError("Only the Alpaca paper trading endpoint is supported.", 503)
        key = os.getenv("APCA_API_KEY_ID", "").strip()
        secret = os.getenv("APCA_API_SECRET_KEY", "").strip()
        if not key or not secret:
            raise BrokerError("Add APCA_API_KEY_ID and APCA_API_SECRET_KEY to the Docker .env file, then restart the container.", 503)
        self.client = httpx.Client(
            timeout=10,
            transport=transport,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"},
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "AlpacaPaperBroker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(self, method: str, path: str, *, data_api: bool = False, **kwargs: Any) -> Any:
        base = DATA_API_URL if data_api else PAPER_API_URL
        try:
            response = self.client.request(method, base + path, **kwargs)
        except httpx.RequestError as exc:
            raise BrokerError("Alpaca could not be reached. The order status is unknown; refresh broker orders before retrying.", 503) from exc
        if response.status_code == 404:
            raise BrokerError("The requested Alpaca resource was not found.", 404)
        if response.is_error:
            try:
                detail = response.json().get("message", "Alpaca rejected the request.")
            except (ValueError, AttributeError):
                detail = "Alpaca rejected the request."
            raise BrokerError(f"Alpaca: {str(detail)[:240]}", 502)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def account(self) -> dict:
        return self.request("GET", "/account")

    def clock(self) -> dict:
        return self.request("GET", "/clock")

    def calendar(self, start: date, end: date) -> list[dict]:
        """Trading days with New York open/close times; holidays are absent."""
        result = self.request("GET", "/calendar", params={"start": start.isoformat(), "end": end.isoformat()})
        return result if isinstance(result, list) else []

    def asset(self, symbol: str) -> dict:
        return self.request("GET", f"/assets/{symbol}")

    def position(self, symbol: str) -> dict | None:
        try:
            return self.request("GET", f"/positions/{symbol}")
        except BrokerError as exc:
            if exc.status_code == 404:
                return None
            raise

    def open_orders(self, symbol: str) -> list[dict]:
        return self.request("GET", "/orders", params={"status": "open", "symbols": symbol, "limit": 500})

    def latest_trade(self, symbol: str) -> dict:
        result = self.request("GET", f"/stocks/{symbol}/trades/latest", data_api=True, params={"feed": "iex"})
        trade = result.get("trade") if isinstance(result, dict) else None
        if not trade or not trade.get("p"):
            raise BrokerError("Alpaca did not return an IEX reference trade for this symbol.", 503)
        return trade

    def candles(self, symbol: str, timeframe: str) -> list[dict]:
        if timeframe not in {"1Min", "5Min", "1Day"}:
            raise BrokerError("Unsupported chart timeframe.", 422)
        now = datetime.now(timezone.utc)
        lookback = timedelta(days=8 if timeframe != "1Day" else 180)
        result = self.request("GET", f"/stocks/{symbol}/bars", data_api=True, params={
            "feed": "iex", "timeframe": timeframe, "start": (now - lookback).isoformat(),
            "end": now.isoformat(), "sort": "desc", "limit": 120, "adjustment": "raw",
        })
        bars = result.get("bars", []) if isinstance(result, dict) else []
        if not isinstance(bars, list):
            raise BrokerError("Alpaca returned invalid chart data.", 503)
        return list(reversed(bars))

    def latest_bar(self, symbol: str) -> dict | None:
        result = self.request("GET", "/stocks/bars/latest", data_api=True, params={"symbols": symbol, "feed": "iex"})
        bars = result.get("bars", {}) if isinstance(result, dict) else {}
        return bars.get(symbol) if isinstance(bars, dict) else None

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        try:
            return self.request("GET", "/orders:by_client_order_id", params={"client_order_id": client_order_id})
        except BrokerError as exc:
            if exc.status_code == 404:
                return None
            raise

    def order(self, order_id: str) -> dict:
        return self.request("GET", f"/orders/{order_id}")

    def submit(self, payload: dict) -> dict:
        existing = self.order_by_client_id(payload["client_order_id"])
        if existing is not None:
            return existing
        return self.request("POST", "/orders", json=payload)

    def cancel(self, order_id: str) -> None:
        self.request("DELETE", f"/orders/{order_id}")

    def replace_stop(self, order_id: str, stop_price: Decimal) -> dict:
        return self.request("PATCH", f"/orders/{order_id}", json={"stop_price": decimal_text(stop_price)})


def safe_order(order: dict) -> dict:
    allowed = (
        "id", "client_order_id", "symbol", "side", "type", "qty", "status", "time_in_force",
        "limit_price", "stop_price", "trail_percent", "hwm", "filled_qty", "filled_avg_price", "submitted_at",
    )
    return {key: order.get(key) for key in allowed}
