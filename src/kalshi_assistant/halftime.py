"""Halftime reports.

Halftime is detected from the live feed (ESPN status STATUS_HALFTIME, or period 2
with 0:00 on the clock for football). The report compares pregame expectations with
the first half, re-prices every listed full-game and second-half market with the live
model, and gives THREE separate decisions: enter new, keep holding, sell.

A trailing favourite is not automatically value, and a high-scoring first half does not
imply a high-scoring second half: the second-half expectation comes from team strength
and time remaining, and only the price comparison decides.
"""
from __future__ import annotations

from .analysis import Analyzer
from .live_decisions import LiveGameReport, evaluate_live_game, render_live_report
from .market_discovery import SportsMarket
from .sports_data import GameState


def is_halftime(game: GameState) -> bool:
    if game.status == "halftime":
        return True
    if game.sport != "soccer" and game.status == "in_progress" and game.period == 2 \
            and game.clock_seconds is not None and game.clock_seconds <= 0:
        return True
    return False


def halftime_report(analyzer: Analyzer, game: GameState, markets: list[SportsMarket],
                    account: str = "paper") -> tuple[str, LiveGameReport]:
    r = evaluate_live_game(analyzer, game, markets, account)
    lines = [f"######## HALFTIME REPORT: {game.title} ########"]
    pre = [(a.market, a.p_yes_pregame, a.p_yes) for a in r.analyses if a.p_yes_pregame is not None and a.p_yes is not None]
    if r.analyses and r.analyses[0].prediction is not None:
        from .analysis import prediction_summary
        lines.append("Pregame expectation " + prediction_summary(r.analyses[0].prediction))
    lines.append(f"First half: {game.score_line()}")
    if game.home_period_scores:
        lines.append(f"  By period: {game.home.name} {game.home_period_scores} | {game.away.name} {game.away_period_scores}")
    if game.sport == "soccer":
        lines.append(f"  Red cards: {game.home.name} {game.red_cards_home}, {game.away.name} {game.red_cards_away}")
    try:
        det = analyzer.provider.game_detail(game.sport, game.competition, game.game_id)
    except Exception:  # noqa: BLE001
        det = {"available": False}
    if det.get("available") and det.get("team_stats"):
        lines.append(f"First-half team stats [FACT - {det.get('source')}]:")
        for team, stats in det["team_stats"].items():
            keep = list(stats.items())[:8]
            lines.append(f"  {team}: " + ", ".join(f"{k} {v}" for k, v in keep))
    else:
        lines.append("First-half stats: unavailable from provider (xG is not available on free data sources).")
    if pre:
        lines.append("How the outlook changed [MODEL ESTIMATES]:")
        for mk, p0, p1 in pre[:10]:
            lines.append(f"  {mk.spec.describe() if mk.spec else mk.title}: pregame {p0:.1%} -> now {p1:.1%} | "
                         f"Kalshi YES bid/ask {mk.yes_bid}/{mk.yes_ask}")
    lines.append("")
    lines.append(render_live_report(r, title="HALFTIME DECISIONS"))
    return "\n".join(lines), r
