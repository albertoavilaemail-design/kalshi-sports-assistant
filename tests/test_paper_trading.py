from helpers import market, settings, storage

from kalshi_assistant.bankroll import Bankroll
from kalshi_assistant.paper_trading import PaperTrader, TradeRejected
from kalshi_assistant.positions import PositionStore
from kalshi_assistant.value_engine import evaluate_side, resize


def test_open_debits_cost_and_fees_and_records_prediction():
    s, st = settings(min_edge=0, min_net_ev_per_dollar=0), storage()
    t = PaperTrader(st, s)
    o = evaluate_side(market(yes_bid=0.38, yes_ask=0.40), "yes", 0.55, 1.0, s)
    resize(o, 2, s)
    r = t.open_from_opportunity(o, 2)
    assert abs(r.cost - (0.80 + 0.04)) < 1e-9  # fee(2, .40) = 0.0336 -> 0.04
    assert abs(Bankroll(st, s.risk).cash("paper") - (25 - 0.84)) < 1e-9
    assert st.q("SELECT COUNT(*) AS n FROM predictions")[0]["n"] == 1
    assert "max loss $0.84" in r.message


def test_duplicate_positions_are_rejected():
    s, st = settings(), storage()
    t = PaperTrader(st, s)
    t.open_position("paper", "T", "yes", 1, 0.5)
    try:
        t.open_position("paper", "T", "yes", 1, 0.5)
        raise AssertionError
    except TradeRejected as exc:
        assert "already exists" in str(exc)


def test_limits_enforced_on_paper_trades():
    s, st = settings(), storage()
    try:
        PaperTrader(st, s).open_position("paper", "T", "yes", 10, 0.5)
        raise AssertionError
    except TradeRejected as exc:
        assert "max position" in str(exc)


def test_sell_and_settle_accounting():
    s, st = settings(), storage()
    t = PaperTrader(st, s)
    br = Bankroll(st, s.risk)
    r = t.open_position("paper", "T", "yes", 2, 0.40)  # cost 0.80 + 0.04
    res = t.sell(r.position.id, 0.60)  # proceeds 1.20 - fee(2,.6)=0.04 -> 1.16
    assert abs(res.cost - 1.16) < 1e-9
    p = PositionStore(st).get(r.position.id)
    assert p.status == "closed" and abs(p.realized_pnl - 0.32) < 1e-9
    assert abs(br.cash("paper") - (25 + 0.32)) < 1e-9
    w = t.open_position("paper", "W", "no", 1, 0.30)
    assert abs(t.settle_position(w.position, "no") - (1 - 0.32)) < 1e-9
    lo = t.open_position("paper", "L", "yes", 1, 0.30)
    assert abs(t.settle_position(lo.position, "no") + 0.32) < 1e-9


def test_partial_sell_keeps_remainder_open():
    s, st = settings(), storage()
    s.risk.max_position_dollars = 5
    t = PaperTrader(st, s)
    r = t.open_position("paper", "T", "yes", 4, 0.25)
    t.sell(r.position.id, 0.50, contracts=1)
    rest = PositionStore(st).get(r.position.id)
    assert rest.status == "open" and rest.contracts == 3
    closed = [p for p in PositionStore(st).all() if p.status == "closed"]
    assert len(closed) == 1 and closed[0].contracts == 1


def test_performance_reports_losses_and_real_account_is_separate():
    s, st = settings(), storage()
    t = PaperTrader(st, s)
    a = t.open_position("paper", "A", "yes", 1, 0.5)
    t.settle_position(a.position, "no")
    rep = t.performance("paper")
    assert "Win rate (closed): 0.0%" in rep and "-0.52" in rep
    t.open_position("real", "R", "yes", 1, 0.5, enforce_limits=False)
    assert Bankroll(st, s.risk).cash("paper") == 25 - 0.52
    assert "No trades" in PaperTrader(storage(), s).performance("paper")


def test_validation_status_requires_sample_and_beating_market():
    from kalshi_assistant.paper_trading import validation_status
    st = storage()
    ok, msg = validation_status(st)
    assert not ok and "NOT YET VALIDATED" in msg
    for i in range(100):
        st.conn.execute("INSERT INTO predictions(ts,model_prob,market_price,outcome) VALUES(0,?,?,?)", (0.6, 0.5, i % 2))
    ok, msg = validation_status(st)
    assert not ok and "UNDERPERFORMING" in msg  # model 0.6 vs coin-flip outcomes is worse than market 0.5
