"""Hold-versus-sell analysis for open positions.

Kalshi has no guaranteed "cash out" button: exiting means SELLING your contracts to
the best bids resting in the order book. This module compares, on the same basis
(dollars you end up with from this position from now on):

    EV(hold) = q * contracts * $1                    (no settlement fee)
    EV(sell) = sweep of the bid book for your contracts - selling fee

Sunk entry cost is reported (realized P/L if sold) but does NOT drive the decision:
we never recommend selling just because a position is losing, nor holding just to
avoid realizing a loss.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .fees import FeeModel
from .kalshi_client import OrderBook
from .market_discovery import SportsMarket
from .positions import Position

HOLD, SELL, REDUCE, NO_ACTION, UNAVAILABLE = "HOLD", "SELL / EXIT", "REDUCE EXPOSURE", "NO ACTION", "DATA UNAVAILABLE"


@dataclass
class ExitAnalysis:
    position: Position
    decision: str
    prob_win: float | None
    sell_price: float | None
    sellable_contracts: float
    gross_proceeds: float
    sell_fee: float
    net_proceeds: float
    ev_hold: float | None
    ev_sell: float | None
    realized_if_sold: float | None
    unrealized_mark: float | None
    reasons: list[str] = field(default_factory=list)
    liquidity_note: str = ""
    reduce_contracts: float = 0.0

    def render(self) -> str:
        p = self.position
        lines = [
            f"Position {p.id}: {p.contract} | {p.side.upper()} x{p.contracts:g} @ ${p.entry_price:.2f} (fees ${p.entry_fees:.2f})",
            f"  DECISION: {self.decision}" + (f" ({self.reduce_contracts:g} contracts)" if self.decision == REDUCE else ""),
        ]
        if self.prob_win is not None:
            lines.append(f"  Estimated probability this position pays $1/contract: {self.prob_win:.1%} (estimate)")
        if self.sell_price is not None:
            lines.append(f"  Best bid to sell into: ${self.sell_price:.2f} | sellable now: {self.sellable_contracts:g} contracts")
            lines.append(f"  Sale proceeds after fee ${self.sell_fee:.2f}: ${self.net_proceeds:.2f} | realized P/L if sold: ${self.realized_if_sold:+.2f}")
        if self.ev_hold is not None:
            lines.append(f"  Expected value of holding: ${self.ev_hold:.2f}  vs  selling now: ${(self.ev_sell or 0):.2f}")
        lines.append(f"  If held and wrong it pays $0" + (f" (giving up ${self.net_proceeds:.2f} available now)" if self.net_proceeds else ""))
        if self.liquidity_note:
            lines.append(f"  Liquidity: {self.liquidity_note}")
        lines += [f"  - {r}" for r in self.reasons]
        return "\n".join(lines)


def analyze_exit(position: Position, market: SportsMarket | None, prob_yes: float | None, settings: Settings,
                 orderbook: OrderBook | None = None, over_risk_limit: bool = False) -> ExitAnalysis:
    side = position.side
    C = position.contracts
    reasons: list[str] = []
    if market is None:
        return ExitAnalysis(position, UNAVAILABLE, None, None, 0, 0, 0, 0, None, None, None, None,
                            ["Market data unavailable - cannot price an exit."])
    q = None if prob_yes is None else (prob_yes if side == "yes" else 1 - prob_yes)
    fm = market.fee_model or FeeModel(settings.value.taker_fee_rate)

    # executable sale value
    if orderbook is not None:
        filled, gross = orderbook.sweep_sell(side, C)
        best = orderbook.best_yes_bid if side == "yes" else orderbook.best_no_bid
        best_price = best[0] if best else None
    else:
        best_price = market.bid(side)
        size = market.yes_bid_size if side == "yes" else None  # NO bid depth not in market summary
        filled = C if (best_price is not None and (size is None or size >= C)) else (size or 0.0)
        gross = filled * (best_price or 0.0)
    avg_price = gross / filled if filled else None
    sell_fee = fm.taker_fee(filled, avg_price) if filled and avg_price and 0 < avg_price < 1 else 0.0
    net = round(gross - sell_fee, 4)
    entry_cost = C * position.entry_price + position.entry_fees
    realized = round(net - entry_cost * (filled / C), 4) if filled else None
    mark = round(C * best_price - position.cost_basis, 4) if best_price is not None else None

    liq = ""
    if best_price is None:
        liq = "No buyers in the order book: the position cannot be sold right now."
    elif filled < C - 1e-9:
        liq = f"Only {filled:g} of {C:g} contracts can be sold at current bids; the rest would need a resting order."
    elif market.spread is not None and market.spread > settings.value.max_spread:
        liq = f"Wide spread ({market.spread:.2f}); selling gives up significant value versus the midpoint."

    if q is None or market.is_stale(settings.kalshi.stale_after_seconds):
        why = "updated probability unavailable" if q is None else "market data stale"
        return ExitAnalysis(position, UNAVAILABLE, q, best_price, filled, gross, sell_fee, net, None, None, realized,
                            mark, [f"Cannot compare hold vs sell: {why}."], liq)

    ev_hold_sold_part = q * filled  # value of holding exactly the contracts we could sell
    ev_hold = round(q * C, 4)
    margin = settings.live.hold_sell_margin * max(filled, 1)
    diff = net - ev_hold_sold_part  # >0 => selling beats holding for the sellable part

    if filled <= 0:
        decision = HOLD if not over_risk_limit else REDUCE
        reasons.append("No executable bids, so holding is the only immediate option." if decision == HOLD
                       else "Risk limit exceeded but no bids: place a resting sell order manually if you want out.")
    elif diff > margin:
        decision = SELL if filled >= C - 1e-9 else REDUCE
        reasons.append(f"Selling locks in ${net:.2f}, which beats the estimated hold value of ${ev_hold_sold_part:.2f} "
                       f"for those contracts (the bid implies {avg_price:.0%} vs our {q:.0%}).")
    elif diff < -margin:
        decision = HOLD
        reasons.append(f"Estimated hold value ${ev_hold_sold_part:.2f} exceeds sale proceeds ${net:.2f}: "
                       f"bids ({avg_price:.0%}) are below our estimated probability ({q:.0%}) after the selling fee.")
        if over_risk_limit:
            decision = REDUCE
            reasons.append("But the position breaches a configured risk limit, so trimming exposure is suggested.")
    else:
        decision = NO_ACTION
        reasons.append("Holding and selling are worth about the same after fees; either is reasonable.")
    if q < 0.5 and decision == HOLD:
        reasons.append("Note: holding is still more likely than not to lose; it is preferred only because the price is worse than the odds.")
    red = 0.0
    if decision == REDUCE:
        red = filled if filled < C else max(1.0, round(C / 2))
    return ExitAnalysis(position, decision, q, best_price, filled, round(gross, 4), sell_fee, net, ev_hold,
                        round(net + q * (C - filled), 4), realized, mark, reasons, liq, red)
