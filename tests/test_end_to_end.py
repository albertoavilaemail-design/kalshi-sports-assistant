"""End-to-end wiring test with MOCK Kalshi + MOCK sports provider (no network).
Exercises discovery -> analysis -> value -> paper trade -> live/halftime -> monitor tick."""
from datetime import date, datetime, timedelta, timezone

from helpers import FakeResponse, settings, storage, synthetic_football_results, team

from kalshi_assistant.analysis import OK, Analyzer
from kalshi_assistant.config import KalshiConfig
from kalshi_assistant.halftime import halftime_report
from kalshi_assistant.kalshi_client import KalshiClient
from kalshi_assistant.monitoring import Monitor
from kalshi_assistant.notifications import Notifier
from kalshi_assistant.paper_trading import PaperTrader
from kalshi_assistant.sports_data import GameState

KICK = datetime.now(timezone.utc) + timedelta(hours=3)


class RoutingSession:
    """Answers MOCK Kalshi API calls by path. TEST ONLY."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def request(self, method, url, params=None, headers=None, timeout=None):
        path = url.split("/trade-api/v2")[1]
        self.calls.append(path)
        if path == "/series":
            return FakeResponse(200, {"series": [
                {"ticker": "KXNFLGAME", "title": "Pro Football Game", "fee_type": "quadratic", "fee_multiplier": 1},
                {"ticker": "KXNFLTOTAL", "title": "Pro Football Total Points", "fee_type": "quadratic", "fee_multiplier": 1},
                {"ticker": "KXNFLMVP", "title": "NFL MVP", "fee_type": "quadratic", "fee_multiplier": 1}]})
        if path == "/events":
            st = (params or {}).get("series_ticker")
            ev = {"event_ticker": f"{st}-MOCK", "title": "Team0 at Team7"}
            if st == "KXNFLGAME":
                ev["markets"] = [
                    {"ticker": "KXNFLGAME-MOCK-T7", "event_ticker": ev["event_ticker"], "yes_sub_title": "Team7", "status": "active",
                     "yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6200", "no_bid_dollars": "0.3800", "no_ask_dollars": "0.4000",
                     "yes_ask_size_fp": "200.00", "yes_bid_size_fp": "200.00", "occurrence_datetime": KICK.isoformat()},
                    {"ticker": "KXNFLGAME-MOCK-T0", "event_ticker": ev["event_ticker"], "yes_sub_title": "Team0", "status": "active",
                     "yes_bid_dollars": "0.3800", "yes_ask_dollars": "0.4000", "no_bid_dollars": "0.6000", "no_ask_dollars": "0.6200",
                     "yes_ask_size_fp": "200.00", "yes_bid_size_fp": "200.00", "occurrence_datetime": KICK.isoformat()}]
            else:
                ev["markets"] = [{"ticker": "KXNFLTOTAL-MOCK-44", "event_ticker": ev["event_ticker"], "floor_strike": 44.5,
                                  "strike_type": "greater", "status": "active", "yes_bid_dollars": "0.4800",
                                  "yes_ask_dollars": "0.5000", "yes_ask_size_fp": "100.00", "yes_bid_size_fp": "100.00"}]
            return FakeResponse(200, {"events": [ev], "cursor": ""})
        if path.startswith("/markets/") and path.endswith("/orderbook"):
            return FakeResponse(200, {"orderbook_fp": {"yes_dollars": [["0.60", "200"]], "no_dollars": [["0.38", "200"]]}})
        if path.startswith("/markets/"):
            return FakeResponse(200, {"market": {"ticker": path.split("/")[2], "status": "active", "result": "",
                                                 "event_ticker": "KXNFLGAME-MOCK", "yes_sub_title": "Team7",
                                                 "yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6200"}})
        if path.startswith("/events/"):
            return FakeResponse(200, {"event": {"event_ticker": "KXNFLGAME-MOCK", "title": "Team0 at Team7"}})
        return FakeResponse(404)


class MockProvider:
    name = "MOCK provider"

    def __init__(self, status="scheduled"):
        self.status = status

    def scoreboard_range(self, sport, comp, start, end):
        g = GameState("G1", "nfl", "nfl", KICK, self.status, team("Team7"), team("Team0"), source="MOCK")
        if self.status != "scheduled":
            g.home_score, g.away_score, g.period, g.clock_seconds = 3, 17, 2, 0.0
            g.home_period_scores, g.away_period_scores = [3, 0], [7, 10]
        return [g]

    def scoreboard(self, *a, **k):
        return self.scoreboard_range(*a)

    def results(self, sport, comp, start, end):
        return []

    def game_detail(self, *a):
        return {"available": True, "source": "MOCK", "injuries": [], "team_stats": {"Team7": {"Total Yards": "120"}}}


def setup(status="scheduled"):
    s = settings()
    s.value.model_weight = 0.6
    st = storage()
    rows = synthetic_football_results()
    # shift synthetic dates to recent past so the history window includes them
    off = (date.today() - date.fromisoformat(rows[-1]["date"])).days - 3
    st.upsert_results([{**r, "game_id": f"nfl:nfl:{r['game_id']}", "sport": "nfl", "competition": "nfl",
                        "date": (date.fromisoformat(r["date"]) + timedelta(days=off)).isoformat(),
                        "source": "MOCK", "fetched_at": 0} for r in rows])
    client = KalshiClient(KalshiConfig(base_url="https://api.elections.kalshi.com/trade-api/v2",
                                       min_request_interval_seconds=0), session=RoutingSession())
    an = Analyzer(s, st, client, provider=MockProvider(status))
    st.kv_set("results_refreshed:nfl:nfl", 9e18)  # skip provider refresh
    return s, st, client, an


def test_pregame_scan_end_to_end():
    s, st, client, an = setup()
    pairs, analyses = an.scan()
    assert len(analyses) == 3  # MVP futures series skipped
    assert all(a.status == OK for a in analyses), [a.notes for a in analyses]
    t7 = next(a for a in analyses if a.market.ticker.endswith("T7"))
    assert t7.team_side == "home" and t7.p_yes > 0.75  # Team7 strongest in synthetic data
    q = [o for o, _ in pairs if o.qualifies]
    assert q and q[0].market.ticker == "KXNFLGAME-MOCK-T7" and q[0].side == "yes"
    assert q[0].max_loss <= s.risk.max_position_dollars + 0.05
    r = PaperTrader(st, s).open_from_opportunity(q[0], q[0].contracts)
    assert "Opened paper position" in r.message


def test_live_halftime_and_monitor_tick():
    s, st, client, an = setup(status="halftime")
    trader = PaperTrader(st, s)
    trader.open_position("paper", "KXNFLGAME-MOCK-T7", "yes", 1, 0.62, event_ticker="KXNFLGAME-MOCK")
    markets = an.discovery.markets()
    game = an.games("nfl", "nfl")[0]
    text, rep = halftime_report(an, game, markets)
    assert "HALFTIME REPORT" in text and "1) ENTER" in text and "2) KEEP HOLDING" in text and "3) SELL" in text
    assert "pregame" in text and len(rep.exits) == 1
    n = Notifier(s.notify, st, dry_run=True)
    mon = Monitor(s, st, client, an, n)
    out = mon.tick(scan_pregame=True)
    assert any("live game" in line for line in out)
    assert st.last_decision("halftime", ticker="G1") == "REPORTED"
    assert mon.tick() and st.q("SELECT COUNT(*) AS c FROM alerts WHERE kind='halftime'")[0]["c"] == 1  # once per game


def test_dashboard_renders_picks_and_paper_section():
    from kalshi_assistant.dashboard import render
    s, st, client, an = setup()
    pairs, analyses = an.scan()
    page = render(pairs, s, st, "MODEL NOT YET VALIDATED", len(analyses), len(analyses))
    assert "<title>Kalshi Picks</title>" in page and "picks found" in page and "data-t=\"soccer\"" in page
    assert "Buy <b>YES</b>" in page and "Paper trading" in page and "MODEL NOT YET VALIDATED" in page
    assert "No qualifying trades found." in render([], s, st, "x", 0, 0)
