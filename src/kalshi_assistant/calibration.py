"""Probability scoring, calibration tables and walk-forward backtests.

A backtest re-fits the model using ONLY games before each test week, predicts that
week, and scores the predictions (Brier score, log loss, calibration bins).
It evaluates the probability model; it does NOT prove trading profit, because
historical Kalshi executable prices are not part of this dataset. Trading results
come from paper trading (see paper_trading.performance).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable, Sequence


def brier(probs: Sequence[float], outcomes: Sequence[int]) -> float | None:
    if not probs:
        return None
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def log_loss(probs: Sequence[float], outcomes: Sequence[int], eps: float = 1e-6) -> float | None:
    if not probs:
        return None
    total = 0.0
    for p, o in zip(probs, outcomes):
        p = min(max(p, eps), 1 - eps)
        total += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / len(probs)


def calibration_table(probs: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> list[dict]:
    rows = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(probs) if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if not idx:
            continue
        rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx),
                     "mean_pred": sum(probs[i] for i in idx) / len(idx),
                     "observed": sum(outcomes[i] for i in idx) / len(idx)})
    return rows


@dataclass
class BacktestReport:
    sport: str
    competition: str
    model_version: str
    n: int
    brier: float | None
    log_loss: float | None
    baseline_brier: float | None  # always predicting the historical home-win rate
    calibration: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        if not self.n:
            return f"Backtest {self.sport}/{self.competition}: no evaluable games (sample size 0)."
        lines = [
            f"Backtest {self.sport}/{self.competition} model {self.model_version}",
            f"  sample size: {self.n} games",
            f"  Brier score: {self.brier:.4f}  (baseline {self.baseline_brier:.4f}; lower is better)",
            f"  Log loss:    {self.log_loss:.4f}",
            "  Calibration (predicted vs observed home-win rate):",
        ]
        for r in self.calibration:
            lines.append(f"    {r['bin']}: n={r['n']:4d} pred={r['mean_pred']:.3f} obs={r['observed']:.3f}")
        lines += [f"  note: {n}" for n in self.notes]
        if self.n < 200:
            lines.append("  WARNING: small sample; treat these numbers as noisy.")
        return "\n".join(lines)


def walk_forward(rows: Sequence[dict], make_model: Callable[[], object], predict_home: Callable[[object, dict], float | None],
                 start: date, step_days: int = 7, min_train: int = 40, outcome: Callable[[dict], int | None] | None = None,
                 sport: str = "", competition: str = "", version: str = "") -> BacktestReport:
    """Generic walk-forward: refit on rows before each window, predict rows inside it."""
    rows = sorted(rows, key=lambda r: str(r["date"]))
    outcome = outcome or (lambda r: 1 if r["home_score"] > r["away_score"] else 0 if r["home_score"] < r["away_score"] else None)
    probs: list[float] = []
    outs: list[int] = []
    d = start
    end = date.fromisoformat(str(rows[-1]["date"])[:10]) if rows else start
    while d <= end:
        train = [r for r in rows if str(r["date"])[:10] < d.isoformat()]
        test = [r for r in rows if d.isoformat() <= str(r["date"])[:10] < (d + timedelta(days=step_days)).isoformat()]
        if len(train) >= min_train and test:
            model = make_model()
            model.fit(train, as_of=d)  # type: ignore[attr-defined]
            for r in test:
                o = outcome(r)
                p = predict_home(model, r)
                if o is None or p is None:
                    continue
                probs.append(p)
                outs.append(o)
        d += timedelta(days=step_days)
    base_rate = (sum(outs) / len(outs)) if outs else None
    return BacktestReport(
        sport, competition, version, len(probs), brier(probs, outs), log_loss(probs, outs),
        brier([base_rate] * len(outs), outs) if base_rate is not None else None,
        calibration_table(probs, outs),
        ["Draws/ties excluded from the home-win test set." if sport == "soccer" else "Ties excluded."],
    )


def summarize_predictions(rows: Iterable[dict]) -> dict:
    """Brier/log-loss of stored predictions vs the market-implied probability at the same time."""
    rows = [r for r in rows if r["outcome"] is not None]
    p = [r["model_prob"] for r in rows]
    o = [int(r["outcome"]) for r in rows]
    mk = [(r["market_price"], int(r["outcome"])) for r in rows if r["market_price"] is not None]
    return {
        "n": len(rows),
        "model_brier": brier(p, o), "model_log_loss": log_loss(p, o),
        "market_n": len(mk),
        "market_brier": brier([m for m, _ in mk], [x for _, x in mk]),
        "market_log_loss": log_loss([m for m, _ in mk], [x for _, x in mk]),
    }
