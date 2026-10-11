"""In-game probability models.

Both sports use the same idea: FINAL = what is already banked in the settlement
window + what the pregame model expects in the time that is LEFT, with uncertainty
shrinking as time runs out. The probabilities therefore move with score, clock,
red cards and (optionally) possession — never a frozen pregame number.

Returns None ("DATA UNAVAILABLE") when the clock, period or score is missing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from .config import LiveConfig, ModelConfig
from .football_model import FootballDists, FootballPrediction
from .market_discovery import ContractSpec
from .soccer_model import SoccerPrediction, score_matrix, shift_matrix
from .sports_data import GameState

log = logging.getLogger(__name__)

QUARTER_SECONDS = 900.0
FOOTBALL_PERIOD_QUARTERS = {"full": (1, 4), "1H": (1, 2), "2H": (3, 4), "Q1": (1, 1), "Q2": (2, 2), "Q3": (3, 3), "Q4": (4, 4)}
SOCCER_PERIOD_HALVES = {"full": (1, 2), "1H": (1, 1), "2H": (2, 2)}
OT_REMAINING_SHARE = 0.08  # assumption: scoring left in a sudden-death OT period


@dataclass
class WindowState:
    banked_home: float
    banked_away: float
    remaining_share: float  # share of a full game's expected scoring still to come in this window
    finished: bool
    elapsed_fraction: float  # of the whole game (for display/momentum)


def _football_quarter_shares(first_half_share: float) -> list[float]:
    s = first_half_share
    return [s / 2, s / 2, (1 - s) / 2, (1 - s) / 2]


def football_window(game: GameState, period: str, mcfg: ModelConfig) -> WindowState | None:
    if game.home_score is None or game.away_score is None:
        return None
    q0, q1 = FOOTBALL_PERIOD_QUARTERS.get(period, (1, 4))
    shares = _football_quarter_shares(mcfg.first_half_share)
    hp, ap = game.home_period_scores, game.away_period_scores
    if game.status == "final":
        if period == "full":
            return WindowState(game.home_score, game.away_score, 0.0, True, 1.0)
        return WindowState(sum(hp[q0 - 1:q1]), sum(ap[q0 - 1:q1]), 0.0, True, 1.0)
    if game.status not in ("in_progress", "halftime"):
        return None
    cur = game.period
    clock = game.clock_seconds
    if game.status == "halftime":
        cur, clock = 2, 0.0
    if cur <= 0 or clock is None:
        return None
    if period == "full":
        bh, ba = game.home_score, game.away_score
    else:
        if len(hp) < min(cur, q1) and cur >= q0:
            return None  # linescores missing -> cannot know window score
        bh, ba = sum(hp[q0 - 1:min(cur, q1)]), sum(ap[q0 - 1:min(cur, q1)])
    if cur > 4:  # overtime
        rem = OT_REMAINING_SHARE if period == "full" else 0.0
        return WindowState(bh, ba, rem, rem == 0.0, 1.0)
    rem = 0.0
    for q in range(q0, q1 + 1):
        if q > cur:
            rem += shares[q - 1]
        elif q == cur:
            rem += shares[q - 1] * max(min(clock, QUARTER_SECONDS), 0.0) / QUARTER_SECONDS
    elapsed = ((cur - 1) * QUARTER_SECONDS + (QUARTER_SECONDS - min(clock, QUARTER_SECONDS))) / (4 * QUARTER_SECONDS)
    finished = cur > q1 or (cur == q1 and clock <= 0)
    return WindowState(bh, ba, 0.0 if finished else rem, finished, elapsed)


def football_live_dists(pred: FootballPrediction, game: GameState, period: str, mcfg: ModelConfig,
                        lcfg: LiveConfig, momentum_shift: float = 0.0) -> FootballDists | None:
    ws = football_window(game, period, mcfg)
    if ws is None:
        return None
    poss = 0.0
    if lcfg.football_possession_value and game.possession and ws.remaining_share > 0:
        poss = lcfg.football_possession_value * (1 if game.possession == "home" else -1)
    shift = poss + momentum_shift * ws.remaining_share
    return pred.window_dists(ws.remaining_share, ws.banked_home, ws.banked_away,
                             overtime_possible=(period == "full"), possession_shift=shift)


# ------------------------------------------------------------------- soccer
def soccer_elapsed_minutes(game: GameState) -> float | None:
    if game.status == "halftime":
        return 45.0
    if game.clock_seconds is not None:
        return game.clock_seconds / 60.0
    txt = (game.display_clock or "").replace("'", "")
    try:
        base, _, extra = txt.partition("+")
        return float(base) + (float(extra) if extra else 0.0)
    except ValueError:
        return None


def soccer_window(game: GameState, period: str, mcfg: ModelConfig, lcfg: LiveConfig) -> WindowState | None:
    if game.home_score is None or game.away_score is None:
        return None
    h0, h1 = SOCCER_PERIOD_HALVES.get(period, (1, 2))
    hp, ap = game.home_period_scores, game.away_period_scores
    if game.status == "final":
        if period == "full":
            return WindowState(game.home_score, game.away_score, 0.0, True, 1.0)
        if len(hp) < h1:
            return None
        return WindowState(sum(hp[h0 - 1:h1]), sum(ap[h0 - 1:h1]), 0.0, True, 1.0)
    if game.status not in ("in_progress", "halftime"):
        return None
    minute = soccer_elapsed_minutes(game)
    half = 1 if game.status == "halftime" else (game.period or (1 if (minute or 0) <= 45 else 2))
    if minute is None or half <= 0:
        return None
    if half > 2:
        return None  # extra time / penalties: regulation markets usually settled; refuse to guess
    s1 = mcfg.soccer_first_half_share
    st1 = lcfg.soccer_stoppage_minutes / 3
    st2 = lcfg.soccer_stoppage_minutes * 2 / 3
    if game.status == "halftime":
        rem1, rem2 = 0.0, 1.0
    elif half == 1:
        rem1 = max(45 + st1 - minute, 0.5) / (45 + st1)
        rem2 = 1.0
    else:
        rem1 = 0.0
        rem2 = max(90 + st2 - max(minute, 45), 0.5) / (45 + st2)
    share = {"full": s1 * rem1 + (1 - s1) * rem2, "1H": s1 * rem1, "2H": (1 - s1) * rem2}[period]
    if period == "full":
        bh, ba = game.home_score, game.away_score
    else:
        if half >= h0 and len(hp) < min(half, h1):
            if period == "1H" and half == 1:
                bh, ba = game.home_score, game.away_score
            else:
                return None
        else:
            bh, ba = sum(hp[h0 - 1:min(half, h1)]), sum(ap[h0 - 1:min(half, h1)])
        if period == "2H" and half == 1:
            bh, ba = 0, 0
    finished = (period == "1H" and (half >= 2 or game.status == "halftime"))
    return WindowState(bh, ba, 0.0 if finished else share, finished, min(minute / 90.0, 1.0))


def soccer_live_matrix(pred: SoccerPrediction, game: GameState, period: str, mcfg: ModelConfig,
                       lcfg: LiveConfig, momentum_factor: float = 0.0) -> np.ndarray | None:
    ws = soccer_window(game, period, mcfg, lcfg)
    if ws is None:
        return None
    lh = pred.lam_home * ws.remaining_share
    la = pred.lam_away * ws.remaining_share
    # Red cards (assumption-based multipliers, configurable)
    lh *= (lcfg.soccer_red_card_own_factor ** game.red_cards_home) * (lcfg.soccer_red_card_opp_factor ** game.red_cards_away)
    la *= (lcfg.soccer_red_card_own_factor ** game.red_cards_away) * (lcfg.soccer_red_card_opp_factor ** game.red_cards_home)
    if momentum_factor:
        lh *= max(1 + momentum_factor, 0.5)
        la *= max(1 - momentum_factor, 0.5)
    remaining = score_matrix(lh, la, 0.0)
    return shift_matrix(remaining, int(ws.banked_home), int(ws.banked_away))


def window_elapsed(game: GameState, mcfg: ModelConfig, lcfg: LiveConfig) -> float | None:
    ws = football_window(game, "full", mcfg) if game.sport != "soccer" else soccer_window(game, "full", mcfg, lcfg)
    return ws.elapsed_fraction if ws else None


def spec_period(spec: ContractSpec) -> str:
    return spec.period or "full"
