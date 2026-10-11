"""TEST-ONLY MOCK DATA. Nothing in this file is real market or sports data."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from kalshi_assistant.config import Settings
from kalshi_assistant.fees import FeeModel
from kalshi_assistant.market_discovery import ContractSpec, SportsMarket
from kalshi_assistant.sports_data import GameState, TeamRef
from kalshi_assistant.storage import Storage


def settings(**value_overrides) -> Settings:
    s = Settings()
    s.db_path = ":memory:"
    s.value.model_weight = 1.0  # tests check raw math unless they opt in
    s.value.max_model_market_gap = 1.0  # guard tested separately
    s.value.min_price, s.value.max_price = 0.01, 0.99
    for k, v in value_overrides.items():
        setattr(s.value, k, v)
    return s


def storage() -> Storage:
    return Storage(":memory:")


def market(yes_bid=0.48, yes_ask=0.50, spec=None, status="active", ticker="MOCK-TICKER-1", event="MOCK-EVENT-1",
           ask_size=100.0, bid_size=100.0, age=0.0, sport="nfl", comp="nfl", title="MOCK Away at MOCK Home") -> SportsMarket:
    return SportsMarket(
        ticker=ticker, event_ticker=event, series_ticker="KXMOCKGAME", sport=sport, competition=comp,
        event_title=title, title="MOCK market", yes_sub_title="MOCK Home", rules="MOCK rules", status=status,
        close_time=None, expected_expiration_time=None, occurrence_time=None,
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=None if yes_ask is None else round(1 - yes_ask, 4),
        no_ask=None if yes_bid is None else round(1 - yes_bid, 4),
        yes_bid_size=bid_size, yes_ask_size=ask_size, last_price=None, volume=1000, open_interest=500,
        fetched_at=time.time() - age, spec=spec or ContractSpec("winner", team_text="MOCK Home"),
        fee_model=FeeModel())


def team(name: str, abbr: str = "", loc: str = "", nick: str = "", pid: str = "") -> TeamRef:
    return TeamRef(name=name, abbreviation=abbr, short_name=nick, nickname=nick, location=loc, provider_id=pid)


def game(sport="nfl", status="in_progress", period=2, clock=0.0, hs=14, as_=7, hp=None, ap=None,
         home=None, away=None, start=None, reds=(0, 0)) -> GameState:
    return GameState(
        game_id="MOCK-GAME-1", sport=sport, competition=sport if sport != "soccer" else "epl",
        start_time=start or datetime.now(timezone.utc) - timedelta(hours=1), status=status,
        home=home or team("Buffalo Bills", "BUF", "Buffalo", "Bills", "1"),
        away=away or team("Miami Dolphins", "MIA", "Miami", "Dolphins", "2"),
        home_score=hs, away_score=as_, period=period, clock_seconds=clock, display_clock="",
        home_period_scores=hp if hp is not None else [7, 7], away_period_scores=ap if ap is not None else [0, 7],
        red_cards_home=reds[0], red_cards_away=reds[1], source="MOCK")


def synthetic_football_results(n_weeks: int = 18, seed: int = 1):
    """Deterministic synthetic season: team strength = index * 3 points. TEST DATA ONLY."""
    import random
    rnd = random.Random(seed)
    teams = [f"Team{i}" for i in range(8)]
    rows = []
    start = datetime(2025, 9, 1)
    gid = 0
    for w in range(n_weeks):
        order = teams[:]
        rnd.shuffle(order)
        for i in range(0, len(order), 2):
            h, a = order[i], order[i + 1]
            sh, sa = teams.index(h) * 3, teams.index(a) * 3
            hs = max(0, round(22 + sh / 2 - sa / 2 + 1 + rnd.gauss(0, 7)))
            as_ = max(0, round(22 + sa / 2 - sh / 2 - 1 + rnd.gauss(0, 7)))
            gid += 1
            rows.append({"game_id": f"g{gid}", "date": (start + timedelta(days=7 * w)).date().isoformat(),
                         "home": h, "away": a, "home_score": hs, "away_score": as_, "neutral": 0})
    return rows


def synthetic_soccer_results(seed: int = 2):
    import math
    import random
    rnd = random.Random(seed)
    teams = [f"Club{i}" for i in range(10)]
    rows = []
    start = datetime(2025, 8, 1)
    d = 0

    def pois(lam):
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= rnd.random()
            if p < L:
                return k
            k += 1
    for h in teams:
        for a in teams:
            if h == a:
                continue
            sh, sa = teams.index(h), teams.index(a)
            lh = 1.4 * math.exp(0.08 * (sh - sa))
            la = 1.1 * math.exp(0.08 * (sa - sh))
            d += 1
            rows.append({"game_id": f"s{d}", "date": (start + timedelta(days=d // 5)).date().isoformat(),
                         "home": h, "away": a, "home_score": pois(lh), "away_score": pois(la), "neutral": 0})
    return rows


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """Replays queued responses; records requests. TEST ONLY."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append((method, url, dict(params or {}), dict(headers or {})))
        return self.responses.pop(0)

    def get(self, url, params=None, headers=None, timeout=None):
        return self.request("GET", url, params, headers, timeout)

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json, {}))
        return self.responses.pop(0)
