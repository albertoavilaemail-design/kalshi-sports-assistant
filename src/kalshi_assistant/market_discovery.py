"""Discover tradable Kalshi sports markets without hardcoded market tickers.

Flow: GET /series?category=Sports -> classify each series (sport, competition,
market type, period) -> GET /events?series_ticker=..&status=open&with_nested_markets
-> parse each market into a SportsMarket with a machine-readable ContractSpec.

Classification is heuristic (Kalshi does not publish a sports taxonomy). Anything
that cannot be classified is SKIPPED, never guessed. Run `kalshi-assistant discover`
to review classifications, and pin corrections in settings.yaml `sports.series_overrides`.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .fees import FeeModel
from .kalshi_client import KalshiClient, _f

log = logging.getLogger(__name__)

SOCCER_COMPS: list[tuple[str, str]] = [
    # (regex on ticker or title, competition key)
    (r"\bEPL\b|PREMIER LEAGUE|^KXEPL", "epl"),
    (r"\bUCL\b|CHAMPIONS LEAGUE|^KXUCL", "ucl"),
    (r"LA ?LIGA|^KXLALIGA", "laliga"),
    (r"BUNDESLIGA|^KXBUNDESLIGA", "bundesliga"),
    (r"SERIE ?A\b|^KXSERIEA", "seriea"),
    (r"LIGUE ?1|^KXLIGUE1", "ligue1"),
    (r"\bMLS\b|MAJOR LEAGUE SOCCER|^KXMLS", "mls"),
]

FUTURES_HINTS = re.compile(
    r"CHAMPION|MVP|SUPER BOWL WINNER|PLAYOFF|DRAFT|AWARD|HEISMAN|WINS? THE (LEAGUE|TITLE)|"
    r"TOP SCORER|RELEGAT|SEASON|COACH|TRADE|SIGN", re.I)

PERIODS = {
    # key: (first_period, last_period) in sport units; football=quarters, soccer=halves
    "full": None,
    "1H": "1H", "2H": "2H", "Q1": "Q1", "Q2": "Q2", "Q3": "Q3", "Q4": "Q4",
}


@dataclass
class SeriesInfo:
    ticker: str
    title: str
    sport: str
    competition: str
    market_type: str  # winner | total | spread | team_total | btts
    period: str  # full | 1H | 2H | Q1..Q4
    fee_model: FeeModel = field(default_factory=FeeModel)
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class ContractSpec:
    """What a YES contract pays on, in model terms."""
    kind: str  # winner | draw | total | spread | team_total | btts
    period: str = "full"
    team_text: str | None = None  # raw team text from Kalshi (resolved to home/away later)
    line: float | None = None
    direction: str = "over"  # over | under (for totals); spread is always "team wins by more than line"
    strict: bool = True  # True: value > line ; False: value >= line

    def describe(self) -> str:
        p = "" if self.period == "full" else f" [{self.period}]"
        if self.kind == "winner":
            return f"{self.team_text} wins{p}"
        if self.kind == "draw":
            return f"Draw / tie{p}"
        if self.kind == "btts":
            return f"Both teams score{p}"
        cmp_ = (">" if self.strict else ">=") if self.direction == "over" else ("<" if self.strict else "<=")
        if self.kind == "total":
            return f"Total {cmp_} {self.line}{p}"
        if self.kind == "team_total":
            return f"{self.team_text} total {cmp_} {self.line}{p}"
        if self.kind == "spread":
            return f"{self.team_text} wins by {'more than' if self.strict else 'at least'} {self.line}{p}"
        return self.kind


@dataclass
class SportsMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    sport: str
    competition: str
    event_title: str
    title: str
    yes_sub_title: str
    rules: str
    status: str
    close_time: str | None
    expected_expiration_time: str | None
    occurrence_time: str | None
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    yes_bid_size: float | None
    yes_ask_size: float | None
    last_price: float | None
    volume: float | None
    open_interest: float | None
    fetched_at: float
    spec: ContractSpec | None
    fee_model: FeeModel
    result: str = ""

    @property
    def url(self) -> str:
        # Series-level page (documented pattern kalshi.com/markets/<series>); event slugs are not documented.
        return f"https://kalshi.com/markets/{self.series_ticker.lower()}"

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return round(self.yes_ask - self.yes_bid, 4)

    @property
    def is_tradable(self) -> bool:
        return self.status in ("active", "open")

    def age_seconds(self, now: float | None = None) -> float:
        return (now or time.time()) - self.fetched_at

    def is_stale(self, max_age: float, now: float | None = None) -> bool:
        return self.age_seconds(now) > max_age

    def ask(self, side: str) -> float | None:
        return self.yes_ask if side == "yes" else self.no_ask

    def bid(self, side: str) -> float | None:
        return self.yes_bid if side == "yes" else self.no_bid

    def event_time(self) -> datetime | None:
        for t in (self.occurrence_time, self.expected_expiration_time, self.close_time):
            if t:
                try:
                    return datetime.fromisoformat(t.replace("Z", "+00:00"))
                except ValueError:
                    continue
        return None


# ---------------------------------------------------------------- classification
def _detect_sport(text: str, ticker: str) -> tuple[str, str] | None:
    t = text.upper()
    tk = ticker.upper()
    if "NCAAF" in tk or re.search(r"COLLEGE FOOTBALL|\bCFB\b|\bNCAAF\b", t):
        return "ncaaf", "ncaaf"
    if re.match(r"^KXNFL", tk) or re.search(r"\bNFL\b|PRO FOOTBALL|PROFESSIONAL FOOTBALL", t):
        return "nfl", "nfl"
    for pattern, comp in SOCCER_COMPS:
        if re.search(pattern, tk) or re.search(pattern, t):
            return "soccer", comp
    return None


def _detect_market_type(text: str, ticker: str) -> str | None:
    t = text.upper()
    tk = ticker.upper()
    if "BTTS" in tk or "BOTH TEAMS" in t:
        return "btts"
    if "TEAMTOTAL" in tk or "TEAM TOTAL" in t:
        return "team_total"
    if "SPREAD" in tk or re.search(r"\bSPREAD\b|WINS? BY|MARGIN", t):
        return "spread"
    if "TOTAL" in tk or re.search(r"\bTOTAL\b|OVER/UNDER|POINTS SCORED|GOALS SCORED", t):
        return "total"
    if re.search(r"GAME$|MATCH$|GAME\b", tk) or re.search(r"\bWINNER\b|\bWIN\b|\bGAME\b|\bMATCH\b", t):
        return "winner"
    return None


def _detect_period(text: str, ticker: str) -> str:
    t = text.upper()
    tk = ticker.upper()
    if re.search(r"1H|FIRST HALF|1ST HALF", tk + " " + t):
        return "1H"
    if re.search(r"2H|SECOND HALF|2ND HALF", tk + " " + t):
        return "2H"
    for q in ("1", "2", "3", "4"):
        if re.search(rf"{q}Q\b|Q{q}\b|QUARTER {q}|{['','1ST','2ND','3RD','4TH'][int(q)]} QUARTER", tk + " " + t):
            return f"Q{q}"
    return "full"


def classify_series(series: dict, settings: Settings) -> SeriesInfo | None:
    ticker = series.get("ticker", "")
    title = series.get("title", "")
    override = settings.sports.series_overrides.get(ticker)
    fee = FeeModel.from_series(series, settings.value.taker_fee_rate, settings.value.maker_fee_rate)
    if override:
        return SeriesInfo(ticker, title, override["sport"], override.get("competition", override["sport"]),
                          override["market_type"], override.get("period", "full"), fee, series)
    text = " ".join([title, " ".join(series.get("tags") or [])])
    if FUTURES_HINTS.search(title):
        return None
    sport = _detect_sport(text, ticker)
    if not sport:
        return None
    mtype = _detect_market_type(title, ticker)
    if not mtype:
        return None
    return SeriesInfo(ticker, title, sport[0], sport[1], mtype, _detect_period(title, ticker), fee, series)


_NUM = re.compile(r"(-?\d+(?:\.\d+)?)")


def _line_from(market: dict) -> float | None:
    fs = _f(market.get("floor_strike"))
    if fs is not None:
        return fs
    for k in ("yes_sub_title", "title", "subtitle"):
        m = _NUM.search(str(market.get(k) or ""))
        if m:
            return float(m.group(1))
    return None


def _team_from_text(text: str) -> str:
    text = re.split(r"\s+(?:wins?|to win|by|over|under|total|scores?)\b", text, maxsplit=1, flags=re.I)[0]
    return text.strip(" ?:-")


def parse_contract(market: dict, info: SeriesInfo) -> ContractSpec | None:
    yes = str(market.get("yes_sub_title") or "").strip()
    title = str(market.get("title") or "")
    strike_type = str(market.get("strike_type") or "")
    period = info.period
    if info.market_type == "winner":
        if re.fullmatch(r"(tie|draw)", yes, re.I) or re.search(r"\b(tie|draw)\b", yes, re.I):
            return ContractSpec("draw", period)
        team = yes or _team_from_text(title.replace("Will ", ""))
        return ContractSpec("winner", period, team_text=team) if team else None
    if info.market_type == "btts":
        return ContractSpec("btts", period)
    line = _line_from(market)
    if line is None:
        return None
    direction = "under" if strike_type in ("less", "less_or_equal") or re.search(r"\bunder\b", yes, re.I) else "over"
    strict = strike_type not in ("greater_or_equal", "less_or_equal")
    if info.market_type == "total":
        return ContractSpec("total", period, line=line, direction=direction, strict=strict)
    team = _team_from_text(yes or title)
    if not team:
        return None
    if info.market_type == "team_total":
        return ContractSpec("team_total", period, team_text=team, line=line, direction=direction, strict=strict)
    if info.market_type == "spread":
        return ContractSpec("spread", period, team_text=team, line=abs(line), strict=strict)
    return None


def _price(m: dict, dollars_key: str, cents_key: str) -> float | None:
    v = _f(m.get(dollars_key))
    if v is None:
        c = _f(m.get(cents_key))
        v = c / 100.0 if c is not None else None
    if v is not None and (v <= 0 or v >= 1):
        return None  # 0 bid / 1 ask means "no order on that side"
    return v


def parse_market(m: dict, info: SeriesInfo, event: dict, fetched_at: float | None = None) -> SportsMarket:
    return SportsMarket(
        ticker=m.get("ticker", ""),
        event_ticker=m.get("event_ticker") or event.get("event_ticker", ""),
        series_ticker=info.ticker,
        sport=info.sport,
        competition=info.competition,
        event_title=str(event.get("title") or ""),
        title=str(m.get("title") or ""),
        yes_sub_title=str(m.get("yes_sub_title") or ""),
        rules=" ".join(x for x in [m.get("rules_primary"), m.get("rules_secondary")] if x),
        status=str(m.get("status") or ""),
        close_time=m.get("close_time"),
        expected_expiration_time=m.get("expected_expiration_time"),
        occurrence_time=m.get("occurrence_datetime"),
        yes_bid=_price(m, "yes_bid_dollars", "yes_bid"),
        yes_ask=_price(m, "yes_ask_dollars", "yes_ask"),
        no_bid=_price(m, "no_bid_dollars", "no_bid"),
        no_ask=_price(m, "no_ask_dollars", "no_ask"),
        yes_bid_size=_f(m.get("yes_bid_size_fp")),
        yes_ask_size=_f(m.get("yes_ask_size_fp")),
        last_price=_f(m.get("last_price_dollars")),
        volume=_f(m.get("volume_fp")) or _f(m.get("volume")),
        open_interest=_f(m.get("open_interest_fp")) or _f(m.get("open_interest")),
        fetched_at=fetched_at or time.time(),
        spec=parse_contract(m, info),
        fee_model=info.fee_model,
        result=str(m.get("result") or ""),
    )


def refresh_with_orderbook(client: KalshiClient, mk: SportsMarket) -> SportsMarket:
    """Re-read the order book so prices/depth are current (used before any actionable alert)."""
    ob = client.get_orderbook(mk.ticker)
    ya, na = ob.best_yes_ask, ob.best_no_ask
    yb, nb = ob.best_yes_bid, ob.best_no_bid
    mk.yes_ask, mk.yes_ask_size = (ya[0], ya[1]) if ya else (None, None)
    mk.no_ask = na[0] if na else None
    mk.yes_bid, mk.yes_bid_size = (yb[0], yb[1]) if yb else (None, None)
    mk.no_bid = nb[0] if nb else None
    mk.fetched_at = ob.fetched_at
    return mk


class MarketDiscovery:
    SERIES_CACHE_SECONDS = 6 * 3600

    def __init__(self, client: KalshiClient, settings: Settings, storage=None):
        self.client = client
        self.settings = settings
        self.storage = storage
        self.skipped: list[tuple[str, str]] = []

    def sports_series(self, refresh: bool = False) -> list[SeriesInfo]:
        raw: list[dict] | None = None
        if self.storage is not None and not refresh:
            raw = self.storage.cache_get("kalshi_sports_series", self.SERIES_CACHE_SECONDS)
        if raw is None:
            raw = self.client.get_series_list(category="Sports")
            for extra in self.settings.sports.extra_series:
                try:
                    raw.append(self.client.get_series(extra))
                except Exception as exc:  # noqa: BLE001
                    log.warning("extra series %s unavailable: %s", extra, exc)
            if self.storage is not None:
                self.storage.cache_set("kalshi_sports_series", raw)
        out: list[SeriesInfo] = []
        self.skipped = []
        for s in raw:
            info = classify_series(s, self.settings)
            if info is None:
                self.skipped.append((s.get("ticker", "?"), s.get("title", "")))
                continue
            if info.sport not in self.settings.sports.enabled:
                continue
            if info.sport == "soccer" and info.competition not in self.settings.sports.soccer_competitions:
                continue
            out.append(info)
        return out

    def markets(self, sports: list[str] | None = None, event_filter: str | None = None) -> list[SportsMarket]:
        result: list[SportsMarket] = []
        for info in self.sports_series():
            if sports and info.sport not in sports:
                continue
            try:
                events = self.client.get_events(series_ticker=info.ticker, status="open")
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not load events for %s: %s", info.ticker, exc)
                continue
            now = time.time()
            for ev in events:
                if event_filter and not _matches_filter(ev, event_filter):
                    continue
                for m in ev.get("markets") or []:
                    mk = parse_market(m, info, ev, now)
                    if mk.spec is None:
                        log.debug("Unparsed contract %s (%s)", mk.ticker, mk.title)
                    result.append(mk)
        return result

    def event_markets(self, event_ticker: str) -> list[SportsMarket]:
        """All supported markets whose event ticker or title matches."""
        return self.markets(event_filter=event_ticker)


def _matches_filter(ev: dict, q: str) -> bool:
    q = q.lower().strip()
    hay = " ".join(str(ev.get(k) or "") for k in ("event_ticker", "title", "sub_title")).lower()
    if q in hay:
        return True
    parts = [p for p in re.split(r"\s+(?:vs\.?|v|at|@)\s+|\s+", q) if len(p) > 2]
    return bool(parts) and all(p in hay for p in parts)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def describe_market_row(mk: SportsMarket) -> dict[str, Any]:
    return {
        "ticker": mk.ticker, "event": mk.event_title, "contract": mk.spec.describe() if mk.spec else "UNPARSED",
        "yes_bid": mk.yes_bid, "yes_ask": mk.yes_ask, "status": mk.status,
    }
