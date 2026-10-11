"""Kalshi client + market discovery tests. All payloads below are MOCK data shaped like
the documented API responses (https://docs.kalshi.com/)."""
import base64

from helpers import FakeResponse, FakeSession, settings

from kalshi_assistant.config import KalshiConfig
from kalshi_assistant.kalshi_client import KalshiAuthError, KalshiClient, KalshiUnavailable, parse_orderbook
from kalshi_assistant.market_discovery import classify_series, parse_market
from kalshi_assistant.sports_data import ESPNProvider, find_game, match_side


def client(responses, **kw):
    cfg = KalshiConfig(base_url="https://api.elections.kalshi.com/trade-api/v2", max_retries=2,
                       min_request_interval_seconds=0, **kw)
    return KalshiClient(cfg, session=FakeSession(responses))


def test_orderbook_parsing_fixed_point_and_legacy():
    ob = parse_orderbook("T", {"orderbook_fp": {"yes_dollars": [["0.40", "10.00"], ["0.42", "5.00"]],
                                                "no_dollars": [["0.55", "7.00"]]}})
    assert ob.best_yes_bid == (0.42, 5.0)
    assert ob.best_yes_ask == (0.45, 7.0)  # 1 - best NO bid
    assert ob.best_no_ask == (0.58, 5.0)
    filled, gross = ob.sweep_sell("yes", 8)
    assert filled == 8 and abs(gross - (5 * 0.42 + 3 * 0.40)) < 1e-9
    legacy = parse_orderbook("T", {"orderbook": {"yes": [[40, 10]], "no": [[55, 7]]}})
    assert legacy.best_yes_bid == (0.40, 10.0)


def test_pagination_follows_cursor():
    c = client([FakeResponse(200, {"markets": [{"ticker": "A"}], "cursor": "abc"}),
                FakeResponse(200, {"markets": [{"ticker": "B"}], "cursor": ""})])
    assert [m["ticker"] for m in c.get_markets(series_ticker="KXX")] == ["A", "B"]
    assert c.session.calls[1][2]["cursor"] == "abc"


def test_retries_on_rate_limit_then_succeeds():
    import kalshi_assistant.kalshi_client as kc
    kc.time.sleep = lambda s: None  # don't actually wait in tests
    c = client([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(500), FakeResponse(200, {"market": {"ticker": "X"}})])
    assert c.get_market("X")["ticker"] == "X"
    c2 = client([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
    try:
        c2.get_market("X")
        raise AssertionError
    except KalshiUnavailable:
        pass


def test_auth_errors_and_missing_credentials():
    c = client([FakeResponse(401)])
    try:
        c.request("GET", "/markets/X")
        raise AssertionError
    except KalshiAuthError:
        pass
    try:
        client([]).get_balance()
        raise AssertionError
    except KalshiAuthError:
        pass


def test_request_signing_rsa_and_ed25519_verify():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
    for key in (rsa.generate_private_key(public_exponent=65537, key_size=2048), ed25519.Ed25519PrivateKey.generate()):
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        c = client([], api_key_id="MOCK-KEY", private_key_pem=pem)
        h = c.sign_headers("GET", "/trade-api/v2/portfolio/balance?limit=5", ts_ms=1700000000000)
        assert h["KALSHI-ACCESS-KEY"] == "MOCK-KEY" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
        msg = b"1700000000000GET/trade-api/v2/portfolio/balance"  # query string stripped
        sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
        if isinstance(key, rsa.RSAPrivateKey):
            key.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                          salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        else:
            key.public_key().verify(sig, msg)


def test_series_classification():
    s = settings()
    nfl = classify_series({"ticker": "KXNFLGAME", "title": "Pro Football Game", "tags": ["Football"]}, s)
    assert (nfl.sport, nfl.market_type, nfl.period) == ("nfl", "winner", "full")
    cfb = classify_series({"ticker": "KXNCAAFTOTAL", "title": "College Football Total Points"}, s)
    assert (cfb.sport, cfb.market_type) == ("ncaaf", "total")
    epl = classify_series({"ticker": "KXEPLBTTS", "title": "Premier League Both Teams to Score"}, s)
    assert (epl.sport, epl.competition, epl.market_type) == ("soccer", "epl", "btts")
    h1 = classify_series({"ticker": "KXNFL1HTOTAL", "title": "Pro Football 1st Half Total"}, s)
    assert h1.period == "1H"
    assert classify_series({"ticker": "KXSB", "title": "Super Bowl Champion"}, s) is None  # futures skipped
    assert classify_series({"ticker": "KXNBAGAME", "title": "Pro Basketball Game"}, s) is None
    s.sports.series_overrides = {"KXODD": {"sport": "nfl", "market_type": "spread"}}
    assert classify_series({"ticker": "KXODD", "title": "?"}, s).market_type == "spread"


def test_parse_markets_into_contract_specs():
    s = settings()
    win = classify_series({"ticker": "KXEPLGAME", "title": "Premier League Game"}, s)
    ev = {"title": "Arsenal vs Chelsea", "event_ticker": "KXEPLGAME-MOCK"}
    m = parse_market({"ticker": "A", "yes_sub_title": "Arsenal", "yes_bid_dollars": "0.5400", "yes_ask_dollars": "0.5600",
                      "no_bid_dollars": "0.4400", "no_ask_dollars": "0.4600", "status": "active",
                      "yes_ask_size_fp": "120.00"}, win, ev)
    assert m.spec.kind == "winner" and m.spec.team_text == "Arsenal"
    assert m.yes_ask == 0.56 and m.spread == 0.02 and m.yes_ask_size == 120.0 and m.is_tradable
    d = parse_market({"ticker": "D", "yes_sub_title": "Tie"}, win, ev)
    assert d.spec.kind == "draw"
    tot = classify_series({"ticker": "KXEPLTOTAL", "title": "Premier League Total Goals"}, s)
    t = parse_market({"ticker": "T", "floor_strike": 2.5, "strike_type": "greater", "yes_bid": 40, "yes_ask": 43}, tot, ev)
    assert (t.spec.kind, t.spec.line, t.spec.direction) == ("total", 2.5, "over") and t.yes_bid == 0.40
    sp = classify_series({"ticker": "KXNFLSPREAD", "title": "Pro Football Spread"}, s)
    x = parse_market({"ticker": "S", "yes_sub_title": "Buffalo wins by over 3.5 points", "floor_strike": 3.5}, sp, ev)
    assert (x.spec.kind, x.spec.team_text, x.spec.line) == ("spread", "Buffalo", 3.5)
    none = parse_market({"ticker": "N", "yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000"}, tot, ev)
    assert none.spec is None and none.yes_bid is None and none.yes_ask is None  # never guessed


MOCK_ESPN_EVENT = {  # MOCK, shaped like ESPN's public scoreboard JSON
    "id": "401", "date": "2026-10-11T17:00Z",
    "status": {"clock": 0, "displayClock": "0:00", "period": 2, "type": {"name": "STATUS_HALFTIME", "state": "in"}},
    "competitions": [{
        "neutralSite": False, "venue": {"fullName": "MOCK Stadium", "address": {"city": "Orchard Park", "state": "NY"}, "indoor": False},
        "situation": {"possession": "1"},
        "competitors": [
            {"homeAway": "home", "score": "17", "linescores": [{"value": 7}, {"value": 10}],
             "team": {"id": "1", "displayName": "Buffalo Bills", "abbreviation": "BUF", "shortDisplayName": "Bills", "name": "Bills", "location": "Buffalo"}},
            {"homeAway": "away", "score": "10", "linescores": [{"value": 3}, {"value": 7}],
             "team": {"id": "2", "displayName": "New York Jets", "abbreviation": "NYJ", "shortDisplayName": "Jets", "name": "Jets", "location": "New York"}},
        ]}],
}


def test_espn_event_parsing_and_matching():
    g = ESPNProvider(session=FakeSession([])).parse_event(MOCK_ESPN_EVENT, "nfl", "nfl")
    assert g.status == "halftime" and g.home_score == 17 and g.away_period_scores == [3, 7]
    assert g.possession == "home" and g.venue["city"] == "Orchard Park"
    assert match_side("Buffalo", g) == "home"
    assert match_side("NY Jets", g) == "away" or match_side("New York Jets", g) == "away"
    assert match_side("Dallas", g) is None  # unknown team -> no guess
    assert find_game("New York Jets at Buffalo Bills", [g]) is g
    assert find_game("Dallas at Houston", [g]) is None


def test_espn_logo_is_captured():
    ev = {**MOCK_ESPN_EVENT}
    comp = dict(ev["competitions"][0])
    c0 = dict(comp["competitors"][0]); c0["team"] = {**c0["team"], "logo": "https://example.invalid/mock-logo.png"}
    comp["competitors"] = [c0, comp["competitors"][1]]
    ev["competitions"] = [comp]
    g = ESPNProvider(session=FakeSession([])).parse_event(ev, "nfl", "nfl")
    assert g.home.logo.endswith("mock-logo.png") and g.away.logo == ""
