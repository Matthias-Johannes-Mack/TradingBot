from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tax import amount, analysis, TaxSettings


class StrategyInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    symbol: str = Field(min_length=1, max_length=10, examples=["TSLA"])
    shares_owned: int = Field(gt=0, examples=[10])
    entry_price: float = Field(gt=0, examples=[250])
    current_price: float = Field(gt=0, examples=[250])
    hard_floor_pct: float = Field(default=10, gt=0, lt=100)
    trail_trigger_pct: float = Field(default=10, gt=0, le=500)
    trail_distance_pct: float = Field(default=5, gt=0, lt=100)
    ladder_step_pct: float = Field(default=20, gt=0, lt=100)
    ladder_shares: int = Field(default=20, gt=0)
    ladder_levels: int = Field(default=3, ge=1, le=10)
    tax: TaxSettings = Field(default_factory=TaxSettings)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.replace(".", "").replace("-", "").isalnum():
            raise ValueError("Symbol may only contain letters, numbers, dots, and hyphens.")
        return normalized


class PaperTick(BaseModel):
    price: float = Field(gt=0)


class OrderPreview(BaseModel):
    id: str
    side: Literal["BUY", "SELL"]
    order_type: str
    condition: str
    price: float | None = None
    quantity: int
    status: Literal["PENDING", "CONDITIONAL", "BLOCKED"]


class PlanPreview(BaseModel):
    currency: Literal["EUR"] = "EUR"
    strategy: StrategyInput
    hard_stop_price: float
    trailing_trigger_price: float
    trailing_stop_at_trigger: float
    orders: list[OrderPreview]
    tax_analysis: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str = "Paper-only simulation. This is not investment advice."


def rounded(number: float) -> float:
    return round(number + 1e-9, 2)


def build_preview(strategy: StrategyInput) -> PlanPreview:
    hard_stop = rounded(strategy.entry_price * (1 - strategy.hard_floor_pct / 100))
    trigger = rounded(strategy.entry_price * (1 + strategy.trail_trigger_pct / 100))
    trail_at_trigger = rounded(trigger * (1 - strategy.trail_distance_pct / 100))
    warnings: list[str] = []
    orders = [
        OrderPreview(
            id="hard-stop", side="SELL", order_type="STOP", quantity=strategy.shares_owned,
            price=hard_stop, status="PENDING",
            condition=f"Sell the open position if {strategy.symbol} is at or below €{hard_stop:,.2f}.",
        ),
        OrderPreview(
            id="trailing-stop", side="SELL", order_type="TRAILING_STOP", quantity=strategy.shares_owned,
            price=None, status="CONDITIONAL",
            condition=(f"Activate at €{trigger:,.2f}; then keep a stop {strategy.trail_distance_pct:g}% below the highest observed price. "
                       f"At activation, the stop would be €{trail_at_trigger:,.2f}."),
        ),
    ]
    for level in range(1, strategy.ladder_levels + 1):
        price = rounded(strategy.entry_price * (1 - strategy.ladder_step_pct * level / 100))
        if price <= 0:
            break
        blocked = price <= hard_stop
        if blocked:
            warnings.append(
                f"Ladder level {level} at €{price:,.2f} is below the hard stop at €{hard_stop:,.2f}, so the paper position would close before this buy can occur."
            )
        orders.append(OrderPreview(
            id=f"ladder-{level}", side="BUY", order_type="LIMIT", quantity=strategy.ladder_shares,
            price=price, status="BLOCKED" if blocked else "PENDING",
            condition=("Blocked by the hard floor: the paper position closes before this level." if blocked else
                       f"Buy {strategy.ladder_shares} shares if {strategy.symbol} is at or below €{price:,.2f} ({strategy.ladder_step_pct * level:g}% below entry)."),
        ))
    return PlanPreview(
        strategy=strategy, hard_stop_price=hard_stop, trailing_trigger_price=trigger,
        trailing_stop_at_trigger=trail_at_trigger, orders=orders, warnings=warnings,
        tax_analysis=analysis(
            basis_total_eur=amount(strategy.entry_price) * strategy.shares_owned + strategy.tax.buy_fee_eur,
            quantity=strategy.shares_owned, settings=strategy.tax,
            scenarios={"Current price": amount(strategy.current_price),
                       "Hard stop (protective)": amount(hard_stop),
                       "Trailing floor at activation": amount(trail_at_trigger)},
            basis_source="Entered EUR entry price plus estimated buy fee (simulation)",
        ),
    )


@dataclass
class Activity:
    message: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PaperStrategy:
    id: str
    preview: PlanPreview
    high_water: float
    armed: bool = True
    closed: bool = False
    filled_ladder_ids: set[str] = field(default_factory=set)
    activities: list[Activity] = field(default_factory=list)

    @classmethod
    def arm(cls, preview: PlanPreview) -> "PaperStrategy":
        paper = cls(id=str(uuid4()), preview=preview, high_water=preview.strategy.current_price)
        paper.activities.append(Activity("Paper strategy armed. No orders were sent to a broker."))
        return paper

    def tick(self, price: float) -> list[Activity]:
        if not self.armed or self.closed:
            return []
        self.high_water = max(self.high_water, price)
        generated: list[Activity] = []
        strategy = self.preview.strategy
        if price <= self.preview.hard_stop_price:
            self.closed = True
            generated.append(Activity(f"SIMULATED SELL: {strategy.shares_owned} {strategy.symbol} at €{price:,.2f}; hard stop was €{self.preview.hard_stop_price:,.2f}."))
        elif self.high_water >= self.preview.trailing_trigger_price:
            dynamic_stop = rounded(self.high_water * (1 - strategy.trail_distance_pct / 100))
            if price <= dynamic_stop:
                self.closed = True
                generated.append(Activity(f"SIMULATED SELL: {strategy.shares_owned} {strategy.symbol} at €{price:,.2f}; trailing stop was €{dynamic_stop:,.2f}."))
        if not self.closed:
            for order in self.preview.orders:
                if order.id.startswith("ladder-") and order.id not in self.filled_ladder_ids and price <= (order.price or 0):
                    self.filled_ladder_ids.add(order.id)
                    generated.append(Activity(f"SIMULATED BUY: {order.quantity} {strategy.symbol} at €{price:,.2f}; ladder limit was €{order.price:,.2f}."))
        self.activities.extend(generated)
        return generated

    def response(self) -> dict:
        return {
            "strategy_id": self.id,
            "armed": self.armed,
            "closed": self.closed,
            "high_water_price": rounded(self.high_water),
            "activities": [{"message": item.message, "created_at": item.created_at.isoformat()} for item in reversed(self.activities)],
        }
