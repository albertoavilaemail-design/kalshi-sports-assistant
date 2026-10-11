"""Price-movement check and injury filter. MOCK data only."""
import time

from helpers import game, market, settings, storage

from kalshi_assistant.analysis import MarketAnalysis
from kalshi_assistant.model_check import apply_injury_filter, price_check, record_picks
from kalshi_assistant.value_engine import evaluate_side


class _Prov:
    def __init__(self, injuries):
        self.injuries = injuries

    def game_detail(self, *a):
        return {"available": True, "source": "MOCK", "injuries": self.injuries}


class _An:
    def __init__(self, injuries):
        self.provider = _Prov(injuries)


def _pair(yes_bid=0.48, yes_ask=0.50, status="scheduled"):
    s = settings(min_edge=0, min_net_ev_per_dollar=0)
    mk = market(yes_bid=yes_bid, yes_ask=yes_ask)
    g = game(status=status)
    a = MarketAnalysis(mk, g, "OK", context="pregame" if status == "scheduled" else "live", p_yes=0.6)
    return evaluate_side(mk, "yes", 0.6, 1.0, s), a


def test_qb_out_removes_pick_but_other_injuries_do_not():
    o, a = _pair()
    assert o.qualifies
    assert apply_injury_filter(_An([{"team": "Bills", "player": "Mock QB", "position": "QB", "status": "Out"}]), [(o, a)]) == 1
    assert not o.qualifies and "quarterback" in o.fail_reasons[-1]
    o2, a2 = _pair()
    assert apply_injury_filter(_An([{"team": "Bills", "player": "Mock WR", "position": "WR", "status": "Out"},
                                    {"team": "Bills", "player": "QB2", "position": "QB", "status": "Questionable"}]), [(o2, a2)]) == 0
    assert o2.qualifies


def test_price_check_tracks_moves_until_kickoff():
    st = storage()
    assert "no picks" in price_check(st).verdict
    for i in range(30):
        o, a = _pair()
        o.market.ticker = a.market.ticker = f"T{i}"
        record_picks(st, [(o, a)])
    st.conn.execute("UPDATE picks SET first_ts = first_ts - 7200")
    for i in range(30):  # market moves 4c toward our YES side
        o, a = _pair(yes_bid=0.52, yes_ask=0.54)
        o.market.ticker = a.market.ticker = f"T{i}"
        record_picks(st, [(o, a)])
    pc = price_check(st)
    assert pc.n == 30 and abs(pc.avg_move - 0.04) < 1e-9 and "PROMISING" in pc.verdict
    o, a = _pair(yes_bid=0.10, yes_ask=0.12, status="in_progress")  # after kickoff: frozen
    o.market.ticker = a.market.ticker = "T0"
    record_picks(st, [(o, a)])
    o, a = _pair(yes_bid=0.10, yes_ask=0.12, status="in_progress")
    o.market.ticker = a.market.ticker = "T0"
    record_picks(st, [(o, a)])
    assert st.q("SELECT last_mid, started FROM picks WHERE market_ticker='T0'")[0]["started"] == 1
    assert abs(st.q("SELECT last_mid FROM picks WHERE market_ticker='T0'")[0]["last_mid"] - 0.53) < 1e-9
