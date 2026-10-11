"""American-football rating model shared by the NFL and college models.

Model (interpretable, testable):
  * Margin ratings r_i fitted by recency-weighted ridge regression on game margins:
        margin_home = HFA * (not neutral) + r_home - r_away + noise
  * Points model: offense o_i and defense d_i (points allowed above average):
        pts_home = mu + HFA/2 + o_home + d_away,  pts_away = mu - HFA/2 + o_away + d_home
  * Margin ~ Normal(mu_margin, sd_margin); total ~ Normal(mu_total, sd_total);
    integer outcomes handled with a 0.5 continuity correction.

Known limitations (documented in README): the normal approximation ignores NFL
"key numbers" (3, 7); injuries/QB changes are NOT automatically priced in —
they are reported so you can judge them; weather is reported, not modeled.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

import numpy as np

from .config import ModelConfig
from .market_discovery import ContractSpec
from .sports_data import normalize_name


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class Normal:
    mu: float
    sd: float

    def p_gt(self, line: float, strict: bool = True, integer: bool = True) -> float:
        """P(X > line) (strict) or P(X >= line)."""
        if self.sd <= 1e-9:
            return 1.0 if (self.mu > line if strict else self.mu >= line) else 0.0
        if integer:
            k = math.floor(line) + 1 if strict else math.ceil(line)
            return 1.0 - norm_cdf((k - 0.5 - self.mu) / self.sd)
        return 1.0 - norm_cdf((line - self.mu) / self.sd)

    def p_lt(self, line: float, strict: bool = True, integer: bool = True) -> float:
        if self.sd <= 1e-9:
            return 1.0 if (self.mu < line if strict else self.mu <= line) else 0.0
        if integer:
            k = math.ceil(line) - 1 if strict else math.floor(line)
            return norm_cdf((k + 0.5 - self.mu) / self.sd)
        return norm_cdf((line - self.mu) / self.sd)

    def p_eq(self, v: int) -> float:
        if self.sd <= 1e-9:
            return 1.0 if round(self.mu) == v else 0.0
        return norm_cdf((v + 0.5 - self.mu) / self.sd) - norm_cdf((v - 0.5 - self.mu) / self.sd)


@dataclass
class FootballDists:
    """Distributions of FINAL values for one settlement window (game, half, quarter)."""
    margin: Normal  # home - away
    total: Normal
    home_pts: Normal
    away_pts: Normal
    overtime_possible: bool = True  # full-game winner markets include OT


def football_prob(spec: ContractSpec, team_side: str | None, d: FootballDists) -> float | None:
    """Probability the YES side of `spec` settles true. team_side: 'home'|'away' for team-specific specs."""
    sign = 1.0
    if spec.kind in ("winner", "spread", "team_total"):
        if team_side not in ("home", "away"):
            return None
        sign = 1.0 if team_side == "home" else -1.0
    m = Normal(sign * d.margin.mu, d.margin.sd)
    if spec.kind == "winner":
        p = m.p_gt(0, strict=True)
        if d.overtime_possible:
            p += 0.5 * m.p_eq(0)  # tied after regulation -> OT ~ coin flip
        return _clip(p)
    if spec.kind == "draw":
        return _clip(d.margin.p_eq(0)) if not d.overtime_possible else None
    if spec.kind == "spread":
        return _clip(m.p_gt(spec.line or 0.0, spec.strict))
    if spec.kind == "total":
        return _clip(d.total.p_gt(spec.line, spec.strict) if spec.direction == "over" else d.total.p_lt(spec.line, spec.strict))
    if spec.kind == "team_total":
        pts = d.home_pts if team_side == "home" else d.away_pts
        return _clip(pts.p_gt(spec.line, spec.strict) if spec.direction == "over" else pts.p_lt(spec.line, spec.strict))
    return None


def _clip(p: float) -> float:
    return min(max(p, 0.0005), 0.9995)


@dataclass
class FootballPrediction:
    home: str
    away: str
    mu_margin: float
    mu_home_pts: float
    mu_away_pts: float
    sd_margin: float
    sd_total: float
    sd_team: float
    reliability: float
    games_home: float
    games_away: float
    notes: list[str] = field(default_factory=list)

    @property
    def mu_total(self) -> float:
        return self.mu_home_pts + self.mu_away_pts

    def win_prob_home(self) -> float:
        d = self.window_dists(1.0)
        return football_prob(ContractSpec("winner", team_text="home"), "home", d) or 0.5

    def window_dists(self, share: float, banked_home: float = 0.0, banked_away: float = 0.0,
                     overtime_possible: bool = True, possession_shift: float = 0.0) -> FootballDists:
        """Final-value distributions when `share` of the expected scoring is still to come."""
        share = max(share, 0.0)
        root = math.sqrt(share)
        mm = (banked_home - banked_away) + self.mu_margin * share + possession_shift
        mh = banked_home + self.mu_home_pts * share + possession_shift / 2
        ma = banked_away + self.mu_away_pts * share - possession_shift / 2
        return FootballDists(
            margin=Normal(mm, self.sd_margin * root),
            total=Normal(mh + ma, self.sd_total * root),
            home_pts=Normal(mh, self.sd_team * root),
            away_pts=Normal(ma, self.sd_team * root),
            overtime_possible=overtime_possible,
        )


class FootballModel:
    sport = "football"

    def __init__(self, cfg: ModelConfig, margin_sd: float, total_sd: float, team_sd: float, hfa: float,
                 unknown_prior: float = 0.0):
        self.cfg = cfg
        self.margin_sd, self.total_sd, self.team_sd, self.hfa = margin_sd, total_sd, team_sd, hfa
        self.unknown_prior = unknown_prior
        self.rating: dict[str, float] = {}
        self.off: dict[str, float] = {}
        self.dfn: dict[str, float] = {}
        self.games: dict[str, float] = {}
        self.mu_pts = 0.0
        self.n_games = 0
        self.fitted_through: str | None = None

    # ------------------------------------------------------------------ fit
    def fit(self, rows: Iterable[dict], as_of: date | None = None) -> "FootballModel":
        rows = [r for r in rows if r["home_score"] is not None and r["away_score"] is not None]
        self.n_games = len(rows)
        if not rows:
            return self
        as_of = as_of or max(_d(r["date"]) for r in rows)
        teams = sorted({normalize_name(r["home"]) for r in rows} | {normalize_name(r["away"]) for r in rows})
        idx = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        w = np.array([0.5 ** (max((as_of - _d(r["date"])).days, 0) / self.cfg.recency_half_life_days) for r in rows])
        counts = np.zeros(n)
        for r, wi in zip(rows, w):
            counts[idx[normalize_name(r["home"])]] += wi
            counts[idx[normalize_name(r["away"])]] += wi
        self.games = {t: float(counts[i]) for t, i in idx.items()}
        prior = np.array([self.unknown_prior if counts[i] < 3 else 0.0 for i in range(n)])
        lam = self.cfg.ridge_lambda

        # margin ratings
        X = np.zeros((len(rows), n))
        y = np.zeros(len(rows))
        for k, r in enumerate(rows):
            X[k, idx[normalize_name(r["home"])]] = 1
            X[k, idx[normalize_name(r["away"])]] = -1
            y[k] = (r["home_score"] - r["away_score"]) - (0 if r.get("neutral") else self.hfa)
        A = X.T @ (X * w[:, None]) + lam * np.eye(n)
        b = X.T @ (w * y) + lam * prior
        r_hat = np.linalg.solve(A, b)
        self.rating = {t: float(r_hat[i]) for t, i in idx.items()}

        # points model: params [o_0..o_n-1, d_0..d_n-1]
        self.mu_pts = float(np.average([r["home_score"] for r in rows] + [r["away_score"] for r in rows],
                                       weights=np.concatenate([w, w])))
        P = np.zeros((2 * len(rows), 2 * n))
        yp = np.zeros(2 * len(rows))
        for k, r in enumerate(rows):
            h, a = idx[normalize_name(r["home"])], idx[normalize_name(r["away"])]
            half = 0 if r.get("neutral") else self.hfa / 2
            P[2 * k, h] = 1
            P[2 * k, n + a] = 1
            yp[2 * k] = r["home_score"] - self.mu_pts - half
            P[2 * k + 1, a] = 1
            P[2 * k + 1, n + h] = 1
            yp[2 * k + 1] = r["away_score"] - self.mu_pts + half
        wp = np.concatenate([[w[k], w[k]] for k in range(len(rows))])
        prior_p = np.concatenate([prior / 2, -prior / 2])
        Ap = P.T @ (P * wp[:, None]) + lam * np.eye(2 * n)
        bp = P.T @ (wp * yp) + lam * prior_p
        sol = np.linalg.solve(Ap, bp)
        self.off = {t: float(sol[i]) for t, i in idx.items()}
        self.dfn = {t: float(sol[n + i]) for t, i in idx.items()}
        self.fitted_through = as_of.isoformat()
        return self

    # -------------------------------------------------------------- predict
    def predict(self, home: str, away: str, neutral: bool = False) -> FootballPrediction:
        h, a = normalize_name(home), normalize_name(away)
        notes: list[str] = []
        for t, label in ((h, home), (a, away)):
            if t not in self.rating:
                notes.append(f"No historical games found for {label}; using prior rating {self.unknown_prior:+.1f} (assumption).")
        rh = self.rating.get(h, self.unknown_prior)
        ra = self.rating.get(a, self.unknown_prior)
        hfa = 0.0 if neutral else self.hfa
        mu_margin = rh - ra + hfa
        oh, oa = self.off.get(h, self.unknown_prior / 2), self.off.get(a, self.unknown_prior / 2)
        dh, da = self.dfn.get(h, -self.unknown_prior / 2), self.dfn.get(a, -self.unknown_prior / 2)
        mu = self.mu_pts or (44.0 if self.sport == "nfl" else 55.0)
        total = max((mu + oh + da) + (mu + oa + dh), 10.0)
        home_pts = (total + mu_margin) / 2
        away_pts = (total - mu_margin) / 2
        gh, ga = self.games.get(h, 0.0), self.games.get(a, 0.0)
        rel = min(1.0, min(gh, ga) / max(self.cfg.min_games_for_full_reliability, 1))
        if self.n_games < 50:
            rel *= 0.5
            notes.append(f"Only {self.n_games} historical games loaded; reliability reduced.")
        return FootballPrediction(home, away, mu_margin, home_pts, away_pts, self.margin_sd, self.total_sd,
                                  self.team_sd, round(rel, 3), gh, ga, notes)

    def share_for_period(self, period: str) -> float:
        s = self.cfg.first_half_share
        return {"full": 1.0, "1H": s, "2H": 1 - s, "Q1": s / 2, "Q2": s / 2, "Q3": (1 - s) / 2, "Q4": (1 - s) / 2}.get(period, 1.0)

    def pregame_dists(self, pred: FootballPrediction, period: str) -> FootballDists:
        return pred.window_dists(self.share_for_period(period), overtime_possible=(period == "full"))


def _d(s: str | date | datetime) -> date:
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    return date.fromisoformat(str(s)[:10])
