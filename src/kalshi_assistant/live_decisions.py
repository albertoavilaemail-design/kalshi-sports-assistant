"""In-game ENTER / HOLD / SELL decisions, evaluated on every monitoring tick.

This is where "one side has momentum" is handled — honestly:

* Momentum is MEASURED and reported: scoring run over the last N minutes, how far
  the Kalshi price moved, and how far our live model moved over the same window.
* What actually matters for value is whether the PRICE moved more or less than the
  updated probability. If a team goes on a run and the market jumps further than the
  score/clock justify, the value (if any) is on the other side; if the market lags,
  the value is on the team that scored. The live model already re-prices every score
  and clock change, so this comparison is built into the edge calculation.
* "Hot hand" persistence beyond the score itself is weakly supported in the research,
  so `live.momentum_weight` defaults to 0 (reported, not traded). You can raise it
  after paper trading shows it helps — the setting is recorded with every decision.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .analysis import OK, Analyzer, MarketAnalysis, elapsed
from .bankroll import Bankroll
from .cashout import ExitAnalysis, analyze_exit
from .market_discovery import SportsMarket
from .positions import PositionStore
from .sports_data import GameState
from .value_engine import Opportunity, rank


@dataclass
class Momentum:
    window_minutes: int
    home_points: float = 0.0
    away_points: float = 0.0
    price_moves: dict[str, float] = field(default_factory=dict)  # ticker -> change in YES mid
    model_moves: dict[str, float] = field(default_factory=dict)  # ticker -> change in model P(YES)
    available: bool = False

    @property
    def run(self) -> float:
        return self.home_points - self.away_points

    def describe(self, game: GameState) -> str:
        if not self.available:
            return f"Momentum: not enough history yet (needs snapshots over {self.window_minutes} min)."
        if self.run == 0:
            who = "no scoring edge"
        else:
            leader = game.home.name if self.run > 0 else game.away.name
            hi, lo = max(self.home_points, self.away_points), min(self.home_points, self.away_points)
            who = f"{leader} on a {hi:g}-{lo:g} run"
        return f"Momentum (last {self.window_minutes} min): {who}."

    def overreaction_notes(self) -> list[str]:
        notes = []
        for t, pm in self.price_moves.items():
            mm = self.model_moves.get(t)
            if mm is None or abs(pm) < 0.05:
                continue
            if abs(pm) > abs(mm) + 0.05 and pm * mm >= 0:
                notes.append(f"{t}: price moved {pm:+.2f} but our estimate only {mm:+.2f} -> market may be OVER-reacting.")
            elif abs(mm) > abs(pm) + 0.05:
                notes.append(f"{t}: our estimate moved {mm:+.2f} but price only {pm:+.2f} -> market may be LAGGING.")
        return notes


def measure_momentum(analyzer: Analyzer, game: GameState, tickers: list[str]) -> Momentum:
    win = analyzer.settings.live.momentum_window_minutes
    mom = Momentum(win)
    since = time.time() - win * 60
    for t in tickers:
        snaps = analyzer.storage.snapshots_since(t, since)
        if len(snaps) < 2:
            continue
        first, last = snaps[0], snaps[-1]
        if last["ts"] - first["ts"] < win * 60 * 0.5:
            continue
        mom.available = True
        if first["home_score"] is not None and last["home_score"] is not None:
            mom.home_points = max(mom.home_points, last["home_score"] - first["home_score"])
            mom.away_points = max(mom.away_points, last["away_score"] - first["away_score"])
        if None not in (first["yes_bid"], first["yes_ask"], last["yes_bid"], last["yes_ask"]):
            mom.price_moves[t] = round((last["yes_bid"] + last["yes_ask"]) / 2 - (first["yes_bid"] + first["yes_ask"]) / 2, 3)
        if first["model_prob"] is not None and last["model_prob"] is not None:
            mom.model_moves[t] = round(last["model_prob"] - first["model_prob"], 3)
    return mom


def momentum_adjustment(analyzer: Analyzer, game: GameState, mom: Momentum) -> float:
    """Optional, configurable tilt (default weight 0 => no effect)."""
    w = analyzer.settings.live.momentum_weight
    if not w or not mom.available or mom.run == 0:
        return 0.0
    if game.sport == "soccer":
        return max(min(w * mom.run, 0.3), -0.3)  # multiplicative tilt on remaining scoring rates
    return max(min(w * mom.run, 7.0), -7.0)  # points of margin over the remaining game


@dataclass
class LiveGameReport:
    game: GameState
    analyses: list[MarketAnalysis]
    entries: list[tuple[Opportunity, MarketAnalysis]]
    near_misses: list[tuple[Opportunity, MarketAnalysis]]
    exits: list[ExitAnalysis]
    momentum: Momentum
    momentum_applied: float
    unavailable: list[MarketAnalysis]


def evaluate_live_game(analyzer: Analyzer, game: GameState, markets: list[SportsMarket],
                       account: str = "paper", record_snapshots: bool = True) -> LiveGameReport:
    tickers = [m.ticker for m in markets]
    mom = measure_momentum(analyzer, game, tickers)
    adj = momentum_adjustment(analyzer, game, mom)
    analyses = [analyzer.analyze_market(m, momentum=adj) for m in markets]
    ef = elapsed(game, analyzer.settings)
    if record_snapshots:
        for a in analyses:
            analyzer.storage.add_snapshot(a.market.ticker, game.game_id, a.market.yes_bid, a.market.yes_ask,
                                          a.p_yes, game.home_score, game.away_score, ef)
    pairs = analyzer.opportunities(analyses, account)
    entries = [(o, a) for o, a in pairs if o.qualifies]
    entries.sort(key=lambda t: t[0].score, reverse=True)
    near = sorted([(o, a) for o, a in pairs if not o.qualifies and o.edge > 0], key=lambda t: -t[0].edge)[:3]

    exits: list[ExitAnalysis] = []
    by_ticker = {a.market.ticker: a for a in analyses}
    store = PositionStore(analyzer.storage)
    br = Bankroll(analyzer.storage, analyzer.settings.risk)
    event_tickers = {m.event_ticker for m in markets}
    for p in store.open():
        if p.event_ticker not in event_tickers and p.market_ticker not in by_ticker:
            continue
        a = by_ticker.get(p.market_ticker)
        mk = a.market if a else analyzer.market_by_ticker(p.market_ticker)
        prob = a.p_yes if (a and a.status == OK) else None
        ob = None
        if mk is not None:
            try:
                ob = analyzer.client.get_orderbook(mk.ticker)
            except Exception:  # noqa: BLE001
                ob = None
        over = (br.open_exposure(p.mode, p.event_ticker) > analyzer.settings.risk.max_event_exposure_dollars + 1e-9
                or br.realized_since(p.mode, br.day_start()) <= -analyzer.settings.risk.daily_loss_limit_dollars)
        exits.append(analyze_exit(p, mk, prob, analyzer.settings, ob, over))
    unavailable = [a for a in analyses if a.status != OK]
    return LiveGameReport(game, analyses, entries, near, exits, mom, adj, unavailable)


def render_live_report(r: LiveGameReport, title: str = "LIVE") -> str:
    from .report import format_entry_line

    g = r.game
    lines = [f"=== {title}: {g.title} ===", f"Score: {g.score_line()}"]
    if g.sport == "soccer" and (g.red_cards_home or g.red_cards_away):
        lines.append(f"Red cards: {g.home.name} {g.red_cards_home}, {g.away.name} {g.red_cards_away}")
    lines.append(r.momentum.describe(g))
    lines += [f"  {n}" for n in r.momentum.overreaction_notes()]
    lines.append(f"  Momentum tilt applied to probabilities: {r.momentum_applied:+.2f}"
                 + ("  (off by default: live.momentum_weight = 0)" if not r.momentum_applied else " (experimental)"))
    lines.append("")
    lines.append("1) ENTER A NEW POSITION?")
    if r.entries:
        for o, a in r.entries[:5]:
            lines.append("   ENTER  " + format_entry_line(o, a))
    else:
        lines.append("   No qualifying trades found.")
        for o, a in r.near_misses:
            lines.append(f"   (near miss) {o.contract_label} {o.side.upper()} @ ${o.price:.2f}: " + "; ".join(o.fail_reasons[:2]))
    lines.append("")
    sells = [e for e in r.exits if e.decision in ("SELL / EXIT", "REDUCE EXPOSURE")]
    holds = [e for e in r.exits if e not in sells]
    lines.append("2) KEEP HOLDING EXISTING POSITIONS?")
    lines += ["   " + e.render().replace("\n", "\n   ") for e in holds] or [
        "   No open positions on this game." if not r.exits else "   None - see section 3."]
    lines.append("")
    lines.append("3) SELL EXISTING POSITIONS?")
    lines += ["   " + e.render().replace("\n", "\n   ") for e in sells] or ["   Selling is not preferred for any open position right now."]
    if r.unavailable:
        lines.append("")
        lines.append(f"DATA UNAVAILABLE for {len(r.unavailable)} market(s): " +
                     "; ".join(f"{a.market.ticker} ({a.notes[-1] if a.notes else 'unknown'})" for a in r.unavailable[:4]))
    lines.append("All probabilities are model estimates, not guarantees.")
    return "\n".join(lines)


def best_entries(r: LiveGameReport, n: int = 3) -> list[Opportunity]:
    return rank([o for o, _ in r.entries])[:n]
