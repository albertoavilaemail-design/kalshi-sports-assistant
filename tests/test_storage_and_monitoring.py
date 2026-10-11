import time

from helpers import game, settings, storage

from kalshi_assistant.live_decisions import Momentum, measure_momentum


def test_lock_prevents_overlapping_runs():
    st = storage()
    h = st.acquire_lock("monitor", ttl=60)
    assert h and st.acquire_lock("monitor", ttl=60) is None
    st.release_lock("monitor", h)
    assert st.acquire_lock("monitor", ttl=60)


def test_expired_lock_can_be_taken_over():
    st = storage()
    assert st.acquire_lock("m", ttl=-1)
    assert st.acquire_lock("m", ttl=60)


def test_cache_and_watchlist():
    st = storage()
    st.cache_set("x", [1, 2])
    assert st.cache_get("x", 60) == [1, 2]
    assert st.cache_get("x", -1) is None
    st.watch("Ohio State")
    assert st.watchlist() == ["Ohio State"]
    st.unwatch("Ohio State")
    assert st.watchlist() == []


class _A:  # minimal analyzer stand-in for momentum tests (TEST ONLY)
    def __init__(self, st):
        self.storage = st
        self.settings = settings()


def test_momentum_measures_runs_and_flags_overreaction():
    st = storage()
    now = time.time()
    st.conn.execute("INSERT INTO snapshots(ts,market_ticker,game_id,yes_bid,yes_ask,model_prob,home_score,away_score) VALUES(?,?,?,?,?,?,?,?)",
                    (now - 590, "T", "G", 0.40, 0.42, 0.45, 7, 7))
    st.conn.execute("INSERT INTO snapshots(ts,market_ticker,game_id,yes_bid,yes_ask,model_prob,home_score,away_score) VALUES(?,?,?,?,?,?,?,?)",
                    (now - 10, "T", "G", 0.70, 0.72, 0.58, 21, 7))
    m = measure_momentum(_A(st), game(), ["T"])
    assert m.available and m.run == 14
    assert abs(m.price_moves["T"] - 0.30) < 1e-9 and abs(m.model_moves["T"] - 0.13) < 1e-9
    assert any("OVER-reacting" in n for n in m.overreaction_notes())
    assert "Buffalo Bills on a 14-0 run" in m.describe(game())


def test_momentum_weight_defaults_to_zero():
    from kalshi_assistant.live_decisions import momentum_adjustment
    m = Momentum(10, 14, 0, available=True)
    assert momentum_adjustment(_A(storage()), game(), m) == 0.0
    a = _A(storage())
    a.settings.live.momentum_weight = 0.1
    assert abs(momentum_adjustment(a, game(), m) - 1.4) < 1e-9


def test_execution_is_disabled():
    from kalshi_assistant.execution import ExecutionDisabled, place_order, preflight
    try:
        place_order()
        raise AssertionError
    except ExecutionDisabled:
        pass
    assert "kill switch is ON" in preflight(settings(), authenticated=False)
