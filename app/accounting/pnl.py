"""Explainable P&L (project.md sections 46, 47). Pure functions over recorded orders and funding.

    Net P&L = long trading P&L + short trading P&L + funding received - funding paid - fees - other costs

Trading P&L is the cash flow of the fills (sells minus buys) plus the mark-to-market value of any position
left open. Slippage is reported separately as an ESTIMATE against the mid price at submission and is already
inside the trading P&L, so it is never subtracted a second time.
"""

from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class OrderRecord:
    leg: str  # "long" | "short"
    side: str  # "BUY" | "SELL"
    quantity: Decimal
    price: Decimal
    fee: Decimal
    reference_price: Decimal | None = None  # mid at submission (slippage reference)


@dataclass(frozen=True)
class FundingRecord:
    leg: str
    expected: Decimal  # positive = received, negative = paid
    actual: Decimal | None


@dataclass
class TradePnL:
    long_trading: Decimal = D(0)
    short_trading: Decimal = D(0)
    funding_received: Decimal = D(0)
    funding_paid: Decimal = D(0)
    fees: Decimal = D(0)
    other_costs: Decimal = D(0)
    slippage_estimate: Decimal = D(0)
    funding_expected_vs_actual_difference: Decimal = D(0)
    unrealized_residual: Decimal = D(0)  # included in trading P&L; shown for transparency
    notes: list[str] = field(default_factory=list)

    @property
    def hedge_trading(self) -> Decimal:
        return self.long_trading + self.short_trading

    @property
    def funding_net(self) -> Decimal:
        return self.funding_received - self.funding_paid

    @property
    def total(self) -> Decimal:
        return self.hedge_trading + self.funding_net - self.fees - self.other_costs

    def as_dict(self) -> dict[str, str]:
        return {
            "funding_pnl": str(self.funding_net), "funding_received": str(self.funding_received),
            "funding_paid": str(self.funding_paid), "hedge_trading_pnl": str(self.hedge_trading),
            "long_trading_pnl": str(self.long_trading), "short_trading_pnl": str(self.short_trading),
            "fees": str(self.fees), "slippage_estimate": str(self.slippage_estimate),
            "other_costs": str(self.other_costs), "total_realized_pnl": str(self.total),
            "unrealized_residual": str(self.unrealized_residual),
            "funding_expected_vs_actual_difference": str(self.funding_expected_vs_actual_difference),
        }


def compute_pnl(
    orders: list[OrderRecord],
    funding: list[FundingRecord],
    residual_marks: dict[str, Decimal] | None = None,
    other_costs: Decimal = D(0),
) -> TradePnL:
    """`residual_marks` maps leg -> current mark price and is used only for quantity still open."""
    pnl = TradePnL(other_costs=other_costs)
    open_qty = {"long": D(0), "short": D(0)}
    for o in orders:
        sign = 1 if o.side == "SELL" else -1  # selling brings cash in, buying pays it out
        cash = sign * o.quantity * o.price
        if o.leg == "long":
            pnl.long_trading += cash
        else:
            pnl.short_trading += cash
        open_qty[o.leg] += o.quantity if o.side == "BUY" else -o.quantity
        pnl.fees += o.fee
        if o.reference_price is not None:
            adverse = (o.price - o.reference_price) if o.side == "BUY" else (o.reference_price - o.price)
            pnl.slippage_estimate += adverse * o.quantity

    for leg, qty in open_qty.items():
        if qty != 0:
            mark = (residual_marks or {}).get(leg)
            if mark is None:
                pnl.notes.append(f"{leg} leg still holds {qty} with no mark price: P&L incomplete")
                continue
            value = qty * mark  # a positive quantity is worth +qty*mark if closed at the mark
            pnl.unrealized_residual += value
            if leg == "long":
                pnl.long_trading += value
            else:
                pnl.short_trading += value

    for f in funding:
        amount = f.actual if f.actual is not None else f.expected
        if amount >= 0:
            pnl.funding_received += amount
        else:
            pnl.funding_paid += -amount
        if f.actual is not None:
            pnl.funding_expected_vs_actual_difference += f.actual - f.expected
        else:
            pnl.notes.append("funding not reconciled with an actual payment")
    return pnl
