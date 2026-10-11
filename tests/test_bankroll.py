from helpers import settings, storage

from kalshi_assistant.bankroll import Bankroll
from kalshi_assistant.paper_trading import PaperTrader


def test_initial_bankroll_and_update():
    s, st = settings(), storage()
    br = Bankroll(st, s.risk)
    assert br.cash("paper") == 25.0
    assert br.cash("real") == 25.0
    br.set_bankroll("real", 40)
    assert br.cash("real") == 40.0
    br.set_reserved("real", 10)
    assert br.available("real") == 30.0
    assert br.cash("paper") == 25.0  # accounts are separate


def test_negative_bankroll_rejected():
    br = Bankroll(storage(), settings().risk)
    try:
        br.set_bankroll("real", -1)
        raise AssertionError
    except ValueError:
        pass


def test_suggested_size_respects_min_max_and_edge():
    s = settings()
    br = Bankroll(storage(), s.risk)
    n, _ = br.suggest_contracts("paper", prob=0.70, price=0.40, fee_per_contract=0.02)
    assert n >= 1 and n * 0.42 <= s.risk.max_position_dollars + 1e-9
    n0, why = br.suggest_contracts("paper", prob=0.40, price=0.45, fee_per_contract=0.02)
    assert n0 == 0 and "edge" in why
    # one contract more expensive than max position -> no trade
    s.risk.max_position_dollars = 0.5
    n1, why1 = br.suggest_contracts("paper", prob=0.95, price=0.80, fee_per_contract=0.02)
    assert n1 == 0


def test_limits_block_overexposure_and_correlated_risk():
    s, st = settings(), storage()
    br = Bankroll(st, s.risk)
    assert br.check_new_position("paper", "EV1", 0.8) == []
    assert any("max position" in p for p in br.check_new_position("paper", "EV1", 3.0))
    t = PaperTrader(st, s)
    t.open_position("paper", "T1", "yes", 1, 0.90, event_ticker="EV1")
    t.open_position("paper", "T2", "yes", 1, 0.90, event_ticker="EV1")
    probs = br.check_new_position("paper", "EV1", 0.5)
    assert any("correlated" in p for p in probs)
    assert br.correlated_positions("paper", "EV1")


def test_daily_loss_limit_blocks_new_trades_and_sizes_never_increase_after_losses():
    s, st = settings(), storage()
    s.risk.daily_loss_limit_dollars = 1.0
    br = Bankroll(st, s.risk)
    t = PaperTrader(st, s)
    before, _ = br.suggest_contracts("paper", 0.8, 0.2, 0.01)
    for i in range(2):
        r = t.open_position("paper", f"L{i}", "yes", 1, 0.90, event_ticker=f"E{i}")
        t.settle_position(r.position, "no")
    assert any("daily loss" in p for p in br.check_new_position("paper", "E9", 0.5))
    after, _ = br.suggest_contracts("paper", 0.8, 0.2, 0.01)
    assert after <= before


def test_snapshot_render_contains_key_fields():
    s, st = settings(), storage()
    txt = Bankroll(st, s.risk).snapshot("paper").render()
    for k in ("Cash balance", "Available", "Open exposure", "Realized P/L", "Today P/L"):
        assert k in txt
