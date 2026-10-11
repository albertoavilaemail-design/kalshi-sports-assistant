"""Human-readable trade reports. Every number is labeled as a price (fact from Kalshi),
an estimate (model output) or an assumption."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .analysis import MarketAnalysis, prediction_summary
from .value_engine import NO_TRADES, Opportunity


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def format_entry_line(o: Opportunity, a: MarketAnalysis) -> str:
    return (f"{o.contract_label} | buy {o.side.upper()} x{o.contracts:g} @ ${o.price:.2f} | est. {o.prob:.1%} vs implied "
            f"{o.implied_prob:.1%} | net EV ${o.net_ev_total():+.3f} | max loss ${o.max_loss:.2f} | {a.market.ticker}")


def format_short(o: Opportunity, a: MarketAnalysis, i: int) -> str:
    g = a.game
    kick = g.start_time.astimezone().strftime("%a %m/%d %I:%M%p") if g and g.start_time else "time n/a"
    flag = "QUALIFIES" if o.qualifies else "does not qualify: " + "; ".join(o.fail_reasons[:2])
    return (f"{i:2d}. [{a.market.sport.upper()}/{a.market.competition}] {a.market.event_title} ({kick}, {o.context})\n"
            f"    {o.contract_label} -> BUY {o.side.upper()} @ ${o.price:.2f} | est. prob {o.prob:.1%} "
            f"(model {o.model_prob_raw:.1%}) | edge {o.edge:+.3f} | net EV/$ {o.net_ev_per_dollar():+.3f}\n"
            f"    size {o.contracts:g} contract(s), cost ${o.cost_total:.2f}, max loss ${o.max_loss:.2f}, "
            f"profit if right ${o.profit_if_win:.2f} | {flag}")


def format_full(o: Opportunity, a: MarketAnalysis, extras: dict[str, Any] | None = None) -> str:
    mk, g = a.market, a.game
    extras = extras or {}
    L = []
    L.append(f"==== {mk.sport.upper()} / {mk.competition} : {mk.event_title} ====")
    if g:
        kick = g.start_time.astimezone().strftime("%a %b %d %I:%M %p %Z") if g.start_time else "n/a"
        L.append(f"Matchup: {g.away.name} at {g.home.name}{' (neutral site)' if g.neutral else ''} | kickoff {kick} | status {g.status}")
        if g.is_live:
            L.append(f"Current: {g.score_line()}")
        ranks = [f"{t.name} #{t.rank}" for t in (g.home, g.away) if t.rank]
        if ranks:
            L.append("Rankings (context only, not a model input): " + ", ".join(ranks))
    L.append(f"Contract [PRICE DATA - Kalshi]: {mk.ticker} - {mk.title} | YES = {mk.yes_sub_title or mk.title}")
    L.append(f"Settlement rules [Kalshi]: {(mk.rules or 'not provided')[:400]}")
    L.append(f"Trade: BUY {o.side.upper()} ({o.contract_label}) at executable ask ${o.price:.2f}")
    L.append(f"  Market-implied probability: {o.implied_prob:.1%}   bid/ask {mk.yes_bid}/{mk.yes_ask} (YES side)   spread {o.spread}")
    L.append(f"  Estimated probability [MODEL ESTIMATE]: {o.prob:.1%} (pure model {o.model_prob_raw:.1%}, "
             "shrunk toward the market mid per value.model_weight)")
    L.append(f"  Edge: {o.edge:+.3f} | gross EV/contract ${o.gross_ev_per_contract:+.3f} | fees ${o.fee_total:.2f} total "
             f"| net EV ${o.net_ev_total():+.3f} ({o.net_ev_per_dollar():+.1%} per $ risked)")
    L.append(f"  Suggested size: {o.contracts:g} contract(s) = ${o.cost_total:.2f}   MAX POSSIBLE LOSS ${o.max_loss:.2f}   "
             f"profit if right ${o.profit_if_win:.2f}")
    L.append(f"  Liquidity: {o.depth if o.depth is not None else 'unknown'} contracts at the ask; last update {_ts(mk.fetched_at)} "
             f"({o.data_age_seconds:.0f}s ago)")
    L.append(f"Team analysis [MODEL]: {prediction_summary(a.prediction)}")
    if a.p_yes_pregame is not None and a.p_yes is not None and a.context != "pregame":
        L.append(f"  Pregame estimate P(YES) {a.p_yes_pregame:.1%} -> now {a.p_yes:.1%}")
    det = extras.get("detail") or {}
    if det.get("available"):
        inj = det.get("injuries") or []
        if inj:
            L.append(f"Injuries [FACT - {det.get('source')}; NOT priced into the model]:")
            for x in inj[:8]:
                L.append(f"  - {x['team']}: {x['player']} ({x.get('position') or '?'}) {x.get('status') or ''}")
        else:
            L.append(f"Injuries: none listed by {det.get('source')} (absence of data is not confirmation).")
    else:
        L.append("Injuries/news: unavailable from the data provider - check before trading.")
    w = extras.get("weather")
    if w:
        if w.get("available"):
            L.append(f"Weather [FORECAST - {w['source']}]: {w['temperature_f']:.0f}F, wind {w['wind_mph']:.0f} mph, "
                     f"precip {w['precip_mm']} mm (reported, not modeled)")
        else:
            L.append(f"Weather: unavailable ({w.get('reason')})")
    L.append("Why the price may be off: the model's rating-based estimate differs from the market by "
             f"{o.edge:+.1%}. That can mean the market over/under-weights recent results or rankings — "
             "or that the model is missing something.")
    L.append("Arguments against:")
    against = list(o.risks) + [
        "Simple rating models are usually less accurate than liquid betting markets.",
        "Injuries, lineup and weather news are not automatically priced into the model.",
    ] + a.notes
    L += [f"  - {x}" for x in against]
    L.append(f"Model confidence: reliability {o.reliability:.2f}/1.00 (data coverage, not the chance of winning).")
    L.append("Invalidated if: the ask rises above "
             f"${max(o.prob - 0.02 - o.fee_per_contract, 0.01):.2f}, key players are ruled out, the market closes, "
             "or the data becomes stale.")
    if not o.qualifies:
        L.append("STATUS: DOES NOT QUALIFY -> " + "; ".join(o.fail_reasons))
    for n in o.notes:
        L.append(f"Note: {n}")
    L.append(f"Link: {mk.url}")
    L.append(f"Generated {_ts(time.time())}. Estimates are not guarantees; never risk money you need.")
    return "\n".join(L)


def format_ranked(pairs: list[tuple[Opportunity, MarketAnalysis]], limit: int = 10, show_all: bool = False) -> str:
    q = [(o, a) for o, a in pairs if o.qualifies]
    if not q and not show_all:
        return NO_TRADES
    shown = (pairs if show_all else q)[:limit]
    body = "\n".join(format_short(o, a, i + 1) for i, (o, a) in enumerate(shown))
    head = f"{len(q)} qualifying opportunit{'y' if len(q) == 1 else 'ies'} (ranked by net EV x reliability x liquidity)"
    if not q:
        head = NO_TRADES + " Showing best candidates for reference:"
    return head + "\n" + body
