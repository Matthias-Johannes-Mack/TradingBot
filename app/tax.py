"""Conservative German private-share-gain estimates in EUR.

This is a planning aid, not a tax ledger. In particular, it does not track
FIFO lots, other capital income, loss pots, or the user's tax return.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CENT = Decimal("0.01")
SOLIDARITY_RATE = Decimal("0.055")


def amount(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value))


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def euro_text(value: Decimal) -> str:
    return str(cents(value))


class TaxSettings(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    # Default to the user's no-church-tax case (26.375% incl. solidarity).
    # 8% is the usual Baden-Wuerttemberg church rate when applicable;
    # 9% covers the Bad Wimpfen Catholic exception.
    church_tax_rate_pct: Literal[0, 8, 9] = 0
    remaining_allowance_eur: Decimal = Field(default=Decimal("0"), ge=0, le=2000)
    buy_fee_eur: Decimal = Field(default=Decimal("0"), ge=0)
    sell_fee_eur: Decimal = Field(default=Decimal("0"), ge=0)
    minimum_net_profit_eur: Decimal = Field(default=Decimal("0.01"), ge=Decimal("0.01"))
    # Required to evaluate existing Alpaca positions: an actual historical EUR
    # acquisition cost including allocated purchase expenses, not current FX.
    broker_cost_basis_eur_per_share: Decimal | None = Field(default=None, gt=0)


def exit_estimate(
    *, basis_total_eur: Decimal, sale_price_eur: Decimal, quantity: int, settings: TaxSettings,
) -> dict:
    proceeds = sale_price_eur * quantity
    pre_tax = proceeds - basis_total_eur - settings.sell_fee_eur
    taxable = max(Decimal("0"), pre_tax - settings.remaining_allowance_eur)
    church_rate = amount(settings.church_tax_rate_pct) / 100
    # EStG 32d(1): church tax reduces the capital-gains tax base via 4 + k.
    capital_tax = taxable / (Decimal("4") + church_rate)
    solidarity = capital_tax * SOLIDARITY_RATE
    church_tax = capital_tax * church_rate
    total_tax = capital_tax + solidarity + church_tax
    net = pre_tax - total_tax
    return {
        "sale_price_eur": euro_text(sale_price_eur),
        "proceeds_eur": euro_text(proceeds),
        "cost_basis_eur": euro_text(basis_total_eur),
        "sell_fee_eur": euro_text(settings.sell_fee_eur),
        "pre_tax_profit_eur": euro_text(pre_tax),
        "taxable_gain_eur": euro_text(taxable),
        "capital_gains_tax_eur": euro_text(capital_tax),
        "solidarity_eur": euro_text(solidarity),
        "church_tax_eur": euro_text(church_tax),
        "total_tax_eur": euro_text(total_tax),
        "net_profit_eur": euro_text(net),
        "meets_minimum": net >= settings.minimum_net_profit_eur,
    }


def minimum_exit_price(*, basis_total_eur: Decimal, quantity: int, settings: TaxSettings) -> Decimal:
    """Lowest cent-priced EUR exit estimate that meets the configured net target."""
    if quantity <= 0:
        raise ValueError("Quantity must be positive")
    low = 0
    high = max(1, int((basis_total_eur + settings.sell_fee_eur + settings.minimum_net_profit_eur) / CENT / quantity) + 2)

    def reaches(price_cents: int) -> bool:
        price = Decimal(price_cents) * CENT
        proceeds = price * quantity
        pre_tax = proceeds - basis_total_eur - settings.sell_fee_eur
        taxable = max(Decimal("0"), pre_tax - settings.remaining_allowance_eur)
        church_rate = amount(settings.church_tax_rate_pct) / 100
        tax = taxable * (Decimal("1.055") + church_rate) / (Decimal("4") + church_rate)
        return pre_tax - tax >= settings.minimum_net_profit_eur

    while not reaches(high):
        high *= 2
    while low < high:
        mid = (low + high) // 2
        if reaches(mid):
            high = mid
        else:
            low = mid + 1
    return Decimal(low) * CENT


def analysis(
    *, basis_total_eur: Decimal, quantity: int, settings: TaxSettings,
    scenarios: dict[str, Decimal], basis_source: str,
) -> dict:
    threshold = minimum_exit_price(basis_total_eur=basis_total_eur, quantity=quantity, settings=settings)
    church_rate = amount(settings.church_tax_rate_pct) / 100
    marginal_rate_pct = (Decimal("1") + SOLIDARITY_RATE + church_rate) / (Decimal("4") + church_rate) * 100
    return {
        "basis_source": basis_source,
        "basis_total_eur": euro_text(basis_total_eur),
        "quantity": quantity,
        "church_tax_rate_pct": settings.church_tax_rate_pct,
        "marginal_tax_rate_pct": euro_text(marginal_rate_pct),
        "remaining_allowance_eur": euro_text(settings.remaining_allowance_eur),
        "minimum_net_profit_eur": euro_text(settings.minimum_net_profit_eur),
        "minimum_exit_eur_per_share": euro_text(threshold),
        "scenarios": {name: exit_estimate(
            basis_total_eur=basis_total_eur, sale_price_eur=price, quantity=quantity, settings=settings,
        ) for name, price in scenarios.items()},
        "limitations": "Estimate only: actual EUR FX, fill/slippage, fees, FIFO lots, other gains/losses and tax treatment can differ. Alpaca does not withhold German tax.",
    }
