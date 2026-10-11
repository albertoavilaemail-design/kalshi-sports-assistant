"""Monitoring: one `tick()` does a full pass; run it from GitHub Actions (every ~10-15
minutes, best effort) or loop it in a background service (every 30-60 s).

Each tick:
  1. discovers open Kalshi sports markets and pulls live scores,
  2. for live games you watch / hold / (optionally) all live games: re-prices every
     market, records snapshots, and produces ENTER / HOLD / SELL decisions,
  3. detects halftime and sends one halftime report per game,
  4. alerts on new qualifying trades (re-checking the order book first), decision
     changes on positions, big probability or price moves, red cards, markets closing,
     stale data and risk-limit warnings,
  5. settles finished paper positions.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime

from .analysis import OK, Analyzer
from .bankroll import Bankroll
from .config import Settings
from .halftime import halftime_report, is_halftime
from .kalshi_client import KalshiClient, KalshiUnavailable
from .live_decisions import evaluate_live_game, render_live_report
from .market_discovery import SportsMarket, refresh_with_orderbook
from .notifications import Notifier, opportunity_alert
from .paper_trading import PaperTrader
from .positions import PositionStore
from .sports_data import find_game
from .storage import Storage
from .value_engine import evaluate_side, resize

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, settings: Settings, storage: Storage, client: KalshiClient, analyzer: Analyzer,
                 notifier: Notifier, account: str = "paper"):
        self.settings = settings
        self.storage = storage
        self.client = client
        self.analyzer = analyzer
        self.notifier = notifier
        self.account = account
        self.positions = PositionStore(storage)
        self.bankroll = Bankroll(storage, settings.risk)

    # ---------------------------------------------------------------- helpers
    def _recheck(self, opp) -> bool:
        """Re-read the order book right before alerting; alert only if it still qualifies."""
        mk = refresh_with_orderbook(self.client, opp.market)
        p_yes = opp.model_prob_raw if opp.side == "yes" else 1 - opp.model_prob_raw
        fresh = evaluate_side(mk, opp.side, p_yes, opp.reliability,
                              self.settings, opp.context, opp.contracts)
        if fresh is None:
            return False
        resize(fresh, opp.contracts, self.settings)
        return fresh.qualifies

    def _watch_match(self, mk: SportsMarket) -> bool:
        hay = f"{mk.event_ticker} {mk.event_title}".lower()
        return any(q.lower() in hay or all(p in hay for p in q.lower().split() if len(p) > 2)
                   for q in self.storage.watchlist())

    # ------------------------------------------------------------------- tick
    def tick(self, scan_pregame: bool = False, only_event: str | None = None) -> list[str]:
        holder = self.storage.acquire_lock("monitor", ttl=600)
        if not holder:
            return ["Another monitoring run is active; skipped to avoid duplicate alerts."]
        log_lines: list[str] = []
        try:
            self._tick(scan_pregame, only_event, log_lines)
        except KalshiUnavailable as exc:
            log_lines.append(f"Kalshi unavailable: {exc}")
            self.notifier.send("failure", Notifier.make_key("kalshi-down", datetime.now().strftime("%Y%m%d%H")),
                               f"MONITORING DEGRADED: Kalshi API unavailable ({exc}). Recommendations suppressed.",
                               dedup_minutes=60)
        finally:
            self.storage.prune()
            self.storage.release_lock("monitor", holder)
        return log_lines

    def _tick(self, scan_pregame: bool, only_event: str | None, out: list[str]) -> None:
        an = self.analyzer
        an.refresh_games()
        markets = an.discovery.markets(event_filter=only_event)
        out.append(f"{len(markets)} open sports markets discovered.")
        open_pos = self.positions.open()
        held_events = {p.event_ticker for p in open_pos}

        by_event: dict[str, list[SportsMarket]] = defaultdict(list)
        for m in markets:
            by_event[m.event_ticker].append(m)

        live_targets = []
        pregame_markets: list[SportsMarket] = []
        for ev, mks in by_event.items():
            m0 = mks[0]
            try:
                game = find_game(m0.event_title or m0.title, an.games(m0.sport, m0.competition), m0.event_time())
            except Exception as exc:  # noqa: BLE001
                out.append(f"sports data unavailable for {m0.sport}: {exc}")
                continue
            if game is None:
                continue
            wanted = (ev in held_events or self._watch_match(m0) or only_event is not None
                      or self.settings.live.scan_all_live_games)
            if game.is_live and wanted:
                live_targets.append((game, mks))
            elif game.status == "scheduled":
                pregame_markets.extend(mks)
        out.append(f"{len(live_targets)} live game(s) to evaluate.")

        for game, mks in live_targets:
            self._live_game(game, mks, out)
        if scan_pregame and pregame_markets:
            self._pregame(pregame_markets, out)
        self._position_health(markets, out)
        if open_pos:
            trader = PaperTrader(self.storage, self.settings)
            for msg in trader.settle_all(self.client):
                out.append(msg)
                self.notifier.send("settlement", Notifier.make_key("settle", msg), "SETTLED: " + msg)

    def _live_game(self, game, mks, out: list[str]) -> None:
        n = self.notifier
        if is_halftime(game):
            if self.storage.last_decision("halftime", ticker=game.game_id) is None:
                text, rep = halftime_report(self.analyzer, game, mks, self.account)
                n.send("halftime", Notifier.make_key("halftime", game.game_id), text, dedup_minutes=24 * 60)
                self.storage.record_decision("halftime", "REPORTED", game_id=game.game_id, ticker=game.game_id)
                out.append(f"Halftime report: {game.title}")
            return
        rep = evaluate_live_game(self.analyzer, game, mks, self.account)
        out.append(f"{game.title}: {len(rep.entries)} entry signal(s), {len(rep.exits)} position(s) checked")
        # red cards
        reds = (game.red_cards_home, game.red_cards_away)
        last = self.storage.kv_get(f"reds:{game.game_id}", [0, 0])
        if game.sport == "soccer" and list(reds) != list(last):
            n.send("event", Notifier.make_key("red", game.game_id, reds),
                   f"RED CARD update [{game.title}]: {game.home.name} {reds[0]}, {game.away.name} {reds[1]}\n{game.score_line()}\n"
                   "Live probabilities have been re-priced.")
            self.storage.kv_set(f"reds:{game.game_id}", list(reds))
        # entries
        for o, a in rep.entries[:3]:
            key = Notifier.make_key("entry", o.market.ticker, o.side, round(o.price, 2))
            n.send("live_entry", key, opportunity_alert(o, a) + "\n" + rep.momentum.describe(game),
                   recheck=lambda o=o: self._recheck(o))
        # exits: alert when the decision changes
        for e in rep.exits:
            prev = self.storage.last_decision("exit_eval", position_id=e.position.id)
            if prev != e.decision:
                self.storage.record_decision("exit_eval", e.decision, game_id=game.game_id,
                                             ticker=e.position.market_ticker, position_id=e.position.id,
                                             detail={"prob": e.prob_win, "sell": e.sell_price})
                if prev is not None or e.decision in ("SELL / EXIT", "REDUCE EXPOSURE"):
                    n.send("hold_sell", Notifier.make_key("exit", e.position.id, e.decision),
                           f"POSITION UPDATE [{game.title}] {prev or 'new'} -> {e.decision}\n{game.score_line()}\n{e.render()}")
        # material probability / price moves on markets you hold or watch
        watched = {p.market_ticker for p in self.positions.open()} | {m.ticker for m in mks if self._watch_match(m)}
        for a in rep.analyses:
            t = a.market.ticker
            if t not in watched or a.status != OK or a.p_yes is None:
                continue
            lastp = self.storage.kv_get(f"lastp:{t}")
            if lastp is None:
                self.storage.kv_set(f"lastp:{t}", a.p_yes)
            elif abs(a.p_yes - lastp) >= self.settings.live.prob_change_alert:
                n.send("prob_change", Notifier.make_key("prob", t, round(a.p_yes, 2)),
                       f"PROBABILITY CHANGE [{game.title}] {a.market.spec.describe() if a.market.spec else t}: "
                       f"{lastp:.0%} -> {a.p_yes:.0%} (estimate)\nKalshi YES bid/ask {a.market.yes_bid}/{a.market.yes_ask}\n"
                       f"{game.score_line()}\n{a.market.url}")
                self.storage.kv_set(f"lastp:{t}", a.p_yes)
            pm = rep.momentum.price_moves.get(t)
            if pm is not None and abs(pm) >= self.settings.live.price_move_alert:
                n.send("price_move", Notifier.make_key("price", t, round(pm, 1)),
                       f"PRICE MOVE [{game.title}] {t}: YES mid {pm:+.2f} in {rep.momentum.window_minutes} min. "
                       f"Our estimate moved {rep.momentum.model_moves.get(t, 0):+.2f}.\n" + "\n".join(rep.momentum.overreaction_notes()))
        if rep.unavailable and len(rep.unavailable) == len(rep.analyses):
            n.send("stale", Notifier.make_key("unavail", game.game_id, datetime.now().strftime("%Y%m%d%H")),
                   f"DATA UNAVAILABLE for all markets in {game.title}; live recommendations suppressed.\n"
                   + "; ".join(rep.unavailable[0].notes[-1:]))

    def _pregame(self, markets: list[SportsMarket], out: list[str]) -> None:
        analyses = self.analyzer.analyze(markets)
        pairs = self.analyzer.opportunities(analyses, self.account)
        q = sorted([(o, a) for o, a in pairs if o.qualifies], key=lambda t: -t[0].score)
        out.append(f"Pregame: {len(q)} qualifying opportunit(ies) across {len(markets)} markets.")
        for o, a in q[:5]:
            if o.net_ev_per_dollar() < self.settings.notify.min_net_ev_per_dollar_alert:
                continue
            key = Notifier.make_key("pregame", o.market.ticker, o.side, round(o.price, 2))
            self.notifier.send("pregame", key, opportunity_alert(o, a), recheck=lambda o=o: self._recheck(o))

    def _position_health(self, markets: list[SportsMarket], out: list[str]) -> None:
        tickers = {m.ticker for m in markets}
        for p in self.positions.open():
            if p.market_ticker in tickers:
                continue
            try:
                m = self.client.get_market(p.market_ticker)
            except Exception:  # noqa: BLE001
                continue
            if m.get("status") not in ("active", "open") and not m.get("result"):
                self.notifier.send("market_unavailable", Notifier.make_key("closed", p.market_ticker, m.get("status")),
                                   f"MARKET NO LONGER TRADING: {p.contract} ({p.market_ticker}) status '{m.get('status')}'. "
                                   "You may be unable to sell before settlement.", dedup_minutes=24 * 60)
        for acct in ("paper", "real"):
            d = self.bankroll.realized_since(acct, self.bankroll.day_start())
            lim = self.settings.risk.daily_loss_limit_dollars
            if d <= -0.8 * lim:
                self.notifier.send("risk", Notifier.make_key("risk", acct, datetime.now().date()),
                                   f"RISK LIMIT WARNING ({acct}): today's realized P/L ${d:.2f} vs daily limit -${lim:.2f}. "
                                   "New entries are blocked at the limit.", dedup_minutes=24 * 60)

    # ------------------------------------------------------------------- loop
    def run_loop(self, interval: int | None = None, scan_pregame_every: int = 15, only_event: str | None = None,
                 max_ticks: int | None = None) -> None:
        interval = interval or self.settings.live.interval_seconds
        i = 0
        print(f"Monitoring every {interval}s. Stop with Ctrl+C or `kalshi-assistant stop-monitor`.")
        self.storage.kv_set("monitor_stop", False)
        while max_ticks is None or i < max_ticks:
            if self.storage.kv_get("monitor_stop", False):
                print("Stop flag set; exiting monitor.")
                return
            started = time.time()
            for line in self.tick(scan_pregame=(i % scan_pregame_every == 0), only_event=only_event):
                print(f"[{datetime.now():%H:%M:%S}] {line}")
            i += 1
            time.sleep(max(interval - (time.time() - started), 1))


def daily_summary(settings: Settings, storage: Storage) -> str:
    br = Bankroll(storage, settings.risk)
    trader = PaperTrader(storage, settings)
    since = br.day_start()
    alerts = storage.q("SELECT kind, COUNT(*) AS n FROM alerts WHERE ts>=? AND sent=1 GROUP BY kind", (since,))
    lines = [f"DAILY SUMMARY {datetime.now():%Y-%m-%d}", br.snapshot("paper").render(), br.snapshot("real").render()]
    lines.append("Alerts sent today: " + (", ".join(f"{r['kind']} {r['n']}" for r in alerts) or "none"))
    lines.append(trader.performance("paper"))
    return "\n".join(lines)


def live_report_text(analyzer: Analyzer, game, markets, account: str = "paper") -> str:
    return render_live_report(evaluate_live_game(analyzer, game, markets, account))
