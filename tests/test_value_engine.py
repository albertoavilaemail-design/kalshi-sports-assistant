import math

from helpers import market, settings

from kalshi_assistant.fees import FeeModel, quadratic_fee
from kalshi_assistant.market_discovery import ContractSpec
from kalshi_assistant.value_engine import NO_TRADES, blend, evaluate_market, evaluate_side, rank, resize


def close(a, b, tol=1e-9):
    return math.isclose(a, b, abs_tol=tol)


def test_fee_matches_kalshi_schedule_examples():
    # Kalshi fee schedule: 100 contracts @ $0.50 -> $1.75 ; 1 @ $0.50 -> 0.0175 rounds up to $0.02
    assert close(quadratic_fee(100, 0.50), 1.75)
    assert close(quadratic_fee(1, 0.50), 0.02)
    assert close(quadratic_fee(100, 0.01), 0.07)
    assert close(quadratic_fee(100, 0.99), 0.07)
    assert close(quadratic_fee(1, 0.10), quadratic_fee(1, 0.90))  # symmetric
    assert quadratic_fee(0, 0.5) == 0.0


def test_fee_rejects_invalid_price():
    for bad in (0.0, 1.0, -0.2, 1.5):
        try:
            quadratic_fee(1, bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_fee_model_from_series_multiplier_and_maker():
    fm = FeeModel.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1})
    assert close(fm.taker_fee(100, 0.5), 1.75)
    assert close(fm.maker_fee(100, 0.5), 0.44)  # 0.0175*100*.25 = 0.4375 -> 0.44
    assert FeeModel.from_series({"fee_type": "quadratic"}).maker_fee(100, 0.5) == 0.0


def test_ev_math_after_fees():
    s = settings(min_edge=0.0, min_net_ev_per_dollar=0.0)
    mk = market(yes_bid=0.58, yes_ask=0.60)
    o = evaluate_side(mk, "yes", 0.70, 1.0, s)
    assert close(o.gross_ev_per_contract, 0.10)
    assert close(o.fee_total, 0.02)  # 0.07*.6*.4 = 0.0168 -> 0.02
    assert close(o.net_ev_per_contract(), 0.08)
    assert close(o.max_loss, 0.62)
    assert close(o.profit_if_win, 0.38)
    assert o.qualifies


def test_no_side_uses_complement_probability_and_yes_bid_depth():
    s = settings(min_edge=0.0, min_net_ev_per_dollar=0.0)
    mk = market(yes_bid=0.70, yes_ask=0.72, bid_size=3, ask_size=50)
    o = evaluate_side(mk, "no", 0.60, 1.0, s)
    assert close(o.price, 0.30)
    assert close(o.prob, 0.40)
    assert o.depth == 3  # buying NO consumes YES bids
    assert any("contracts at the ask" in r for r in o.fail_reasons)


def test_high_probability_alone_does_not_qualify():
    s = settings()
    mk = market(yes_bid=0.93, yes_ask=0.95)
    o = evaluate_side(mk, "yes", 0.96, 1.0, s)
    assert not o.qualifies
    assert any("edge" in r for r in o.fail_reasons)


def test_stale_wide_closed_and_unreliable_markets_fail():
    s = settings(min_edge=0.0, min_net_ev_per_dollar=0.0)
    assert any("stale" in r for r in evaluate_side(market(age=999), "yes", 0.9, 1.0, s).fail_reasons)
    assert any("spread" in r for r in evaluate_side(market(yes_bid=0.30, yes_ask=0.50), "yes", 0.9, 1.0, s).fail_reasons)
    assert any("status" in r for r in evaluate_side(market(status="closed"), "yes", 0.9, 1.0, s).fail_reasons)
    assert any("reliability" in r for r in evaluate_side(market(), "yes", 0.9, 0.1, s).fail_reasons)


def test_missing_ask_returns_none_and_blend_needs_two_sided_quote():
    s = settings()
    assert evaluate_side(market(yes_ask=None), "yes", 0.9, 1.0, s) is None
    p, note = blend(0.8, market(yes_bid=0.4, yes_ask=0.5), 0.5)
    assert close(p, 0.625)
    p2, note2 = blend(0.8, market(yes_bid=None), 0.5)
    assert p2 == 0.8 and note2


def test_resize_recomputes_fees_and_rank_orders_by_score():
    s = settings(min_edge=0.0, min_net_ev_per_dollar=0.0)
    o = evaluate_side(market(yes_bid=0.48, yes_ask=0.50), "yes", 0.65, 1.0, s)
    resize(o, 100, s)
    assert close(o.fee_total, 1.75)
    a = evaluate_market(market(yes_bid=0.48, yes_ask=0.50, ticker="A"), 0.70, 1.0, s)
    b = evaluate_market(market(yes_bid=0.48, yes_ask=0.50, ticker="B"), 0.60, 1.0, s)
    ranked = rank(a + b)
    assert ranked[0].market.ticker == "A" and ranked[0].side == "yes"
    assert NO_TRADES == "No qualifying trades found."


def test_over_under_label_and_spec_description():
    spec = ContractSpec("total", line=44.5, direction="over")
    o = evaluate_side(market(spec=spec), "no", 0.5, 1.0, settings())
    assert o.contract_label == "NOT (Total > 44.5)"


def test_huge_model_market_gap_never_qualifies():
    s = settings(min_edge=0.0, min_net_ev_per_dollar=0.0)
    s.value.max_model_market_gap = 0.15
    o = evaluate_side(market(yes_bid=0.07, yes_ask=0.08), "yes", 0.41, 1.0, s)
    assert not o.qualifies and any("missing information" in r for r in o.fail_reasons)
    ok = evaluate_side(market(yes_bid=0.48, yes_ask=0.50), "yes", 0.60, 1.0, s)
    assert ok.qualifies
