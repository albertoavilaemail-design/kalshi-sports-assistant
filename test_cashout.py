from helpers import market, settings

from kalshi_assistant.cashout import HOLD, NO_ACTION, REDUCE, SELL, UNAVAILABLE, analyze_exit
from kalshi_assistant.kalshi_client import parse_orderbook
from kalshi_assistant.positions import Position


def pos(side="yes", n=2, price=0.50, fees=0.02):
    return Position(id="p1", mode="paper", market_ticker="MOCK-TICKER-1", event_ticker="MOCK-EVENT-1",
                    series_ticker=None, sport="nfl", competition="nfl", market_type="winner", contract="MOCK",
                    side=side, contracts=n, entry_price=price, entry_fees=fees, opened_at=0)


def test_sell_when_bid_exceeds_estimated_probability():
    s = settings()
    mk = market(yes_bid=0.80, yes_ask=0.82)
    e = analyze_exit(pos(), mk, prob_yes=0.60, settings=s)
    assert e.decision == SELL
    # proceeds: 2*0.80 - fee(2,0.80)= 1.60-0.03 = 1.57 ; realized = 1.57 - (1.00+0.02)
    assert abs(e.net_proceeds - 1.57) < 1e-9
    assert abs(e.realized_if_sold - 0.55) < 1e-9


def test_hold_when_estimate_beats_bid_even_if_position_is_losing():
    s = settings()
    mk = market(yes_bid=0.30, yes_ask=0.33)  # bought at 0.50 -> losing
    e = analyze_exit(pos(), mk, prob_yes=0.45, settings=s)
    assert e.decision == HOLD
    assert e.realized_if_sold < 0


def test_no_action_when_values_are_close():
    e = analyze_exit(pos(n=1), market(yes_bid=0.60, yes_ask=0.62), prob_yes=0.585, settings=settings())
    assert e.decision == NO_ACTION


def test_no_side_position_uses_no_bids_from_orderbook():
    s = settings()
    ob = parse_orderbook("MOCK", {"orderbook_fp": {"yes_dollars": [["0.20", "50"]], "no_dollars": [["0.70", "50"]]}})
    e = analyze_exit(pos(side="no"), market(yes_bid=0.20, yes_ask=0.30), prob_yes=0.40, settings=s, orderbook=ob)
    assert e.sell_price == 0.70
    assert e.decision == SELL  # NO worth 0.60 by estimate, bid 0.70


def test_partial_liquidity_suggests_reduce_and_explains():
    s = settings()
    ob = parse_orderbook("MOCK", {"orderbook_fp": {"yes_dollars": [["0.80", "1"]], "no_dollars": []}})
    e = analyze_exit(pos(n=3), market(yes_bid=0.80, yes_ask=0.85), 0.50, s, ob)
    assert e.decision == REDUCE and e.sellable_contracts == 1
    assert "Only 1 of 3" in e.liquidity_note


def test_no_buyers_means_hold_and_missing_data_means_unavailable():
    s = settings()
    ob = parse_orderbook("MOCK", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    e = analyze_exit(pos(), market(yes_bid=None, yes_ask=0.5), 0.5, s, ob)
    assert e.decision == HOLD and "cannot be sold" in e.liquidity_note
    assert analyze_exit(pos(), market(), None, s).decision == UNAVAILABLE
    assert analyze_exit(pos(), None, 0.5, s).decision == UNAVAILABLE
    assert analyze_exit(pos(), market(age=9999), 0.5, s).decision == UNAVAILABLE


def test_risk_limit_turns_hold_into_reduce():
    e = analyze_exit(pos(n=4), market(yes_bid=0.40, yes_ask=0.42), 0.60, settings(), over_risk_limit=True)
    assert e.decision == REDUCE
    assert "REDUCE" in e.render() or "Reduce" in e.render() or "REDUCE EXPOSURE" in e.render()
