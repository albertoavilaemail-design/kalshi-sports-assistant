from helpers import game, settings, synthetic_football_results, synthetic_soccer_results, team

from kalshi_assistant.football_model import football_prob
from kalshi_assistant.halftime import is_halftime
from kalshi_assistant.live_model import football_live_dists, football_window, soccer_live_matrix, soccer_window
from kalshi_assistant.market_discovery import ContractSpec
from kalshi_assistant.nfl_model import NFLModel
from kalshi_assistant.soccer_model import SoccerModel, soccer_prob

WIN = ContractSpec("winner", team_text="x")


def _nfl():
    s = settings()
    return s, NFLModel(s.model).fit(synthetic_football_results())


def test_halftime_detection():
    assert is_halftime(game(status="halftime"))
    assert is_halftime(game(status="in_progress", period=2, clock=0.0))
    assert not is_halftime(game(status="in_progress", period=2, clock=120.0))
    assert not is_halftime(game(status="in_progress", period=3, clock=900.0, hp=[7, 7, 0], ap=[0, 7, 0]))


def test_football_window_at_halftime():
    s, _ = _nfl()
    ws = football_window(game(status="halftime"), "full", s.model)
    assert ws.banked_home == 14 and ws.banked_away == 7
    assert abs(ws.remaining_share - (1 - s.model.first_half_share)) < 1e-9
    w1 = football_window(game(status="halftime"), "1H", s.model)
    assert w1.finished and w1.remaining_share == 0
    w2 = football_window(game(status="halftime"), "2H", s.model)
    assert w2.banked_home == 0 and w2.remaining_share > 0


def test_lead_raises_win_probability_but_does_not_make_it_certain():
    s, m = _nfl()
    pred = m.predict("Team3", "Team4")
    pre = football_prob(WIN, "home", m.pregame_dists(pred, "full"))
    d = football_live_dists(pred, game(status="halftime", hs=14, as_=7), "full", s.model, s.live)
    live = football_prob(WIN, "home", d)
    assert live > pre and live < 0.95


def test_trailing_favourite_is_repriced_not_assumed_value():
    s, m = _nfl()
    pred = m.predict("Team7", "Team0")  # strong home favourite
    d = football_live_dists(pred, game(status="halftime", hs=0, as_=21, hp=[0, 0], ap=[14, 7]), "full", s.model, s.live)
    p = football_prob(WIN, "home", d)
    pre = football_prob(WIN, "home", m.pregame_dists(pred, "full"))
    assert p < pre  # the model updates downwards; whether it's value depends on price


def test_final_seconds_are_nearly_deterministic():
    s, m = _nfl()
    pred = m.predict("Team3", "Team4")
    g = game(status="in_progress", period=4, clock=5.0, hs=24, as_=10, hp=[7, 7, 3, 7], ap=[3, 0, 7, 0])
    assert football_prob(WIN, "home", football_live_dists(pred, g, "full", s.model, s.live)) > 0.99


def test_missing_clock_returns_data_unavailable():
    s, m = _nfl()
    g = game(status="in_progress", period=3, clock=None)
    assert football_live_dists(m.predict("Team1", "Team2"), g, "full", s.model, s.live) is None


def test_soccer_halftime_and_red_card():
    s = settings()
    sm = SoccerModel(s.model).fit(synthetic_soccer_results())
    pred = sm.predict("Club5", "Club4")
    h, a = team("Club5"), team("Club4")
    g = game(sport="soccer", status="halftime", period=1, clock=2700, hs=1, as_=1, hp=[1], ap=[1], home=h, away=a)
    ws = soccer_window(g, "full", s.model, s.live)
    assert ws.banked_home == 1 and abs(ws.remaining_share - (1 - s.model.soccer_first_half_share)) < 1e-9
    m0 = soccer_live_matrix(pred, g, "full", s.model, s.live)
    over = soccer_prob(ContractSpec("total", line=2.5, direction="over"), None, m0)
    assert 0.3 < over < 0.95  # already 2 goals, needs one more: neither guaranteed nor ruled out
    g_red = game(sport="soccer", status="halftime", period=1, clock=2700, hs=1, as_=1, hp=[1], ap=[1], home=h, away=a, reds=(1, 0))
    m1 = soccer_live_matrix(pred, g_red, "full", s.model, s.live)
    assert soccer_prob(WIN, "home", m1) < soccer_prob(WIN, "home", m0)
    # first-half markets are settled at halftime
    m_1h = soccer_live_matrix(pred, g, "1H", s.model, s.live)
    assert soccer_prob(ContractSpec("draw", period="1H"), None, m_1h) > 0.999


def test_render_three_independent_decisions():
    from kalshi_assistant.cashout import analyze_exit
    from kalshi_assistant.live_decisions import LiveGameReport, Momentum, render_live_report
    from kalshi_assistant.positions import Position
    from helpers import market
    p = Position(id="p1", mode="paper", market_ticker="MOCK-TICKER-1", event_ticker="E", series_ticker=None,
                 sport="nfl", competition="nfl", market_type="winner", contract="MOCK", side="yes", contracts=1,
                 entry_price=0.5, entry_fees=0.02, opened_at=0)
    e = analyze_exit(p, market(yes_bid=0.8, yes_ask=0.82), 0.6, settings())
    r = LiveGameReport(game(status="halftime"), [], [], [], [e], Momentum(10), 0.0, [])
    txt = render_live_report(r, "HALFTIME DECISIONS")
    assert "1) ENTER A NEW POSITION?" in txt and "No qualifying trades found." in txt
    assert "2) KEEP HOLDING" in txt and "3) SELL EXISTING" in txt and "SELL / EXIT" in txt
