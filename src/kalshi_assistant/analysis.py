"""Orchestration: Kalshi markets + sports data + models -> probabilities and opportunities."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .bankroll import Bankroll
from .college_football_model import CollegeFootballModel
from .config import Settings
from .football_model import FootballModel, FootballPrediction, football_prob
from .kalshi_client import KalshiClient
from .live_model import football_live_dists, soccer_live_matrix, window_elapsed
from .market_discovery import MarketDiscovery, SportsMarket
from .nfl_model import NFLModel
from .soccer_model import SoccerModel, SoccerPrediction, soccer_prob
from .sports_data import (ESPNProvider, FootballDataProvider, GameState, OpenMeteoWeather, SportsDataProvider,
                          find_game, match_side)
from .storage import Storage
from .value_engine import Opportunity, evaluate_market, rank, resize

log = logging.getLogger(__name__)

OK, UNAVAILABLE = "OK", "DATA UNAVAILABLE"


@dataclass
class MarketAnalysis:
    market: SportsMarket
    game: GameState | None
    status: str
    context: str = "pregame"  # pregame | live | halftime
    team_side: str | None = None
    p_yes: float | None = None
    p_yes_pregame: float | None = None
    reliability: float = 0.0
    prediction: Any = None
    notes: list[str] = field(default_factory=list)


class Analyzer:
    RESULTS_REFRESH_SECONDS = 12 * 3600

    def __init__(self, settings: Settings, storage: Storage, client: KalshiClient,
                 provider: SportsDataProvider | None = None, results_provider: Any = None):
        self.settings = settings
        self.storage = storage
        self.client = client
        self.provider = provider or ESPNProvider()
        if results_provider is None and settings.sports.soccer_results_provider == "football-data" \
                and settings.sports.football_data_api_key:
            results_provider = FootballDataProvider(settings.sports.football_data_api_key)
        self.soccer_results_provider = results_provider
        self.discovery = MarketDiscovery(client, settings, storage)
        self.bankroll = Bankroll(storage, settings.risk)
        self._models: dict[tuple[str, str], Any] = {}
        self._games: dict[tuple[str, str], list[GameState]] = {}
        self.weather = OpenMeteoWeather()

    # --------------------------------------------------------------- history
    def refresh_results(self, sport: str, comp: str, force: bool = False) -> int:
        key = f"results_refreshed:{sport}:{comp}"
        last = self.storage.kv_get(key, 0)
        if not force and time.time() - last < self.RESULTS_REFRESH_SECONDS:
            return 0
        prov = self.soccer_results_provider if (sport == "soccer" and self.soccer_results_provider) else self.provider
        today = date.today()
        start = today - timedelta(days=self.settings.model.history_days)
        end = today - timedelta(days=1)

        def to_rows(games, fallback_day):
            return [{
                "game_id": f"{sport}:{comp}:{g.game_id}", "sport": sport, "competition": comp,
                "date": (g.start_time or datetime.combine(fallback_day, datetime.min.time())).date().isoformat(),
                "home": g.home.name, "away": g.away.name, "home_score": g.home_score, "away_score": g.away_score,
                "neutral": int(g.neutral), "source": g.source, "fetched_at": time.time(),
            } for g in games if g.status == "final" and g.home_score is not None and g.away_score is not None]

        n = 0
        if hasattr(prov, "history_chunks"):
            for ck, fetch in prov.history_chunks(sport, comp, start, end):
                wkey = f"results_chunk:{prov.name}:{sport}:{comp}:{ck}"
                if self.storage.kv_get(wkey):
                    continue
                games = fetch()
                n += self.storage.upsert_results(to_rows(games, start))
                dates = [g.start_time.date() for g in games if g.start_time]
                if games and all(g.status in ("final", "postponed") for g in games) and max(dates) < today - timedelta(days=3):
                    self.storage.kv_set(wkey, True)  # finished chunk: never refetch
        else:
            d = start
            while d <= end:
                e = min(d + timedelta(days=9), end)
                wkey = f"results_window:{prov.name}:{sport}:{comp}:{d.isoformat()}"
                complete = e < today - timedelta(days=3)
                if not (complete and self.storage.kv_get(wkey)):
                    n += self.storage.upsert_results(to_rows(prov.results(sport, comp, d, e), d))
                    if complete:
                        self.storage.kv_set(wkey, True)
                d = e + timedelta(days=1)
        self.storage.kv_set(key, time.time())
        return n

    def model_for(self, sport: str, comp: str) -> Any:
        k = (sport, comp)
        if k in self._models:
            return self._models[k]
        try:
            self.refresh_results(sport, comp)
        except Exception as exc:  # noqa: BLE001
            log.warning("results refresh failed for %s/%s: %s", sport, comp, exc)
        since = (date.today() - timedelta(days=self.settings.model.history_days)).isoformat()
        rows = [dict(r) for r in self.storage.results(sport, comp if sport == "soccer" else None, since=since)]
        mcfg = self.settings.model
        model = NFLModel(mcfg) if sport == "nfl" else CollegeFootballModel(mcfg) if sport == "ncaaf" else SoccerModel(mcfg)
        model.calibrate(rows)
        model.fit(rows)
        self._models[k] = model
        return model

    # ----------------------------------------------------------------- games
    def games(self, sport: str, comp: str) -> list[GameState]:
        k = (sport, comp)
        if k not in self._games:
            today = date.today()
            if hasattr(self.provider, "scoreboard_range"):
                ahead = 7 if sport != "soccer" else 4
                gs = self.provider.scoreboard_range(sport, comp, today - timedelta(days=1), today + timedelta(days=ahead))
            else:
                gs = self.provider.scoreboard(sport, comp, today)
            self._games[k] = gs
        return self._games[k]

    def refresh_games(self) -> None:
        self._games.clear()

    # --------------------------------------------------------------- analyze
    def analyze(self, markets: list[SportsMarket]) -> list[MarketAnalysis]:
        out = []
        for mk in markets:
            try:
                out.append(self.analyze_market(mk))
            except Exception as exc:  # noqa: BLE001
                log.exception("analysis failed for %s", mk.ticker)
                out.append(MarketAnalysis(mk, None, UNAVAILABLE, notes=[f"analysis error: {type(exc).__name__}"]))
        return out

    def analyze_market(self, mk: SportsMarket, momentum: float = 0.0) -> MarketAnalysis:
        if mk.spec is None:
            return MarketAnalysis(mk, None, UNAVAILABLE, notes=["Contract terms could not be parsed; skipped (never guessed)."])
        games = self.games(mk.sport, mk.competition)
        game = find_game(mk.event_title or mk.title, games, mk.event_time())
        if game is None:
            return MarketAnalysis(mk, None, UNAVAILABLE, notes=["No matching game found in the sports data feed."])
        a = MarketAnalysis(mk, game, OK)
        a.context = "halftime" if game.status == "halftime" else "live" if game.status == "in_progress" else "pregame"
        if game.status in ("final", "postponed", "unknown"):
            a.status = UNAVAILABLE
            a.notes.append(f"Game status is '{game.status}'; no trading analysis.")
            return a
        spec = mk.spec
        if spec.kind in ("winner", "spread", "team_total"):
            a.team_side = match_side(spec.team_text, game)
            if a.team_side is None:
                a.status = UNAVAILABLE
                a.notes.append(f"Could not confidently match Kalshi team '{spec.team_text}' to {game.home.name} / {game.away.name}.")
                return a
        model = self.model_for(mk.sport, mk.competition)
        pred = model.predict(game.home.name, game.away.name, game.neutral)
        a.prediction = pred
        a.reliability = pred.reliability
        a.notes += pred.notes
        mcfg, lcfg = self.settings.model, self.settings.live
        if isinstance(model, FootballModel):
            pre = model.pregame_dists(pred, spec.period)
            a.p_yes_pregame = football_prob(spec, a.team_side, pre)
            if a.context == "pregame":
                a.p_yes = a.p_yes_pregame
            else:
                d = football_live_dists(pred, game, spec.period, mcfg, lcfg, momentum_shift=momentum)
                a.p_yes = football_prob(spec, a.team_side, d) if d else None
        else:
            a.p_yes_pregame = soccer_prob(spec, a.team_side, model.pregame_matrix(pred, spec.period))
            if a.context == "pregame":
                a.p_yes = a.p_yes_pregame
            else:
                m = soccer_live_matrix(pred, game, spec.period, mcfg, lcfg, momentum_factor=momentum)
                a.p_yes = soccer_prob(spec, a.team_side, m) if m is not None else None
        if a.p_yes is None:
            a.status = UNAVAILABLE
            a.notes.append("Live game state incomplete (clock/period/score missing) or market type unsupported.")
        if a.context != "pregame":
            age = time.time() - game.fetched_at
            if age > self.settings.kalshi.stale_after_seconds:
                a.status = UNAVAILABLE
                a.notes.append(f"Live score data is {age:.0f}s old (stale).")
        return a

    # ---------------------------------------------------------- opportunities
    def opportunities(self, analyses: list[MarketAnalysis], account: str = "paper",
                      budget: float | None = None) -> list[tuple[Opportunity, MarketAnalysis]]:
        out = []
        for a in analyses:
            if a.status != OK or a.p_yes is None:
                continue
            for opp in evaluate_market(a.market, a.p_yes, a.reliability, self.settings, a.context):
                n, why = self.bankroll.suggest_contracts(account, opp.prob, opp.price, opp.fee_per_contract, budget)
                if n > 0:
                    resize(opp, n, self.settings)
                    opp.notes.append(f"Sizing: {why}")
                    limits = self.bankroll.check_new_position(account, a.market.event_ticker, opp.cost_total)
                    if limits:
                        opp.qualifies = False
                        opp.fail_reasons += [f"risk limit: {x}" for x in limits]
                else:
                    opp.qualifies = False
                    opp.fail_reasons.append(f"sizing: {why}")
                corr = self.bankroll.correlated_positions(account, a.market.event_ticker)
                if corr:
                    opp.risks.append("Correlated with your open positions on this game: " + "; ".join(corr))
                out.append((opp, a))
        return out

    def scan(self, sports: list[str] | None = None, event_filter: str | None = None, account: str = "paper",
             budget: float | None = None) -> tuple[list[tuple[Opportunity, MarketAnalysis]], list[MarketAnalysis]]:
        markets = self.discovery.markets(sports=sports, event_filter=event_filter)
        analyses = self.analyze(markets)
        pairs = self.opportunities(analyses, account, budget)
        from .model_check import apply_injury_filter, record_picks
        try:
            apply_injury_filter(self, pairs)
        except Exception as exc:  # noqa: BLE001
            log.warning("injury filter failed: %s", exc)
        ranked = rank([o for o, _ in pairs], only_qualifying=False)
        by_id = {id(o): a for o, a in pairs}
        # One pick per game x market type x period: alternate lines of the same market are the same bet.
        seen: set = set()
        out = []
        for o in ranked:
            a = by_id[id(o)]
            spec = a.market.spec
            key = (a.market.event_ticker.split("-", 1)[-1], spec.kind if spec else "?", spec.period if spec else "?")
            if o.qualifies and key in seen:
                continue
            if o.qualifies:
                seen.add(key)
            out.append((o, a))
        try:
            record_picks(self.storage, out)
        except Exception as exc:  # noqa: BLE001
            log.warning("pick tracking failed: %s", exc)
        return out, analyses

    def market_by_ticker(self, ticker: str) -> SportsMarket | None:
        """Load one market directly (e.g. for an open position) with its series classification."""
        from .market_discovery import parse_market
        try:
            m = self.client.get_market(ticker)
        except Exception as exc:  # noqa: BLE001
            log.warning("market %s unavailable: %s", ticker, exc)
            return None
        ev_ticker = m.get("event_ticker", "")
        infos = {i.ticker: i for i in self.discovery.sports_series()}
        info = next((i for t, i in infos.items() if ev_ticker.upper().startswith(t.upper())), None)
        if info is None:
            return None
        try:
            ev = self.client.get_event(ev_ticker)
        except Exception:  # noqa: BLE001
            ev = {"title": m.get("title", ""), "event_ticker": ev_ticker}
        return parse_market(m, info, ev)

    def game_extras(self, game: GameState) -> dict[str, Any]:
        """Injuries, team stats and weather (best effort, all labeled with source)."""
        extra: dict[str, Any] = {}
        try:
            extra["detail"] = self.provider.game_detail(game.sport, game.competition, game.game_id)
        except Exception as exc:  # noqa: BLE001
            extra["detail"] = {"available": False, "reason": str(exc)}
        if game.sport != "soccer" and not game.venue.get("indoor") and game.status == "scheduled":
            try:
                extra["weather"] = self.weather.forecast(game.venue.get("city"), game.venue.get("state"), game.start_time)
            except Exception as exc:  # noqa: BLE001
                extra["weather"] = {"available": False, "reason": str(exc)}
        return extra


def elapsed(game: GameState, settings: Settings) -> float | None:
    return window_elapsed(game, settings.model, settings.live)


def prediction_summary(pred: Any) -> str:
    if isinstance(pred, FootballPrediction):
        return (f"Model: {pred.home} {pred.mu_home_pts:.1f} - {pred.mu_away_pts:.1f} {pred.away} "
                f"(margin {pred.mu_margin:+.1f}, total {pred.mu_total:.1f}; sd {pred.sd_margin:.1f}/{pred.sd_total:.1f}); "
                f"games in data: {pred.games_home:.0f}/{pred.games_away:.0f} (recency-weighted)")
    if isinstance(pred, SoccerPrediction):
        return (f"Model: expected goals {pred.home} {pred.lam_home:.2f} - {pred.lam_away:.2f} {pred.away}; "
                f"matches in data: {pred.games_home:.0f}/{pred.games_away:.0f} (recency-weighted)")
    return "Model summary unavailable"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
