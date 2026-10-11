"""Soccer goal model: Poisson attack/defence ratings with Dixon-Coles low-score correction.

    lambda_home = base * home_adv * attack_home * defence_away
    lambda_away = base * attack_away * defence_home

Ratings are fitted per competition by recency-weighted multiplicative updates,
shrunk toward 1.0 with `soccer_prior_matches` pseudo-matches. The full score
matrix gives home/draw/away, totals at the exact listed line, both-teams-to-score,
team totals and handicaps — so "strong favourite" never implies "many goals".

Limitations: no xG input (free providers do not supply it; see README), no lineup
or injury adjustments (reported, not modeled), Poisson independence apart from
the Dixon-Coles correction. Cross-competition games (UCL) only rate teams on UCL
results unless you load domestic results too.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

import numpy as np

from .config import ModelConfig
from .football_model import _d
from .market_discovery import ContractSpec
from .sports_data import normalize_name

MAX_GOALS = 12


def poisson_pmf(lam: float, n: int = MAX_GOALS) -> np.ndarray:
    lam = max(lam, 1e-9)
    k = np.arange(n + 1)
    logp = -lam + k * math.log(lam) - np.array([math.lgamma(i + 1) for i in k])
    p = np.exp(logp)
    p[-1] += max(0.0, 1.0 - p.sum())  # fold tail into the last bucket
    return p


def score_matrix(lam_h: float, lam_a: float, rho: float = 0.0) -> np.ndarray:
    m = np.outer(poisson_pmf(lam_h), poisson_pmf(lam_a))
    if rho:
        m[0, 0] *= max(1 - lam_h * lam_a * rho, 0)
        m[0, 1] *= max(1 + lam_h * rho, 0)
        m[1, 0] *= max(1 + lam_a * rho, 0)
        m[1, 1] *= max(1 - rho, 0)
        m /= m.sum()
    return m


def shift_matrix(remaining: np.ndarray, banked_home: int, banked_away: int) -> np.ndarray:
    """Final-score matrix = current score + remaining goals."""
    n = remaining.shape[0]
    out = np.zeros((n + banked_home, n + banked_away))
    out[banked_home:, banked_away:] = remaining
    return out


def soccer_prob(spec: ContractSpec, team_side: str | None, final: np.ndarray) -> float | None:
    """P(YES) from a final-score matrix final[i, j] = P(home i, away j) for the settlement window."""
    i = np.arange(final.shape[0])[:, None]
    j = np.arange(final.shape[1])[None, :]
    if spec.kind in ("winner", "spread", "team_total") and team_side not in ("home", "away"):
        return None
    diff = (i - j) if team_side != "away" else (j - i)
    if spec.kind == "winner":
        mask = diff > 0
    elif spec.kind == "draw":
        mask = (i - j) == 0
    elif spec.kind == "btts":
        mask = (i >= 1) & (j >= 1)
    elif spec.kind == "spread":
        mask = diff > spec.line if spec.strict else diff >= spec.line
    elif spec.kind in ("total", "team_total"):
        val = (i + j) if spec.kind == "total" else (i if team_side == "home" else j) + 0 * (i + j)
        if spec.direction == "over":
            mask = val > spec.line if spec.strict else val >= spec.line
        else:
            mask = val < spec.line if spec.strict else val <= spec.line
    else:
        return None
    mask = np.broadcast_to(mask, final.shape)
    return float(min(max(final[mask].sum(), 0.0005), 0.9995))


@dataclass
class SoccerPrediction:
    home: str
    away: str
    lam_home: float
    lam_away: float
    reliability: float
    games_home: float
    games_away: float
    notes: list[str] = field(default_factory=list)

    def matrix(self, rho: float, share: float = 1.0) -> np.ndarray:
        return score_matrix(self.lam_home * share, self.lam_away * share, rho if share >= 0.999 else 0.0)

    def outcome_probs(self, rho: float) -> dict[str, float]:
        m = self.matrix(rho)
        return {"home": float(np.tril(m, -1).sum()), "draw": float(np.trace(m)), "away": float(np.triu(m, 1).sum())}


class SoccerModel:
    sport = "soccer"

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.attack: dict[str, float] = {}
        self.defence: dict[str, float] = {}
        self.games: dict[str, float] = {}
        self.base = 1.35
        self.home_adv = 1.15
        self.n_games = 0

    def fit(self, rows: Iterable[dict], as_of: date | None = None, iterations: int = 60) -> "SoccerModel":
        rows = [r for r in rows if r["home_score"] is not None]
        self.n_games = len(rows)
        if not rows:
            return self
        as_of = as_of or max(_d(r["date"]) for r in rows)
        w = [0.5 ** (max((as_of - _d(r["date"])).days, 0) / self.cfg.recency_half_life_days) for r in rows]
        H = [normalize_name(r["home"]) for r in rows]
        A = [normalize_name(r["away"]) for r in rows]
        hg = [float(r["home_score"]) for r in rows]
        ag = [float(r["away_score"]) for r in rows]
        teams = set(H) | set(A)
        att = {t: 1.0 for t in teams}
        dfc = {t: 1.0 for t in teams}
        sw = sum(w)
        self.base = max(sum(wi * (h + a) for wi, h, a in zip(w, hg, ag)) / (2 * sw), 0.3)
        home_rate = sum(wi * h for wi, h in zip(w, hg)) / sw
        away_rate = sum(wi * a for wi, a in zip(w, ag)) / sw
        self.home_adv = min(max(math.sqrt(home_rate / max(away_rate, 1e-6)), 0.9), 1.5)
        k = self.cfg.soccer_prior_matches
        games = {t: 0.0 for t in teams}
        for wi, h, a in zip(w, H, A):
            games[h] += wi
            games[a] += wi
        for _ in range(iterations):
            num = {t: k for t in teams}
            den = {t: k for t in teams}
            for wi, h, a, x, y in zip(w, H, A, hg, ag):
                num[h] += wi * x / self.base
                den[h] += wi * self.home_adv * dfc[a]
                num[a] += wi * y / self.base
                den[a] += wi * dfc[h] / self.home_adv
            att = {t: num[t] / den[t] for t in teams}
            num = {t: k for t in teams}
            den = {t: k for t in teams}
            for wi, h, a, x, y in zip(w, H, A, hg, ag):
                num[a] += wi * x / self.base  # goals conceded by away team
                den[a] += wi * self.home_adv * att[h]
                num[h] += wi * y / self.base
                den[h] += wi * att[a] / self.home_adv
            dfc = {t: num[t] / den[t] for t in teams}
        # normalise geometric mean to 1
        ga = math.exp(sum(math.log(v) for v in att.values()) / len(att))
        gd = math.exp(sum(math.log(v) for v in dfc.values()) / len(dfc))
        self.attack = {t: v / ga for t, v in att.items()}
        self.defence = {t: v / gd for t, v in dfc.items()}
        self.base *= ga * gd
        self.games = games
        return self

    def predict(self, home: str, away: str, neutral: bool = False) -> SoccerPrediction:
        h, a = normalize_name(home), normalize_name(away)
        notes = [f"No historical matches found for {n}; using league-average strength (assumption)."
                 for t, n in ((h, home), (a, away)) if t not in self.attack]
        ha = 1.0 if neutral else self.home_adv
        lam_h = self.base * ha * self.attack.get(h, 1.0) * self.defence.get(a, 1.0)
        lam_a = self.base / (1.0 if neutral else self.home_adv) * self.attack.get(a, 1.0) * self.defence.get(h, 1.0)
        gh, ga = self.games.get(h, 0.0), self.games.get(a, 0.0)
        rel = min(1.0, min(gh, ga) / max(self.cfg.min_games_for_full_reliability, 1))
        if self.n_games < 60:
            rel *= 0.5
            notes.append(f"Only {self.n_games} historical matches loaded; reliability reduced.")
        return SoccerPrediction(home, away, lam_h, lam_a, round(rel, 3), gh, ga, notes)

    def share_for_period(self, period: str) -> float:
        s = self.cfg.soccer_first_half_share
        return {"full": 1.0, "1H": s, "2H": 1 - s}.get(period, 1.0)

    def pregame_matrix(self, pred: SoccerPrediction, period: str) -> np.ndarray:
        return pred.matrix(self.cfg.soccer_rho, self.share_for_period(period))
