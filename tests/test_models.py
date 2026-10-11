import math
from datetime import date

import numpy as np
from helpers import settings, synthetic_football_results, synthetic_soccer_results

from kalshi_assistant.calibration import brier, calibration_table, log_loss, walk_forward
from kalshi_assistant.college_football_model import CollegeFootballModel
from kalshi_assistant.football_model import Normal, football_prob
from kalshi_assistant.market_discovery import ContractSpec
from kalshi_assistant.nfl_model import NFLModel
from kalshi_assistant.soccer_model import SoccerModel, score_matrix, soccer_prob


def test_normal_continuity_and_complements():
    n = Normal(0, 10)
    assert math.isclose(n.p_gt(44.5) + n.p_lt(44.5), 1.0, abs_tol=1e-9)
    # integer line: strictly-over 44 and under 44 leave P(=44) out
    assert n.p_gt(44) + n.p_lt(44) + n.p_eq(44) - 1 < 1e-9
    assert Normal(50, 0).p_gt(44.5) == 1.0


def test_nfl_ratings_recover_strength_order():
    s = settings()
    m = NFLModel(s.model).fit(synthetic_football_results())
    r = [m.rating[f"team{i}"] for i in range(8)]
    assert r[7] > r[4] > r[0]
    pred = m.predict("Team7", "Team0")
    assert pred.win_prob_home() > 0.75
    assert pred.reliability > 0.5


def test_football_market_probabilities_are_consistent():
    s = settings()
    m = NFLModel(s.model).fit(synthetic_football_results())
    pred = m.predict("Team5", "Team2")
    d = m.pregame_dists(pred, "full")
    win = football_prob(ContractSpec("winner", team_text="x"), "home", d)
    lose = football_prob(ContractSpec("winner", team_text="x"), "away", d)
    assert math.isclose(win + lose, 1.0, abs_tol=1e-6)
    s3 = football_prob(ContractSpec("spread", team_text="x", line=3.5), "home", d)
    s10 = football_prob(ContractSpec("spread", team_text="x", line=10.5), "home", d)
    assert win > s3 > s10
    over = football_prob(ContractSpec("total", line=40.5, direction="over"), None, d)
    under = football_prob(ContractSpec("total", line=40.5, direction="under"), None, d)
    assert math.isclose(over + under, 1.0, abs_tol=1e-6)
    h1 = m.pregame_dists(pred, "1H")
    assert h1.total.mu < d.total.mu and h1.total.sd < d.total.sd
    assert football_prob(ContractSpec("winner", team_text="x"), None, d) is None  # unresolved team -> no guess


def test_college_unknown_team_prior_and_reliability():
    s = settings()
    m = CollegeFootballModel(s.model).fit(synthetic_football_results())
    p = m.predict("Team3", "Tiny FCS College")
    assert p.mu_margin > 5  # unknown opponent starts below average (documented assumption)
    assert p.reliability == 0.0
    assert any("No historical games" in n for n in p.notes)


def test_soccer_matrix_and_market_probs():
    m = score_matrix(1.6, 1.1, -0.05)
    assert math.isclose(m.sum(), 1.0, abs_tol=1e-9)
    home = soccer_prob(ContractSpec("winner", team_text="h"), "home", m)
    away = soccer_prob(ContractSpec("winner", team_text="a"), "away", m)
    draw = soccer_prob(ContractSpec("draw"), None, m)
    assert math.isclose(home + away + draw, 1.0, abs_tol=2e-3)
    o25 = soccer_prob(ContractSpec("total", line=2.5, direction="over"), None, m)
    u25 = soccer_prob(ContractSpec("total", line=2.5, direction="under"), None, m)
    assert math.isclose(o25 + u25, 1.0, abs_tol=2e-3)
    o35 = soccer_prob(ContractSpec("total", line=3.5, direction="over"), None, m)
    assert o35 < o25
    btts = soccer_prob(ContractSpec("btts"), None, m)
    assert 0.3 < btts < 0.7


def test_strong_favourite_does_not_imply_many_goals():
    # dominant favourite with a low total: high win prob, but over 2.5 still below 50%
    m = score_matrix(1.7, 0.4)
    assert soccer_prob(ContractSpec("winner", team_text="h"), "home", m) > 0.6
    assert soccer_prob(ContractSpec("total", line=2.5, direction="over"), None, m) < 0.5


def test_soccer_fit_orders_teams():
    s = settings()
    sm = SoccerModel(s.model).fit(synthetic_soccer_results())
    strong, weak = sm.predict("Club9", "Club0"), sm.predict("Club0", "Club9")
    assert strong.lam_home > weak.lam_home
    pr = strong.outcome_probs(s.model.soccer_rho)
    assert math.isclose(sum(pr.values()), 1.0, abs_tol=1e-6) and pr["home"] > pr["away"]


def test_scores_and_calibration():
    assert brier([1.0, 0.0], [1, 0]) == 0.0
    assert math.isclose(brier([0.5, 0.5], [1, 0]), 0.25)
    assert log_loss([0.5], [1]) > 0.69
    rows = calibration_table([0.1, 0.15, 0.9], [0, 0, 1], bins=10)
    assert sum(r["n"] for r in rows) == 3
    assert brier([], []) is None


def test_walk_forward_backtest_runs_without_lookahead():
    s = settings()
    rows = synthetic_football_results(n_weeks=18)
    rep = walk_forward(rows, lambda: NFLModel(s.model), lambda m, r: m.predict(r["home"], r["away"]).win_prob_home(),
                       date(2025, 10, 6), min_train=20, sport="nfl", competition="nfl", version="test")
    assert rep.n > 20
    assert rep.brier < rep.baseline_brier  # synthetic data has real signal
    assert "sample size" in rep.render()


def test_matrix_shift_preserves_mass():
    from kalshi_assistant.soccer_model import shift_matrix
    m = shift_matrix(score_matrix(0.5, 0.5), 2, 1)
    assert math.isclose(m.sum(), 1.0, abs_tol=1e-9)
    assert np.all(m[:2, :] == 0) and np.all(m[:, :1] == 0)
