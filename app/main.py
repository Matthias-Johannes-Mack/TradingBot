from __future__ import annotations

import os
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from app.broker import AlpacaPaperBroker, BrokerError, PAPER_API_URL, decimal_text, ecb_rate, money, require_recent_trade, safe_order
from app.strategy import PaperStrategy, PaperTick, PlanPreview, StrategyInput, build_preview
from app.tax import amount, analysis, exit_estimate, TaxSettings
from app.automation import AutomationWorker, activate_plan, list_plans, pause_plan
from app import autopilot, market, research, watchlist
from app.autopilot import AutopilotSettings
from app.research import ResearchWorker
from app.sources import tickers

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
PAPER_STRATEGIES: dict[str, PaperStrategy] = {}
PENDING_BROKER_ORDERS: dict[str, "PreparedBrokerOrder"] = {}
PENDING_AUTOMATION_PLANS: dict[str, tuple[StrategyInput, datetime, str | None]] = {}
PENDING_AUTOPILOT: dict[str, tuple[AutopilotSettings, datetime]] = {}
AUTOMATION_DB = Path(os.getenv("GUARDRAIL_STATE_DB", str(ROOT / "data" / "guardrail.sqlite")))
load_dotenv(ROOT / ".env")


class BrokerOrderIntent(BaseModel):
    strategy: StrategyInput
    kind: Literal["market_buy", "hard_stop", "ladder_buy", "trailing_stop", "take_profit_limit"]
    level: int = Field(default=1, ge=1, le=10)
    target_exit_eur_per_share: Decimal | None = Field(default=None, gt=0)


class BrokerConfirmation(BaseModel):
    preview_token: str


class AutomationConfirmation(BaseModel):
    preview_token: str
    acknowledge_auto_orders: Literal[True]


class WatchlistAddition(BaseModel):
    symbol: str = Field(min_length=1, max_length=10)
    note: str | None = Field(default=None, max_length=200)


class WatchlistMute(BaseModel):
    muted: bool


class ResearchRefresh(BaseModel):
    source: str | None = None


@dataclass
class PreparedBrokerOrder:
    payload: dict
    summary: dict
    expires_at: datetime
    replaced_stop_id: str | None = None
    restore_stop: dict | None = None
    result: dict | None = None
    tax_settings: TaxSettings | None = None
    tax_basis_total_eur: Decimal | None = None


def broker_error(exc: BrokerError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def decimal_value(value: str | float | int) -> Decimal:
    return Decimal(str(value))


def account_snapshot(account: dict, rate: dict | None) -> dict:
    usd_equity = account.get("equity")
    eur_equity = None
    if usd_equity is not None and rate is not None:
        eur_equity = decimal_text(decimal_value(usd_equity) / decimal_value(rate["usd_per_eur"]))
    return {
        "status": account.get("status"), "currency": account.get("currency"),
        "trading_blocked": account.get("trading_blocked"), "equity_usd": usd_equity,
        "equity_eur_estimate": eur_equity,
    }


def checked_account_and_asset(broker: AlpacaPaperBroker, symbol: str) -> dict:
    account = broker.account()
    if account.get("currency") != "USD":
        raise BrokerError("This integration expects a USD Alpaca paper account. Check the account currency before submitting orders.", 409)
    if account.get("trading_blocked") or account.get("account_blocked") or account.get("status") != "ACTIVE":
        raise BrokerError("The Alpaca paper account is not active for trading.", 409)
    asset = broker.asset(symbol)
    if asset.get("class") != "us_equity" or not asset.get("tradable") or asset.get("status") != "active":
        raise BrokerError(f"{symbol} is not an active, tradable US equity at Alpaca.", 409)
    return account


def broker_order_preview(intent: BrokerOrderIntent, broker: AlpacaPaperBroker, rate: dict) -> PreparedBrokerOrder:
    strategy = intent.strategy
    checked_account_and_asset(broker, strategy.symbol)
    position = broker.position(strategy.symbol)
    open_orders = broker.open_orders(strategy.symbol)
    fx = decimal_value(rate["usd_per_eur"])
    client_order_id = "guardrail-" + uuid4().hex
    payload = {
        "symbol": strategy.symbol, "qty": str(strategy.shares_owned),
        "client_order_id": client_order_id,
    }
    summary = {
        "broker": "Alpaca paper", "symbol": strategy.symbol, "quantity": strategy.shares_owned,
        "fx": rate, "kind": intent.kind, "execution_currency": "USD", "display_currency": "EUR",
        "warnings": ["EUR values use the ECB daily reference rate. Alpaca executes and reports this paper order in USD."],
    }
    replaced_stop_id = None
    restore_stop = None
    tax_basis_total_eur = None
    if intent.kind == "market_buy":
        trade = broker.latest_trade(strategy.symbol)
        estimate_usd = decimal_value(trade["p"])
        payload.update({"side": "buy", "type": "market", "time_in_force": "day"})
        summary.update({
            "side": "buy", "order_type": "market", "price_usd": None,
            "reference_price_usd": decimal_text(estimate_usd),
            "reference_price_eur": decimal_text(estimate_usd / fx),
            "reference_total_usd": decimal_text(estimate_usd * strategy.shares_owned),
            "reference_total_eur": decimal_text(estimate_usd * strategy.shares_owned / fx),
            "reference_time": trade.get("t"),
        })
        summary["warnings"].append("A market order can fill at a different price or remain pending outside market hours. No stop is placed until you review and submit it separately after the buy fills.")
        estimated_basis = estimate_usd * strategy.shares_owned / fx + strategy.tax.buy_fee_eur
        trigger_floor = estimate_usd * (1 + amount(strategy.trail_trigger_pct) / 100) * (1 - amount(strategy.trail_distance_pct) / 100) / fx
        summary["tax_analysis"] = analysis(
            basis_total_eur=estimated_basis, quantity=strategy.shares_owned, settings=strategy.tax,
            scenarios={"IEX reference": estimate_usd / fx, "Protective hard stop": estimate_usd * (1 - amount(strategy.hard_floor_pct) / 100) / fx,
                       "Trailing floor at activation": trigger_floor},
            basis_source="IEX buy estimate converted at current ECB rate plus entered buy fee; replace with actual EUR acquisition cost after fill",
        )
        summary["warnings"].append("A buy cannot guarantee a profitable future sale. The tax chart uses an estimated purchase price, not a fill.")
    else:
        if not position or decimal_value(position.get("qty", "0")) != strategy.shares_owned:
            raise BrokerError(f"The share count must match the full Alpaca position in {strategy.symbol} before preparing this order. Refresh the broker position and update Shares.", 409)
        entry_usd = decimal_value(position["avg_entry_price"])
        summary["broker_entry_usd"] = decimal_text(entry_usd)
        summary["broker_entry_eur_estimate"] = decimal_text(entry_usd / fx)
        hard_stop_usd = money(entry_usd * (1 - decimal_value(strategy.hard_floor_pct) / 100))
        sell_orders = [order for order in open_orders if order.get("side") == "sell"]
        if intent.kind == "hard_stop":
            if sell_orders:
                raise BrokerError("There is already an open sell order for this symbol. Review it before placing another stop.", 409)
            payload.update({"side": "sell", "type": "stop", "time_in_force": "gtc", "stop_price": decimal_text(hard_stop_usd)})
            summary.update({"side": "sell", "order_type": "stop", "price_usd": decimal_text(hard_stop_usd), "price_eur_estimate": decimal_text(hard_stop_usd / fx), "total_usd": decimal_text(hard_stop_usd * strategy.shares_owned), "total_eur_estimate": decimal_text(hard_stop_usd * strategy.shares_owned / fx)})
        elif intent.kind == "ladder_buy":
            if intent.level > strategy.ladder_levels:
                raise BrokerError("This ladder level is outside the configured plan.", 422)
            drop_pct = decimal_value(strategy.ladder_step_pct) * intent.level
            if drop_pct >= strategy.hard_floor_pct or drop_pct >= 100:
                raise BrokerError("This buy level is at or below the hard stop, so the position would close first.", 409)
            limit_usd = money(entry_usd * (1 - drop_pct / 100))
            if any(order.get("side") == "buy" and order.get("type") == "limit" and order.get("limit_price") and money(order["limit_price"]) == limit_usd for order in open_orders):
                raise BrokerError("An open buy limit order already exists at this price for this symbol.", 409)
            payload.update({"qty": str(strategy.ladder_shares), "side": "buy", "type": "limit", "time_in_force": "gtc", "limit_price": decimal_text(limit_usd)})
            summary.update({"side": "buy", "order_type": "limit", "quantity": strategy.ladder_shares, "level": intent.level, "price_usd": decimal_text(limit_usd), "price_eur_estimate": decimal_text(limit_usd / fx), "total_usd": decimal_text(limit_usd * strategy.ladder_shares), "total_eur_estimate": decimal_text(limit_usd * strategy.ladder_shares / fx)})
            summary["warnings"].append("A filled ladder buy increases the position. Existing stop quantity is not adjusted automatically by this first broker integration.")
            estimated_basis = limit_usd * strategy.ladder_shares / fx + strategy.tax.buy_fee_eur
            summary["tax_analysis"] = analysis(
                basis_total_eur=estimated_basis, quantity=strategy.ladder_shares, settings=strategy.tax,
                scenarios={"Ladder buy reference": limit_usd / fx,
                           "Trailing floor at activation": limit_usd * (1 + amount(strategy.trail_trigger_pct) / 100) * (1 - amount(strategy.trail_distance_pct) / 100) / fx},
                basis_source="Ladder limit converted at current ECB rate plus entered buy fee; actual fill and historical EUR cost may differ",
            )
        elif intent.kind == "take_profit_limit":
            if sell_orders:
                raise BrokerError("An open sell order already exists. This app cannot safely combine a profit limit with a protective stop as OCO; review the existing order first.", 409)
            if intent.target_exit_eur_per_share is None:
                raise BrokerError("Enter a EUR target price for the profit-taking limit order.", 422)
            if strategy.tax.broker_cost_basis_eur_per_share is None:
                raise BrokerError("Enter your actual historical EUR acquisition cost per share, including allocated buy fees, before a profit-taking sell.", 422)
            limit_usd = (intent.target_exit_eur_per_share * fx).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
            tax_basis_total_eur = strategy.tax.broker_cost_basis_eur_per_share * strategy.shares_owned
            estimate = exit_estimate(basis_total_eur=tax_basis_total_eur, sale_price_eur=limit_usd / fx,
                                     quantity=strategy.shares_owned, settings=strategy.tax)
            if not estimate["meets_minimum"]:
                raise BrokerError("The proposed profit limit does not meet your after-tax EUR profit minimum. Increase the target price.", 409)
            payload.update({"side": "sell", "type": "limit", "time_in_force": "gtc", "limit_price": decimal_text(limit_usd)})
            summary.update({"side": "sell", "order_type": "limit", "price_usd": decimal_text(limit_usd),
                            "price_eur_estimate": decimal_text(limit_usd / fx), "total_usd": decimal_text(limit_usd * strategy.shares_owned),
                            "total_eur_estimate": decimal_text(limit_usd * strategy.shares_owned / fx)})
            summary["warnings"].append("Profit gate passed using current ECB FX and your entered EUR basis. A later USD fill, FX movement, fees or tax circumstances can change actual EUR net profit; this is not a guarantee.")
        else:
            trade = broker.latest_trade(strategy.symbol)
            require_recent_trade(trade)
            trade_usd = decimal_value(trade["p"])
            trigger_usd = entry_usd * (1 + decimal_value(strategy.trail_trigger_pct) / 100)
            if trade_usd < trigger_usd:
                raise BrokerError(f"The latest IEX trade is below the trailing activation level of ${decimal_text(trigger_usd)}.", 409)
            if len(sell_orders) > 1 or (sell_orders and (sell_orders[0].get("type") != "stop" or not str(sell_orders[0].get("client_order_id", "")).startswith("guardrail-"))):
                raise BrokerError("An existing sell order prevents the trailing stop. Only a Guardrail hard stop can be replaced here.", 409)
            if sell_orders:
                existing = sell_orders[0]
                if decimal_value(existing.get("qty", "0")) != strategy.shares_owned or not existing.get("stop_price"):
                    raise BrokerError("The existing Guardrail stop does not cover this full position. Review broker orders before switching to a trailing stop.", 409)
                replaced_stop_id = existing["id"]
                restore_stop = {
                    "symbol": strategy.symbol, "qty": str(strategy.shares_owned), "side": "sell",
                    "type": "stop", "time_in_force": "gtc", "stop_price": existing["stop_price"],
                    "client_order_id": "guardrail-restore-" + uuid4().hex,
                }
            payload.update({"side": "sell", "type": "trailing_stop", "time_in_force": "gtc", "trail_percent": str(strategy.trail_distance_pct)})
            summary.update({
                "side": "sell", "order_type": "trailing_stop", "price_usd": None,
                "trigger_price_usd": decimal_text(trigger_usd), "trigger_price_eur_estimate": decimal_text(trigger_usd / fx),
                "reference_price_usd": decimal_text(trade_usd), "reference_price_eur": decimal_text(trade_usd / fx),
                "reference_time": trade.get("t"), "trail_percent": strategy.trail_distance_pct,
            })
            if replaced_stop_id:
                summary["warnings"].append("Confirmation cancels the existing Guardrail stop before placing the trailing stop. A short gap in protection is possible; if submission fails, Guardrail attempts to restore the original stop.")
        if intent.kind in {"hard_stop", "trailing_stop", "take_profit_limit"}:
            if strategy.tax.broker_cost_basis_eur_per_share is not None:
                tax_basis_total_eur = strategy.tax.broker_cost_basis_eur_per_share * strategy.shares_owned
                if intent.kind == "trailing_stop":
                    scenario_price = trade_usd * (1 - amount(strategy.trail_distance_pct) / 100) / fx
                    scenario_name = "Trailing floor at IEX reference"
                else:
                    scenario_price = amount(summary["price_usd"]) / fx
                    scenario_name = "Protective hard stop" if intent.kind == "hard_stop" else "Profit-taking limit"
                summary["tax_analysis"] = analysis(
                    basis_total_eur=tax_basis_total_eur, quantity=strategy.shares_owned, settings=strategy.tax,
                    scenarios={scenario_name: scenario_price},
                    basis_source="User-entered historical EUR acquisition cost per share, including allocated buy fees",
                )
                if intent.kind != "take_profit_limit":
                    summary["warnings"].append("Protective stops are exempt from the profit gate and may realize a loss. Stop-market fills can be below the displayed stop, especially after gaps.")
            else:
                summary["warnings"].append("Tax P&L cannot be calculated for this protective stop until you enter your actual historical EUR acquisition cost per share.")
    return PreparedBrokerOrder(payload=payload, summary=summary, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                               replaced_stop_id=replaced_stop_id, restore_stop=restore_stop,
                               tax_settings=strategy.tax, tax_basis_total_eur=tax_basis_total_eur)


def llm_settings() -> tuple[str, str]:
    return (
        os.getenv("OLLAMA_MODEL", "qwen3.5:9b"),
        os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )


def explain_with_local_llm(preview: PlanPreview) -> str:
    model, base_url = llm_settings()
    chat = ChatOllama(model=model, base_url=base_url, temperature=0)
    message = HumanMessage(content=(
        "Explain this paper-trading plan in 4 short bullets for a person reviewing it. "
        "Do not recommend buying or selling. Do not alter figures. State that it is a simulation.\n\n"
        + preview.model_dump_json(indent=2)
    ))
    response = chat.invoke([
        SystemMessage(content="You are a cautious financial education assistant. Explain supplied plans only; do not give investment advice or create new orders."),
        message,
    ])
    content = response.content
    return content if isinstance(content, str) else str(content)


@asynccontextmanager
async def lifespan(_: FastAPI):
    worker = AutomationWorker(AUTOMATION_DB, after_cycle=autopilot.run_cycle)
    researcher = ResearchWorker(AUTOMATION_DB, symbols=lambda: watchlist.focus_symbols(AUTOMATION_DB))
    worker.start()
    if os.getenv("GUARDRAIL_RESEARCH", "on").strip().lower() not in {"off", "0", "false"}:
        researcher.start()
    yield
    researcher.stop()
    worker.stop()
    PAPER_STRATEGIES.clear()


app = FastAPI(title="Guardrail", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def homepage() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    model, base_url = llm_settings()
    return {"status": "ok", "llm_provider": "ollama", "model": model, "base_url": base_url}


@app.post("/api/preview", response_model=PlanPreview)
def preview_strategy(strategy: StrategyInput) -> PlanPreview:
    return build_preview(strategy)


@app.post("/api/strategies", status_code=201)
def arm_paper_strategy(strategy: StrategyInput) -> dict:
    paper = PaperStrategy.arm(build_preview(strategy))
    PAPER_STRATEGIES[paper.id] = paper
    return {"preview": paper.preview.model_dump(), **paper.response()}


@app.get("/api/strategies/{strategy_id}")
def paper_strategy_status(strategy_id: str) -> dict:
    paper = PAPER_STRATEGIES.get(strategy_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="Paper strategy was not found. Start a new session.")
    return {"preview": paper.preview.model_dump(), **paper.response()}


@app.post("/api/strategies/{strategy_id}/ticks")
def evaluate_paper_strategy(strategy_id: str, tick: PaperTick) -> dict:
    paper = PAPER_STRATEGIES.get(strategy_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="Paper strategy was not found. Start a new session.")
    events = paper.tick(tick.price)
    return {"events": [{"message": event.message, "created_at": event.created_at.isoformat()} for event in events], **paper.response()}


@app.post("/api/explain")
def explain_plan(strategy: StrategyInput) -> dict[str, str]:
    preview = build_preview(strategy)
    model, _ = llm_settings()
    try:
        explanation = explain_with_local_llm(preview)
    except Exception:
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach local Ollama model '{model}'. Start Ollama and run: ollama pull {model}",
        )
    return {"model": model, "explanation": explanation}


@app.get("/api/fx")
def latest_euro_rate() -> dict[str, str]:
    try:
        return ecb_rate()
    except BrokerError as exc:
        raise broker_error(exc) from exc


@app.get("/api/broker/status")
def alpaca_status(symbol: str = "TSLA") -> dict:
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.-]{1,10}", symbol):
        raise HTTPException(status_code=422, detail="Enter a valid stock symbol.")
    if not os.getenv("APCA_API_KEY_ID", "").strip() or not os.getenv("APCA_API_SECRET_KEY", "").strip():
        return {"configured": False, "connected": False, "paper_api_url": PAPER_API_URL, "message": "Add your Alpaca paper key and secret to .env, then restart Docker Compose."}
    try:
        try:
            rate = ecb_rate()
        except BrokerError:
            rate = None
        with AlpacaPaperBroker() as broker:
            account = broker.account()
            position = broker.position(symbol)
            orders = broker.open_orders(symbol)
        position_summary = None
        if position:
            position_summary = {key: position.get(key) for key in ("symbol", "qty", "avg_entry_price", "current_price", "market_value")}
            if rate and position.get("avg_entry_price"):
                position_summary["avg_entry_eur_estimate"] = decimal_text(decimal_value(position["avg_entry_price"]) / decimal_value(rate["usd_per_eur"]))
        return {
            "configured": True, "connected": True, "paper_api_url": PAPER_API_URL,
            "account": account_snapshot(account, rate), "position": position_summary,
            "open_orders": [safe_order(order) for order in orders], "fx": rate,
        }
    except BrokerError as exc:
        raise broker_error(exc) from exc


@app.get("/api/broker/chart")
def alpaca_chart(symbol: str = "TSLA", timeframe: Literal["1Min", "5Min", "1Day"] = "5Min") -> dict:
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.-]{1,10}", symbol):
        raise HTTPException(status_code=422, detail="Enter a valid stock symbol.")
    try:
        with AlpacaPaperBroker() as broker:
            bars = broker.candles(symbol, timeframe)
            latest = broker.latest_bar(symbol)
        if not bars:
            raise BrokerError("Alpaca has no IEX candles for this symbol and timeframe.", 503)
        if timeframe == "1Min" and latest and latest.get("t"):
            bars = [bar for bar in bars if bar.get("t") != latest["t"]] + [latest]
            bars.sort(key=lambda bar: bar.get("t", ""))
            bars = bars[-120:]
        def safe_bar(bar: dict) -> dict:
            return {key: bar.get(key) for key in ("t", "o", "h", "l", "c", "v")}
        return {
            "symbol": symbol, "currency": "USD", "feed": "IEX", "timeframe": timeframe,
            "bars": [safe_bar(bar) for bar in bars],
            "latest_minute_bar": safe_bar(latest) if latest else None,
            "polled_at": datetime.now(timezone.utc).isoformat(),
            "coverage_note": "Live-updating IEX single-exchange data, not consolidated US market data. No new candles while IEX is closed.",
        }
    except BrokerError as exc:
        raise broker_error(exc) from exc


@app.get("/api/automation/plans")
def automation_plans() -> dict:
    return {"interval_seconds": 30, "plans": list_plans(AUTOMATION_DB)}


@app.post("/api/automation/preview")
def preview_automation(strategy: StrategyInput) -> dict:
    try:
        if any(plan["active"] and plan["symbol"] == strategy.symbol for plan in list_plans(AUTOMATION_DB)):
            raise BrokerError("An automatic paper plan is already active for this symbol. Pause it first.", 409)
        with AlpacaPaperBroker() as broker:
            checked_account_and_asset(broker, strategy.symbol)
            position = broker.position(strategy.symbol)
            open_orders = broker.open_orders(strategy.symbol)
            clock = broker.clock()
        expected_qty = position.get("qty") if position else None
        if position and amount(expected_qty) != strategy.shares_owned:
            raise BrokerError("Set Shares to the current Alpaca position quantity before activating automatic stop management.", 409)
        pending_initial_buys = [order for order in open_orders if order.get("side") == "buy" and order.get("type") == "market"]
        if not position and len(pending_initial_buys) == 1 and amount(pending_initial_buys[0].get("qty", "0")) != strategy.shares_owned:
            raise BrokerError("Set Shares to the queued initial market-buy quantity before activating the automatic plan.", 409)
        warnings = [
            "This paper plan will submit and replace orders automatically every 30 seconds while Alpaca says the US market is open, even when this browser is closed.",
            "It can place a full-position protective stop, raise that stop, submit a market exit if the floor is already breached, and submit one dip re-entry limit after a plan-owned exit fills.",
            "Protective exits can lose money and are exempt from the after-tax profit gate. A stop or market exit can fill below its displayed threshold.",
            "Re-entry uses the last broker USD entry and the configured ladder step/size; it cannot guarantee a profitable future exit.",
            "An existing unrelated sell order will not be modified. Pausing this plan will not cancel orders already at Alpaca.",
        ]
        if open_orders:
            warnings.append(f"There are {len(open_orders)} existing open {strategy.symbol} order(s); the worker will wait or request review rather than overwrite them.")
        token = uuid4().hex
        expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        PENDING_AUTOMATION_PLANS[token] = (strategy, expiry, expected_qty)
        for old_token, (_, old_expiry, _) in list(PENDING_AUTOMATION_PLANS.items()):
            if old_expiry <= datetime.now(timezone.utc):
                del PENDING_AUTOMATION_PLANS[old_token]
        return {"preview_token": token, "expires_at": expiry.isoformat(), "summary": {
            "symbol": strategy.symbol, "current_position_qty": expected_qty,
            "hard_floor_pct": strategy.hard_floor_pct,
            "trail_trigger_pct": strategy.trail_trigger_pct,
            "trail_distance_pct": strategy.trail_distance_pct,
            "reentry_step_pct": strategy.ladder_step_pct,
            "reentry_shares": strategy.ladder_shares,
            "reentry_levels": strategy.ladder_levels,
            "market_open_now": bool(clock.get("is_open")), "warnings": warnings,
        }}
    except BrokerError as exc:
        raise broker_error(exc) from exc


@app.post("/api/automation/activate")
def confirm_automation(confirmation: AutomationConfirmation) -> dict:
    prepared = PENDING_AUTOMATION_PLANS.get(confirmation.preview_token)
    if not prepared or prepared[1] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="Automatic plan preview expired. Review it again.")
    strategy, _, expected_qty = prepared
    try:
        with AlpacaPaperBroker() as broker:
            checked_account_and_asset(broker, strategy.symbol)
            position = broker.position(strategy.symbol)
        current_qty = position.get("qty") if position else None
        if (current_qty is None) != (expected_qty is None) or (current_qty is not None and amount(current_qty) != amount(expected_qty)):
            raise BrokerError("Alpaca position changed since the automatic plan preview. Review it again.", 409)
        plan = activate_plan(AUTOMATION_DB, strategy)
        del PENDING_AUTOMATION_PLANS[confirmation.preview_token]
        return {"plan": plan}
    except BrokerError as exc:
        raise broker_error(exc) from exc


@app.post("/api/automation/plans/{plan_id}/pause")
def pause_automation(plan_id: str) -> dict:
    try:
        return {"plan": pause_plan(AUTOMATION_DB, plan_id)}
    except BrokerError as exc:
        raise broker_error(exc) from exc


@app.get("/api/research/status")
def research_status() -> dict:
    return {**research.status(AUTOMATION_DB), "refresh_running": research.RUN_LOCK.locked()}


def _run_refresh(source: str | None) -> None:
    try:
        focus = watchlist.focus_symbols(AUTOMATION_DB)
        research.refresh(AUTOMATION_DB, focus, only=source, force=source is None)
    finally:
        research.RUN_LOCK.release()


@app.post("/api/research/refresh", status_code=202)
def refresh_research(request: ResearchRefresh) -> dict:
    if request.source is not None and request.source not in research.SOURCES:
        raise HTTPException(status_code=422, detail="Unknown source. Choose one of: " + ", ".join(research.SOURCES) + ".")
    if not research.RUN_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A research refresh is already running.")
    threading.Thread(target=_run_refresh, args=(request.source,), name="research-refresh", daemon=True).start()
    return {"started": True, "source": request.source or "all"}


@app.get("/api/market/hours")
def market_hours() -> dict:
    return market.hours(AlpacaPaperBroker)


@app.get("/api/watchlist")
def watchlist_board() -> dict:
    return {**autopilot.board(AUTOMATION_DB), "research": research_status(), "market": market_hours()}


@app.post("/api/watchlist", status_code=201)
def add_to_watchlist(addition: WatchlistAddition) -> dict:
    try:
        return {"entry": watchlist.add(AUTOMATION_DB, addition.symbol, addition.note)}
    except watchlist.WatchlistError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/watchlist/{symbol}")
def remove_from_watchlist(symbol: str) -> dict:
    try:
        watchlist.remove(AUTOMATION_DB, symbol)
    except watchlist.WatchlistError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"removed": symbol.strip().upper()}


@app.post("/api/watchlist/{symbol}/mute")
def mute_watchlist_symbol(symbol: str, request: WatchlistMute) -> dict:
    try:
        return {"entry": watchlist.set_muted(AUTOMATION_DB, symbol, request.muted)}
    except watchlist.WatchlistError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/watchlist/{symbol}/evidence")
def watchlist_evidence(symbol: str) -> dict:
    try:
        symbol = watchlist.normalize(symbol)
    except watchlist.WatchlistError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    scored = research.score_symbols(AUTOMATION_DB, symbols=[symbol], limit=1)
    return {"symbol": symbol, "name": tickers.company_name(symbol), "score": scored[0] if scored else None,
            "evidence": research.evidence_for(AUTOMATION_DB, symbol),
            "decisions": autopilot.decisions(AUTOMATION_DB, symbol=symbol, limit=20)}


@app.get("/api/autopilot")
def autopilot_state() -> dict:
    return {"autopilot": autopilot.state(AUTOMATION_DB), "decisions": autopilot.decisions(AUTOMATION_DB, limit=40),
            "defaults": AutopilotSettings().model_dump(mode="json")}


@app.post("/api/autopilot/preview")
def preview_autopilot(settings: AutopilotSettings) -> dict:
    try:
        summary = autopilot.preview(AUTOMATION_DB, settings, broker_factory=AlpacaPaperBroker, rate_provider=ecb_rate)
    except BrokerError as exc:
        raise broker_error(exc) from exc
    token = uuid4().hex
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    for old_token, (_, old_expiry) in list(PENDING_AUTOPILOT.items()):
        if old_expiry <= datetime.now(timezone.utc):
            del PENDING_AUTOPILOT[old_token]
    PENDING_AUTOPILOT[token] = (settings, expiry)
    return {"preview_token": token, "expires_at": expiry.isoformat(), "summary": summary}


@app.post("/api/autopilot/activate")
def activate_autopilot(confirmation: AutomationConfirmation) -> dict:
    prepared = PENDING_AUTOPILOT.pop(confirmation.preview_token, None)
    if not prepared or prepared[1] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="Autopilot preview expired. Review it again.")
    return {"autopilot": autopilot.activate(AUTOMATION_DB, prepared[0])}


@app.post("/api/autopilot/pause")
def pause_autopilot() -> dict:
    return {"autopilot": autopilot.pause(AUTOMATION_DB)}


@app.post("/api/broker/order-preview")
def preview_alpaca_order(intent: BrokerOrderIntent) -> dict:
    try:
        rate = ecb_rate()
        with AlpacaPaperBroker() as broker:
            prepared = broker_order_preview(intent, broker, rate)
        token = uuid4().hex
        for old_token, old_order in list(PENDING_BROKER_ORDERS.items()):
            if old_order.expires_at <= datetime.now(timezone.utc):
                del PENDING_BROKER_ORDERS[old_token]
        PENDING_BROKER_ORDERS[token] = prepared
        return {"preview_token": token, "expires_at": prepared.expires_at.isoformat(), "summary": prepared.summary}
    except BrokerError as exc:
        raise broker_error(exc) from exc


@app.post("/api/broker/orders")
def confirm_alpaca_order(confirmation: BrokerConfirmation) -> dict:
    prepared = PENDING_BROKER_ORDERS.get(confirmation.preview_token)
    if prepared is None or prepared.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="The broker preview expired. Review the order again before submitting.")
    if prepared.result is not None:
        return prepared.result
    symbol = prepared.payload["symbol"]
    try:
        with AlpacaPaperBroker() as broker:
            checked_account_and_asset(broker, symbol)
            if prepared.payload["side"] == "sell":
                position = broker.position(symbol)
                if not position or decimal_value(position.get("qty", "0")) != decimal_value(prepared.payload["qty"]):
                    raise BrokerError("The Alpaca position size changed since the preview. Review the full-position sell order again.", 409)
            open_orders = broker.open_orders(symbol)
            sell_orders = [order for order in open_orders if order.get("side") == "sell"]
            if prepared.payload["type"] == "limit" and any(
                order.get("side") == prepared.payload["side"] and order.get("type") == "limit" and order.get("limit_price")
                and money(order["limit_price"]) == money(prepared.payload["limit_price"])
                for order in open_orders
            ):
                raise BrokerError("A matching limit order appeared since the preview. Review broker orders before retrying.", 409)
            if prepared.payload["type"] == "limit" and prepared.payload["side"] == "sell":
                if sell_orders:
                    raise BrokerError("An open sell order appeared since the profit-limit preview. Review broker orders before retrying.", 409)
                if prepared.tax_settings is None or prepared.tax_basis_total_eur is None:
                    raise BrokerError("Missing tax inputs for the profit-taking order. Preview it again.", 409)
                current_fx = amount(ecb_rate()["usd_per_eur"])
                current_estimate = exit_estimate(
                    basis_total_eur=prepared.tax_basis_total_eur,
                    sale_price_eur=amount(prepared.payload["limit_price"]) / current_fx,
                    quantity=int(prepared.payload["qty"]), settings=prepared.tax_settings,
                )
                if not current_estimate["meets_minimum"]:
                    raise BrokerError("The profit limit no longer meets your after-tax EUR minimum at the current FX reference. Preview a higher target before submitting.", 409)
            if prepared.payload["type"] == "stop" and sell_orders:
                raise BrokerError("An open sell order appeared since the preview. Review broker orders before retrying.", 409)
            if prepared.payload["type"] == "trailing_stop":
                if prepared.replaced_stop_id:
                    if len(sell_orders) != 1 or sell_orders[0].get("id") != prepared.replaced_stop_id:
                        raise BrokerError("The existing stop changed since the preview. Review the order again.", 409)
                    trade = broker.latest_trade(symbol)
                    require_recent_trade(trade)
                    if decimal_value(trade["p"]) < decimal_value(prepared.summary["trigger_price_usd"]):
                        raise BrokerError("The latest IEX trade is now below the trailing trigger. The hard stop remains in place.", 409)
                    broker.cancel(prepared.replaced_stop_id)
                    for _ in range(10):
                        prior = broker.order(prepared.replaced_stop_id)
                        if prior.get("status") == "canceled":
                            break
                        if prior.get("status") in {"filled", "partially_filled"}:
                            raise BrokerError("The hard stop filled while switching. No trailing order was placed.", 409)
                        time.sleep(0.5)
                    else:
                        raise BrokerError("Alpaca has not confirmed cancellation of the hard stop. No trailing order was placed; check broker status.", 409)
                elif sell_orders:
                    raise BrokerError("An open sell order appeared since the preview. Review broker orders before retrying.", 409)
            try:
                order = broker.submit(prepared.payload)
            except BrokerError as submit_error:
                existing = broker.order_by_client_id(prepared.payload["client_order_id"])
                if existing is not None:
                    order = existing
                elif prepared.restore_stop:
                    try:
                        restored = broker.submit(prepared.restore_stop)
                    except BrokerError as restore_error:
                        raise BrokerError("Trailing order failed after the original stop was canceled, and restoration failed. Check the Alpaca account immediately.", 502) from restore_error
                    raise BrokerError(f"Trailing order failed. Original hard stop was restored as order {restored.get('id', 'unknown')}.", 502) from submit_error
                else:
                    raise
        prepared.result = {"summary": prepared.summary, "order": safe_order(order), "replaced_stop_order_id": prepared.replaced_stop_id}
        return prepared.result
    except BrokerError as exc:
        raise broker_error(exc) from exc
