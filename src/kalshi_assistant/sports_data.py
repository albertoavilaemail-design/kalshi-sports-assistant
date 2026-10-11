"""Sports data provider abstraction + implementations.

Providers
---------
ESPNProvider (default; free, no key)
    Uses ESPN's public site API (site.api.espn.com). It is UNOFFICIAL and
    undocumented: it is widely used but can change or rate-limit without notice.
    Supplies schedules, live scores, clock/period, linescores, possession (NFL/CFB),
    red cards and goals (soccer), venue/weather, injuries and box-score stats.
FootballDataProvider (optional; free tier with API key, documented)
    https://www.football-data.org/documentation/api  — official v4 REST API for
    soccer results (PL, CL, PD, BL1, SA, FL1). MLS is not in the free tier.
OpenMeteoWeather (free, documented, no key) — https://open-meteo.com/en/docs
    Forecast wind/precipitation for outdoor American-football venues.

Swap or add providers by implementing the `SportsDataProvider` protocol.
Nothing here fabricates data: missing fields stay None and are reported as unavailable.
"""
from __future__ import annotations

import difflib
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

ESPN_PATHS = {
    ("nfl", "nfl"): "football/nfl",
    ("ncaaf", "ncaaf"): "football/college-football",
    ("soccer", "epl"): "soccer/eng.1",
    ("soccer", "ucl"): "soccer/uefa.champions",
    ("soccer", "laliga"): "soccer/esp.1",
    ("soccer", "bundesliga"): "soccer/ger.1",
    ("soccer", "seriea"): "soccer/ita.1",
    ("soccer", "ligue1"): "soccer/fra.1",
    ("soccer", "mls"): "soccer/usa.1",
}
FOOTBALL_DATA_CODES = {"epl": "PL", "ucl": "CL", "laliga": "PD", "bundesliga": "BL1", "seriea": "SA", "ligue1": "FL1"}

_STOP = {"fc", "cf", "afc", "sc", "ac", "club", "the", "cd", "ud", "ssc", "as", "rc", "vfb", "vfl", "tsg", "1"}


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\bst\.?$", "state", s.strip())
    s = re.sub(r"\bst\.\s", "saint ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t not in _STOP]
    return " ".join(toks)


@dataclass
class TeamRef:
    name: str
    abbreviation: str = ""
    short_name: str = ""
    nickname: str = ""
    location: str = ""
    provider_id: str = ""
    rank: int | None = None  # poll ranking (college) if provided
    logo: str = ""  # image URL supplied by the data provider

    @property
    def key(self) -> str:
        return normalize_name(self.name)

    def aliases(self) -> list[str]:
        out = {normalize_name(x) for x in (self.name, self.short_name, self.location, self.nickname) if x}
        if self.abbreviation:
            out.add(self.abbreviation.lower())
        if self.location and self.nickname:
            out.add(normalize_name(f"{self.location} {self.nickname}"))
        out.discard("")
        return sorted(out, key=len, reverse=True)


@dataclass
class GameState:
    game_id: str
    sport: str
    competition: str
    start_time: datetime | None
    status: str  # scheduled | in_progress | halftime | final | postponed | unknown
    home: TeamRef
    away: TeamRef
    home_score: float | None = None
    away_score: float | None = None
    period: int = 0
    clock_seconds: float | None = None  # football: remaining in period; soccer: elapsed match seconds
    display_clock: str = ""
    home_period_scores: list[float] = field(default_factory=list)
    away_period_scores: list[float] = field(default_factory=list)
    possession: str | None = None  # home | away
    red_cards_home: int = 0
    red_cards_away: int = 0
    goal_minutes_home: list[float] = field(default_factory=list)
    goal_minutes_away: list[float] = field(default_factory=list)
    neutral: bool = False
    venue: dict[str, Any] = field(default_factory=dict)
    weather: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    fetched_at: float = field(default_factory=time.time)

    @property
    def is_live(self) -> bool:
        return self.status in ("in_progress", "halftime")

    @property
    def title(self) -> str:
        return f"{self.away.name} at {self.home.name}" if self.sport != "soccer" else f"{self.home.name} vs {self.away.name}"

    def score_line(self) -> str:
        if self.home_score is None:
            return "score unavailable"
        return f"{self.home.name} {int(self.home_score)} - {int(self.away_score or 0)} {self.away.name} ({self.display_clock or self.status}, P{self.period})"


class SportsDataProvider(Protocol):
    name: str

    def scoreboard(self, sport: str, competition: str, day: date | None = None) -> list[GameState]: ...
    def results(self, sport: str, competition: str, start: date, end: date) -> list[GameState]: ...
    def game_detail(self, sport: str, competition: str, game_id: str) -> dict[str, Any]: ...


def _get_json(session: requests.Session, url: str, params: dict | None = None, headers: dict | None = None,
              retries: int = 3, timeout: float = 15) -> dict | None:
    delay = 1.0
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                log.warning("%s -> HTTP %s (attempt %d)", url, r.status_code, attempt + 1)
            else:
                log.warning("%s -> HTTP %s", url, r.status_code)
                return None
        except (requests.RequestException, ValueError) as exc:
            log.warning("%s request failed: %s", url, type(exc).__name__)
        time.sleep(delay)
        delay *= 2
    return None


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class ESPNProvider:
    name = "espn (unofficial public API)"
    BASE = "https://site.api.espn.com/apis/site/v2/sports"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def _path(self, sport: str, competition: str) -> str:
        key = (sport, competition if sport == "soccer" else sport)
        if key not in ESPN_PATHS:
            raise ValueError(f"No ESPN mapping for {sport}/{competition}")
        return ESPN_PATHS[key]

    def _params(self, sport: str) -> dict:
        p: dict[str, Any] = {"limit": 300}
        if sport == "ncaaf":
            p["groups"] = 80  # FBS
        return p

    def scoreboard(self, sport: str, competition: str, day: date | None = None) -> list[GameState]:
        return self._fetch(sport, competition, {"dates": day.strftime("%Y%m%d")} if day else {})

    def _fetch(self, sport: str, competition: str, params: dict) -> list[GameState]:
        p = self._params(sport)
        p.update(params)
        data = _get_json(self.session, f"{self.BASE}/{self._path(sport, competition)}/scoreboard", p)
        if not data:
            return []
        return [g for g in (self.parse_event(e, sport, competition) for e in data.get("events") or []) if g]

    def scoreboard_range(self, sport: str, competition: str, start: date, end: date) -> list[GameState]:
        """ESPN rejects date ranges (HTTP 400 since 2026), so query one day at a time."""
        seen: dict[str, GameState] = {}
        d = start
        while d <= end:
            for g in self._fetch(sport, competition, {"dates": d.strftime("%Y%m%d")}):
                seen[g.game_id] = g
            d += timedelta(days=1)
        return list(seen.values())

    def history_chunks(self, sport: str, competition: str, start: date, end: date) -> list[tuple[str, Any]]:
        """Cheap historical queries: football by (season, season type, week), soccer by month."""
        chunks: list[tuple[str, Any]] = []
        if sport in ("nfl", "ncaaf"):
            weeks = {"nfl": [(2, w) for w in range(1, 19)] + [(3, w) for w in range(1, 6)],
                     "ncaaf": [(2, w) for w in range(1, 17)] + [(3, 1)]}[sport]
            for season in range(start.year - 1, end.year + 1):
                for stype, wk in weeks:
                    chunks.append((f"{season}-{stype}-{wk}",
                                   lambda season=season, stype=stype, wk=wk: self._fetch(
                                       sport, competition, {"dates": str(season), "seasontype": stype, "week": wk})))
        else:
            y, m = start.year, start.month
            while (y, m) <= (end.year, end.month):
                chunks.append((f"{y}{m:02d}", lambda y=y, m=m: self._fetch(sport, competition, {"dates": f"{y}{m:02d}"})))
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return chunks

    def results(self, sport: str, competition: str, start: date, end: date) -> list[GameState]:
        out: list[GameState] = []
        for _, fetch in self.history_chunks(sport, competition, start, end):
            out.extend(g for g in fetch() if g.status == "final" and g.start_time
                       and start <= g.start_time.date() <= end)
        return out

    def game_detail(self, sport: str, competition: str, game_id: str) -> dict[str, Any]:
        data = _get_json(self.session, f"{self.BASE}/{self._path(sport, competition)}/summary", {"event": game_id})
        if not data:
            return {"available": False}
        out: dict[str, Any] = {"available": True, "source": self.name, "fetched_at": time.time(),
                               "injuries": [], "team_stats": {}}
        for team_block in data.get("injuries") or []:
            tname = (team_block.get("team") or {}).get("displayName", "")
            for inj in team_block.get("injuries") or []:
                ath = inj.get("athlete") or {}
                out["injuries"].append({
                    "team": tname, "player": ath.get("displayName"),
                    "position": (ath.get("position") or {}).get("abbreviation"),
                    "status": inj.get("status"), "detail": (inj.get("details") or {}).get("type"),
                })
        for tb in (data.get("boxscore") or {}).get("teams") or []:
            tname = (tb.get("team") or {}).get("displayName", "")
            stats = {}
            for st in tb.get("statistics") or []:
                label = st.get("label") or st.get("name")
                stats[label] = st.get("displayValue")
            out["team_stats"][tname] = stats
        return out

    def parse_event(self, ev: dict, sport: str, competition: str) -> GameState | None:
        try:
            comp = (ev.get("competitions") or [{}])[0]
            teams = comp.get("competitors") or []
            home_c = next(c for c in teams if c.get("homeAway") == "home")
            away_c = next(c for c in teams if c.get("homeAway") == "away")
        except (StopIteration, IndexError):
            return None

        def team(c: dict) -> TeamRef:
            t = c.get("team") or {}
            rank = (c.get("curatedRank") or {}).get("current")
            return TeamRef(
                name=t.get("displayName") or t.get("name") or "?",
                abbreviation=t.get("abbreviation") or "",
                short_name=t.get("shortDisplayName") or "",
                nickname=t.get("name") or "",
                location=t.get("location") or "",
                provider_id=str(t.get("id") or c.get("id") or ""),
                rank=rank if isinstance(rank, int) and 0 < rank < 99 else None,
                logo=str(t.get("logo") or ((t.get("logos") or [{}])[0].get("href")) or ""),
            )

        st = (ev.get("status") or comp.get("status") or {})
        stype = st.get("type") or {}
        name = (stype.get("name") or "").upper()
        state = stype.get("state")
        if "HALFTIME" in name:
            status = "halftime"
        elif "POSTPONED" in name or "CANCELED" in name or "SUSPENDED" in name:
            status = "postponed"
        elif state == "pre":
            status = "scheduled"
        elif state == "in":
            status = "in_progress"
        elif state == "post":
            status = "final"
        else:
            status = "unknown"

        def score(c: dict) -> float | None:
            v = c.get("score")
            if isinstance(v, dict):
                v = v.get("value")
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        def lines(c: dict) -> list[float]:
            out = []
            for ls in c.get("linescores") or []:
                try:
                    out.append(float(ls.get("value", ls.get("displayValue", 0)) or 0))
                except (TypeError, ValueError):
                    out.append(0.0)
            return out

        home, away = team(home_c), team(away_c)
        g = GameState(
            game_id=str(ev.get("id")), sport=sport, competition=competition,
            start_time=_dt(ev.get("date") or comp.get("date")), status=status, home=home, away=away,
            home_score=score(home_c) if status != "scheduled" else None,
            away_score=score(away_c) if status != "scheduled" else None,
            period=int(st.get("period") or 0),
            clock_seconds=float(st["clock"]) if st.get("clock") is not None else None,
            display_clock=str(st.get("displayClock") or ""),
            home_period_scores=lines(home_c), away_period_scores=lines(away_c),
            neutral=bool(comp.get("neutralSite")),
            venue={"name": (comp.get("venue") or {}).get("fullName"),
                   "city": ((comp.get("venue") or {}).get("address") or {}).get("city"),
                   "state": ((comp.get("venue") or {}).get("address") or {}).get("state"),
                   "indoor": (comp.get("venue") or {}).get("indoor")},
            weather=dict(ev.get("weather") or {}),
            source=self.name,
        )
        sit = comp.get("situation") or {}
        poss = str(sit.get("possession") or "")
        if poss:
            g.possession = "home" if poss == home.provider_id else "away" if poss == away.provider_id else None
        for d in comp.get("details") or []:
            tid = str((d.get("team") or {}).get("id") or "")
            side = "home" if tid == home.provider_id else "away" if tid == away.provider_id else None
            if side is None:
                continue
            if d.get("redCard"):
                if side == "home":
                    g.red_cards_home += 1
                else:
                    g.red_cards_away += 1
            if d.get("scoringPlay"):
                minute = ((d.get("clock") or {}).get("value") or 0) / 60.0
                (g.goal_minutes_home if side == "home" else g.goal_minutes_away).append(minute)
        return g


class FootballDataProvider:
    """Official football-data.org v4 API (soccer results only). Requires FOOTBALL_DATA_API_KEY."""
    name = "football-data.org v4"
    BASE = "https://api.football-data.org/v4"

    def __init__(self, api_key: str, session: requests.Session | None = None):
        if not api_key:
            raise ValueError("FOOTBALL_DATA_API_KEY is required for FootballDataProvider")
        self.api_key = api_key
        self.session = session or requests.Session()

    def results(self, sport: str, competition: str, start: date, end: date) -> list[GameState]:
        code = FOOTBALL_DATA_CODES.get(competition)
        if sport != "soccer" or not code:
            return []
        out: list[GameState] = []
        d = start
        while d <= end:  # free tier limits date ranges; query in 10-day windows
            e = min(d + timedelta(days=9), end)
            data = _get_json(self.session, f"{self.BASE}/competitions/{code}/matches",
                             {"dateFrom": d.isoformat(), "dateTo": e.isoformat(), "status": "FINISHED"},
                             headers={"X-Auth-Token": self.api_key})
            for m in (data or {}).get("matches") or []:
                ft = (m.get("score") or {}).get("fullTime") or {}
                if ft.get("home") is None:
                    continue
                out.append(GameState(
                    game_id=f"fd-{m.get('id')}", sport="soccer", competition=competition,
                    start_time=_dt(m.get("utcDate")), status="final",
                    home=TeamRef(m["homeTeam"].get("name", "?"), m["homeTeam"].get("tla") or "",
                                 m["homeTeam"].get("shortName") or ""),
                    away=TeamRef(m["awayTeam"].get("name", "?"), m["awayTeam"].get("tla") or "",
                                 m["awayTeam"].get("shortName") or ""),
                    home_score=float(ft["home"]), away_score=float(ft["away"]), source=self.name))
            d = e + timedelta(days=1)
            time.sleep(6.5)  # free tier: 10 requests/minute
        return out

    def scoreboard(self, sport: str, competition: str, day: date | None = None) -> list[GameState]:
        return []  # live scores come from the scoreboard provider

    def game_detail(self, sport: str, competition: str, game_id: str) -> dict[str, Any]:
        return {"available": False}


class OpenMeteoWeather:
    name = "open-meteo.com"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def forecast(self, city: str | None, state: str | None, when: datetime | None) -> dict[str, Any]:
        if not city or not when:
            return {"available": False, "reason": "venue city or kickoff time unknown"}
        geo = _get_json(self.session, "https://geocoding-api.open-meteo.com/v1/search",
                        {"name": city, "count": 5, "country": "US"})
        results = (geo or {}).get("results") or []
        if state:
            results = [r for r in results if (r.get("admin1") or "").lower().startswith(state.lower()[:4])] or results
        if not results:
            return {"available": False, "reason": f"could not geocode {city}"}
        lat, lon = results[0]["latitude"], results[0]["longitude"]
        when_utc = when.astimezone(timezone.utc)
        data = _get_json(self.session, "https://api.open-meteo.com/v1/forecast", {
            "latitude": lat, "longitude": lon, "hourly": "temperature_2m,wind_speed_10m,precipitation",
            "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "timezone": "UTC",
            "start_date": when_utc.date().isoformat(), "end_date": when_utc.date().isoformat()})
        if not data:
            return {"available": False, "reason": "forecast unavailable"}
        hours = (data.get("hourly") or {}).get("time") or []
        target = when_utc.strftime("%Y-%m-%dT%H:00")
        if target not in hours:
            return {"available": False, "reason": "kickoff hour outside forecast range"}
        i = hours.index(target)
        h = data["hourly"]
        return {"available": True, "source": self.name, "temperature_f": h["temperature_2m"][i],
                "wind_mph": h["wind_speed_10m"][i], "precip_mm": h["precipitation"][i], "fetched_at": time.time()}


# ------------------------------------------------------------------ matching
def _alias_score(text: str, team: TeamRef) -> float:
    t = normalize_name(text)
    if not t:
        return 0.0
    best = 0.0
    tt = set(t.split())
    for a in team.aliases():
        if t == a:
            return 1.0
        at = set(a.split())
        if at and (at <= tt or tt <= at):
            best = max(best, 0.9 if len(at & tt) >= 1 and len(a) > 3 else 0.7)
        best = max(best, difflib.SequenceMatcher(None, t, a).ratio() * 0.95)
    return best


def match_side(text: str | None, game: GameState, min_score: float = 0.75, min_margin: float = 0.08) -> str | None:
    """Resolve Kalshi team text to 'home' / 'away'; None when ambiguous (never guess)."""
    if not text:
        return None
    hs, as_ = _alias_score(text, game.home), _alias_score(text, game.away)
    if max(hs, as_) < min_score or abs(hs - as_) < min_margin:
        return None
    return "home" if hs > as_ else "away"


def _contains_alias(title_norm: str, team: TeamRef) -> float:
    padded = f" {title_norm} "
    for a in team.aliases():
        if len(a) >= 3 and f" {a} " in padded:
            return len(a)
    return 0.0


def find_game(event_title: str, games: list[GameState], event_time: datetime | None = None,
              tolerance_hours: float = 36) -> GameState | None:
    title = normalize_name(event_title)
    best, best_score = None, 0.0
    for g in games:
        if event_time and g.start_time and abs((g.start_time - event_time).total_seconds()) > tolerance_hours * 3600:
            continue
        h, a = _contains_alias(title, g.home), _contains_alias(title, g.away)
        if h and a and h + a > best_score:
            best, best_score = g, h + a
    return best
