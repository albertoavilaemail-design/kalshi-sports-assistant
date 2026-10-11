"""Value engine: is the executable price good enough, after fees, to justify a trade?

For a $1 binary contract bought at price p with estimated win probability q:
    gross EV / contract = q * $1 - p
    net EV / contract   = q - p - fee(C, p)/C - slippage
    max loss            = C * p + fee(C, p)          (contract settles $0)
    profit if win       = C * (1 - p) - fee(C, p)    (no settlement fee)

A high probability alone never qualifies a trade: the price must leave enough edge.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Settings
from .fees import FeeModel
from .market_discovery import SportsMarket


@dataclass
class Opportunity:
    market: SportsMarket
    side: str  # "yes" | "no"
    price: float  # executable ask for this side
    model_prob_raw: float  # pure model probability for this side
    prob: float  # probability used for decisions (model blended with market mid)
    implied_prob: float  # = executable price
    contracts: float
    fee_total: float
    reliability: float
    context: str  # pregame | live | halftime
    depth: float | None
    spread: float | None
    data_age_seconds: float
    qualifies: bool = False
    fail_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    score: float = 0.0
    evaluated_at: float = field(default_factory=time.time)

    @property
    def edge(self) -> float:
        return self.prob - self.implied_prob

    @property
    def gross_ev_per_contract(self) -> float:
        return self.prob - self.price

    @property
    def fee_per_contract(self) -> float:
        return self.fee_total / self.contracts if self.contracts else 0.0

    def net_ev_per_contract(self, slippage: float = 0.0) -> float:
        return self.prob - self.price - self.fee_per_contract - slippage

    @property
    def cost_total(self) -> float:
        return round(self.contracts * self.price + self.fee_total, 4)

    @property
    def max_loss(self) -> float:
        return self.cost_total

    @property
    def profit_if_win(self) -> float:
        return round(self.contracts * (1 - self.price) - self.fee_total, 4)

    def net_ev_total(self, slippage: float = 0.0) -> float:
        return self.net_ev_per_contract(slippage) * self.contracts

    def net_ev_per_dollar(self, slippage: float = 0.0) -> float:
        return self.net_ev_total(slippage) / self.cost_total if self.cost_total else 0.0

    @property
    def contract_label(self) -> str:
        base = self.market.spec.describe() if self.market.spec else self.market.title
        return base if self.side == "yes" else f"NOT ({base})"


def blend(model_p_yes: float, market: SportsMarket, model_weight: float) -> tuple[float, str | None]:
    if market.yes_bid is not None and market.yes_ask is not None:
        mid = (market.yes_bid + market.yes_ask) / 2
        return model_weight * model_p_yes + (1 - model_weight) * mid, None
    return model_p_yes, "No two-sided market; probability not blended with market mid."


def evaluate_side(market: SportsMarket, side: str, model_p_yes: float, reliability: float, settings: Settings,
                  context: str = "pregame", contracts: float = 1.0, now: float | None = None) -> Opportunity | None:
    """Evaluate buying `side` at the current ask. Returns None when there is no ask on that side."""
    v = settings.value
    price = market.ask(side)
    if price is None:
        return None
    p_yes, note = blend(model_p_yes, market, v.model_weight)
    prob = p_yes if side == "yes" else 1 - p_yes
    raw = model_p_yes if side == "yes" else 1 - model_p_yes
    fm = market.fee_model or FeeModel(v.taker_fee_rate, v.maker_fee_rate)
    fee = fm.taker_fee(contracts, price) if contracts > 0 else 0.0
    # Buying NO matches resting YES bids, so NO-ask depth = YES-bid size.
    depth = market.yes_ask_size if side == "yes" else market.yes_bid_size
    opp = Opportunity(
        market=market, side=side, price=price, model_prob_raw=raw, prob=prob, implied_prob=price,
        contracts=contracts, fee_total=fee, reliability=reliability, context=context,
        depth=depth, spread=market.spread, data_age_seconds=market.age_seconds(now),
    )
    if note:
        opp.notes.append(note)
    _qualify(opp, settings, now)
    return opp


def _qualify(opp: Opportunity, s: Settings, now: float | None) -> None:
    v = s.value
    r = opp.fail_reasons
    mk = opp.market
    if not mk.is_tradable:
        r.append(f"market status is '{mk.status}', not open for trading")
    if mk.is_stale(s.kalshi.stale_after_seconds, now):
        r.append(f"price data is stale ({opp.data_age_seconds:.0f}s old) - re-verify before trading")
    if opp.spread is None:
        r.append("no two-sided quote (bid or ask missing)")
    elif opp.spread > v.max_spread:
        r.append(f"bid-ask spread {opp.spread:.2f} exceeds {v.max_spread:.2f}")
    if opp.depth is not None and opp.depth < v.min_depth_contracts:
        r.append(f"only {opp.depth:.0f} contracts at the ask (< {v.min_depth_contracts:.0f})")
    if mk.yes_bid is not None and mk.yes_ask is not None:
        mid = (mk.yes_bid + mk.yes_ask) / 2
        side_mid = mid if opp.side == "yes" else 1 - mid
        gap = abs(opp.model_prob_raw - side_mid)
        if gap > v.max_model_market_gap:
            r.append(f"model differs from market by {gap:.0%} (> {v.max_model_market_gap:.0%}): treated as missing information, not an edge")
    if opp.reliability < v.min_reliability:
        r.append(f"model reliability {opp.reliability:.2f} below {v.min_reliability:.2f}")
    if opp.edge < v.min_edge:
        r.append(f"edge {opp.edge:+.3f} below minimum {v.min_edge:.3f}")
    ev_d = opp.net_ev_per_dollar(v.slippage_per_contract)
    if ev_d < v.min_net_ev_per_dollar:
        r.append(f"net EV per $ {ev_d:+.3f} below minimum {v.min_net_ev_per_dollar:.3f}")
    opp.qualifies = not r
    liq = 1.0 if opp.depth is None else min(1.0, (opp.depth / max(v.min_depth_contracts * 4, 1)) ** 0.5)
    opp.score = max(ev_d, -1.0) * opp.reliability * liq

    if opp.price >= 0.85:
        opp.risks.append("High price: small profit if right, large loss relative to profit if wrong.")
    if opp.price <= 0.15:
        opp.risks.append("Long shot: most likely outcome is losing the full stake.")
    if opp.model_prob_raw - opp.prob > 0.08 or opp.prob - opp.model_prob_raw > 0.08:
        opp.risks.append("Model and market disagree strongly - the model may be missing information (injuries, lineups).")
    if opp.contracts and opp.fee_per_contract / max(opp.price, 0.01) > 0.04:
        opp.risks.append("Per-order fee rounding is large relative to this small position.")


def evaluate_market(market: SportsMarket, model_p_yes: float, reliability: float, settings: Settings,
                    context: str = "pregame", now: float | None = None) -> list[Opportunity]:
    out = []
    for side in ("yes", "no"):
        o = evaluate_side(market, side, model_p_yes, reliability, settings, context, 1.0, now)
        if o:
            out.append(o)
    return out


def resize(opp: Opportunity, contracts: float, settings: Settings) -> Opportunity:
    """Recompute fees and qualification for the actual position size."""
    fm = opp.market.fee_model or FeeModel(settings.value.taker_fee_rate)
    opp.contracts = contracts
    opp.fee_total = fm.taker_fee(contracts, opp.price) if contracts > 0 else 0.0
    opp.fail_reasons = []
    opp.risks = []
    _qualify(opp, settings, None)
    return opp


def rank(opps: list[Opportunity], only_qualifying: bool = True) -> list[Opportunity]:
    pool = [o for o in opps if o.qualifies] if only_qualifying else list(opps)
    return sorted(pool, key=lambda o: o.score, reverse=True)


NO_TRADES = "No qualifying trades found."
