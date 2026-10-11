"""Command-line interface: `kalshi-assistant <command>` (or `python -m kalshi_assistant`)."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import __version__
from .analysis import OK, Analyzer
from .bankroll import Bankroll
from .calibration import walk_forward
from .cashout import analyze_exit
from .config import Settings, load_settings, setup_logging
from .execution import preflight
from .explain import explain
from .halftime import halftime_report
from .kalshi_client import KalshiClient, KalshiError
from .live_decisions import evaluate_live_game, render_live_report
from .market_discovery import refresh_with_orderbook
from .monitoring import Monitor, daily_summary
from .notifications import Notifier
from .paper_trading import PaperTrader, TradeRejected
from .positions import PositionStore
from .report import format_full, format_ranked
from .storage import Storage
from .value_engine import NO_TRADES, evaluate_side, resize

SPORT_ALIASES = {"nfl": "nfl", "college": "ncaaf", "ncaaf": "ncaaf", "cfb": "ncaaf", "soccer": "soccer"}


@dataclass
class Ctx:
    settings: Settings
    storage: Storage
    client: KalshiClient
    analyzer: Analyzer
    notifier: Notifier
    account: str


def build(args) -> Ctx:
    s = load_settings(getattr(args, "config", None))
    setup_logging(s)
    st = Storage(s.db_path)
    client = KalshiClient(s.kalshi)
    an = Analyzer(s, st, client)
    notifier = Notifier(s.notify, st, dry_run=getattr(args, "dry_run", False))
    account = "real" if getattr(args, "real", False) else "paper"
    return Ctx(s, st, client, an, notifier, account)


# ------------------------------------------------------------------ commands
def cmd_scan(ctx: Ctx, args) -> None:
    sports = [SPORT_ALIASES[args.sport]] if getattr(args, "sport", None) else None
    pairs, analyses = ctx.analyzer.scan(sports=sports, account=ctx.account, budget=args.available)
    ok = sum(1 for a in analyses if a.status == OK)
    print(f"Scanned {len(analyses)} Kalshi markets ({ok} priced by the model, {len(analyses) - ok} DATA UNAVAILABLE).")
    if len(analyses) - ok:
        from collections import Counter
        why = Counter((a.notes[-1] if a.notes else "unknown") for a in analyses if a.status != OK)
        print("Why markets were skipped:")
        for reason, n in why.most_common(6):
            print(f"  {n:4d} x {reason[:140]}")
    from .paper_trading import validation_status
    from .model_check import price_check
    banner = validation_status(ctx.storage)[1]
    print(banner)
    print(price_check(ctx.storage).verdict)
    print(format_ranked(pairs, args.limit, show_all=args.all))
    if getattr(args, "html", None):
        from pathlib import Path
        from .dashboard import render
        out = Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(pairs, ctx.settings, ctx.storage, banner, len(analyses), ok), encoding="utf-8")
        print(f"Dashboard written to {out}")


def _game_markets(ctx: Ctx, query: str):
    markets = ctx.analyzer.discovery.markets(event_filter=query)
    if not markets:
        print(f"No open Kalshi sports markets match '{query}'. Try `kalshi-assistant scan --all` to see event names.")
    return markets


def cmd_game(ctx: Ctx, args) -> None:
    markets = _game_markets(ctx, args.event)
    if not markets:
        return
    analyses = ctx.analyzer.analyze(markets)
    pairs = ctx.analyzer.opportunities(analyses, ctx.account, args.available)
    game = next((a.game for a in analyses if a.game), None)
    print(f"Event: {markets[0].event_title}  ({len(markets)} markets)")
    if game and game.is_live:
        print(render_live_report(evaluate_live_game(ctx.analyzer, game, markets, ctx.account)))
        return
    for a in analyses:
        line = f"  {a.market.ticker}: {a.market.spec.describe() if a.market.spec else a.market.title} | YES bid/ask {a.market.yes_bid}/{a.market.yes_ask}"
        print(line + (f" | est P(YES) {a.p_yes:.1%}" if a.p_yes is not None else f" | {a.status}: {'; '.join(a.notes[-1:])}"))
    q = sorted([(o, a) for o, a in pairs if o.qualifies], key=lambda t: -t[0].score)
    print()
    if not q:
        print(NO_TRADES)
        near = sorted(pairs, key=lambda t: -t[0].edge)[:2]
        for o, a in near:
            print(f"  Closest: {o.contract_label} {o.side.upper()} @ ${o.price:.2f} est {o.prob:.1%}: " + "; ".join(o.fail_reasons[:3]))
        return
    extras = ctx.analyzer.game_extras(game) if game else {}
    for o, a in q[: args.limit]:
        print(format_full(o, a, extras))
        if args.explain:
            txt = explain(ctx.settings.explain, {"matchup": a.market.event_title, "contract": o.contract_label,
                                                 "price": o.price, "model_probability": o.prob, "edge": o.edge,
                                                 "risks": o.risks, "notes": a.notes, "injuries": (extras.get("detail") or {}).get("injuries")})
            print("Explanation (Claude, facts-only): " + (txt or "unavailable (set explain.enabled and ANTHROPIC_API_KEY)"))
        print()


def cmd_compare(ctx: Ctx, args) -> None:
    markets = _game_markets(ctx, args.event)
    if not markets:
        return
    kinds = set(args.types.split(",")) if args.types else None
    analyses = [a for a in ctx.analyzer.analyze(markets) if not kinds or (a.market.spec and a.market.spec.kind in kinds)]
    pairs = ctx.analyzer.opportunities(analyses, ctx.account, args.available)
    budget = f" (budget ${args.available:.2f})" if args.available else ""
    print(f"COMPARE {markets[0].event_title}{budget}")
    best_by_kind: dict[str, tuple] = {}
    for o, a in pairs:
        k = a.market.spec.kind if a.market.spec else "?"
        if k not in best_by_kind or o.net_ev_per_dollar() > best_by_kind[k][0].net_ev_per_dollar():
            best_by_kind[k] = (o, a)
    for k, (o, a) in sorted(best_by_kind.items()):
        print(f"  [{k}] best side: {o.contract_label} BUY {o.side.upper()} @ ${o.price:.2f} | est {o.prob:.1%} vs implied "
              f"{o.implied_prob:.1%} | net EV/$ {o.net_ev_per_dollar():+.1%} | max loss ${o.max_loss:.2f} | "
              + ("QUALIFIES" if o.qualifies else "no: " + "; ".join(o.fail_reasons[:2])))
    unavailable = [a for a in analyses if a.status != OK]
    for a in unavailable[:5]:
        print(f"  [{a.market.spec.kind if a.market.spec else '?'}] {a.market.ticker}: DATA UNAVAILABLE - {'; '.join(a.notes[-1:])}")
    q = [(o, a) for o, a in best_by_kind.values() if o.qualifies]
    if not q:
        print(NO_TRADES + " None of these markets offers enough value after fees.")
        return
    o, a = max(q, key=lambda t: t[0].score)
    print(f"\nBetter estimated value: {o.contract_label} ({a.market.spec.kind}) - net EV {o.net_ev_per_dollar():+.1%} per $ risked, "
          f"max loss ${o.max_loss:.2f}. A higher win probability elsewhere does not mean better value.")


def cmd_halftime(ctx: Ctx, args) -> None:
    markets = _game_markets(ctx, args.event)
    if not markets:
        return
    a = ctx.analyzer.analyze_market(markets[0])
    if not a.game:
        print("DATA UNAVAILABLE: could not match this event to a live game feed.")
        return
    if not a.game.is_live:
        print(f"Game status is '{a.game.status}'. Halftime report needs a live game; showing current analysis.")
    text, _ = halftime_report(ctx.analyzer, a.game, markets, ctx.account)
    print(text)
    if args.send:
        ctx.notifier.send("halftime", Notifier.make_key("halftime-manual", a.game.game_id, datetime.now().minute), text)


def cmd_live(ctx: Ctx, args) -> None:
    markets = _game_markets(ctx, args.event)
    if not markets:
        return
    a = ctx.analyzer.analyze_market(markets[0])
    if not a.game:
        print("DATA UNAVAILABLE: could not match this event to a game in the live feed.")
        return
    print(render_live_report(evaluate_live_game(ctx.analyzer, a.game, markets, ctx.account),
                             title="LIVE" if a.game.is_live else f"NOT LIVE ({a.game.status})"))


def cmd_positions(ctx: Ctx, args) -> None:
    store = PositionStore(ctx.storage)
    trader = PaperTrader(ctx.storage, ctx.settings)
    if args.action == "add":
        mk = ctx.analyzer.market_by_ticker(args.ticker)
        try:
            res = trader.open_position(
                account="real" if args.real else "paper", ticker=args.ticker, side=args.side, contracts=args.contracts,
                price=args.price, fees=args.fees, event_ticker=mk.event_ticker if mk else None,
                series_ticker=mk.series_ticker if mk else None, sport=mk.sport if mk else None,
                competition=mk.competition if mk else None, market_type=mk.spec.kind if mk and mk.spec else None,
                contract=(mk.spec.describe() if mk and mk.spec else args.ticker), context="manual",
                enforce_limits=not args.real, allow_add=True, notes="added manually")
            print(res.message)
            if args.real:
                print("Tracked as a REAL position you placed yourself. Nothing was sent to Kalshi.")
        except TradeRejected as exc:
            print(f"Rejected: {exc}")
        return
    if args.action == "close":
        try:
            print(trader.sell(args.ticker, args.price, reason="manual close").message)
        except TradeRejected as exc:
            print(f"Rejected: {exc}")
        return
    ps = store.open(None if args.all_accounts else ctx.account)
    if not ps:
        print("No open positions.")
        return
    for p in ps:
        print(f"{p.id} [{p.mode}] {p.contract} | {p.side.upper()} x{p.contracts:g} @ ${p.entry_price:.2f} "
              f"(+${p.entry_fees:.2f} fees) | max loss ${p.cost_basis:.2f} | opened {datetime.fromtimestamp(p.opened_at):%m/%d %H:%M} | {p.market_ticker}")


def cmd_cashout(ctx: Ctx, args) -> None:
    p = PositionStore(ctx.storage).get(args.position)
    if not p or p.status != "open":
        print(f"No open position '{args.position}'.")
        return
    mk = ctx.analyzer.market_by_ticker(p.market_ticker)
    prob, ob = None, None
    if mk:
        a = ctx.analyzer.analyze_market(mk)
        prob = a.p_yes if a.status == OK else None
        try:
            ob = ctx.client.get_orderbook(mk.ticker)
        except KalshiError:
            ob = None
        if a.game:
            print(a.game.score_line())
    br = Bankroll(ctx.storage, ctx.settings.risk)
    over = br.open_exposure(p.mode, p.event_ticker) > ctx.settings.risk.max_event_exposure_dollars + 1e-9
    e = analyze_exit(p, mk, prob, ctx.settings, ob, over)
    print(e.render())
    ctx.storage.record_decision("cashout", e.decision, ticker=p.market_ticker, position_id=p.id,
                                detail={"prob": e.prob_win, "net": e.net_proceeds, "ev_hold": e.ev_hold})


def cmd_bankroll(ctx: Ctx, args) -> None:
    br = Bankroll(ctx.storage, ctx.settings.risk)
    for acct in (["paper", "real"] if not (args.real or args.paper) else [ctx.account]):
        print(br.snapshot(acct).render())
        print()
    r = ctx.settings.risk
    print(f"Sizing rules: ${r.min_position_dollars:.2f}-${r.max_position_dollars:.2f} per position, max {r.max_total_exposure_pct:.0%} "
          f"of equity exposed, ${r.max_event_exposure_dollars:.2f} per game, daily loss limit ${r.daily_loss_limit_dollars:.2f}, "
          f"weekly ${r.weekly_loss_limit_dollars:.2f}.")


def cmd_set_bankroll(ctx: Ctx, args) -> None:
    br = Bankroll(ctx.storage, ctx.settings.risk)
    acct = "paper" if args.paper else "real"
    delta = br.set_bankroll(acct, args.amount)
    print(f"{acct.upper()} cash balance set to ${args.amount:.2f} (adjustment {delta:+.2f}). Open positions are tracked separately.")
    if args.reserve is not None:
        br.set_reserved(acct, args.reserve)
        print(f"Reserved (not tradable): ${args.reserve:.2f}")


def _pick_market(ctx: Ctx, event: str, market_query: str):
    markets = _game_markets(ctx, event)
    mq = market_query.lower()
    for m in markets:
        hay = f"{m.ticker} {m.title} {m.yes_sub_title} {m.spec.describe() if m.spec else ''}".lower()
        if mq == m.ticker.lower() or all(p in hay for p in mq.split()):
            return m
    if markets:
        print(f"No market in this event matches '{market_query}'. Available:")
        for m in markets:
            print(f"  {m.ticker}: {m.spec.describe() if m.spec else m.title}")
    return None


def cmd_plan(ctx: Ctx, args) -> None:
    mk = _pick_market(ctx, args.event, args.market)
    if not mk:
        return
    mk = refresh_with_orderbook(ctx.client, mk)
    a = ctx.analyzer.analyze_market(mk)
    if a.status != OK or a.p_yes is None:
        print(f"DATA UNAVAILABLE: {'; '.join(a.notes)}")
        return
    opp = evaluate_side(mk, args.side, a.p_yes, a.reliability, ctx.settings, a.context)
    if opp is None:
        print(f"No sellers on the {args.side.upper()} side right now - cannot buy at any displayed price.")
        return
    br = Bankroll(ctx.storage, ctx.settings.risk)
    if args.amount:
        n = int(args.amount // (opp.price + opp.fee_per_contract)) or 0
        why = f"your proposed ${args.amount:.2f}"
    else:
        n, why = br.suggest_contracts(ctx.account, opp.prob, opp.price, opp.fee_per_contract, args.available)
    if n <= 0:
        print(f"Size: 0 contracts ({why}).")
        n = 1
    resize(opp, n, ctx.settings)
    limits = br.check_new_position(ctx.account, mk.event_ticker, opp.cost_total)
    if limits:
        opp.qualifies = False
        opp.fail_reasons += [f"risk limit: {x}" for x in limits]
    print(format_full(opp, a, ctx.analyzer.game_extras(a.game) if a.game else {}))
    print(f"\nPLAN: {n} contract(s) ({why}). MAXIMUM POSSIBLE LOSS: ${opp.max_loss:.2f}. Verdict: "
          + ("meets your criteria." if opp.qualifies else "does NOT meet your criteria."))
    if args.paper_buy:
        if not opp.qualifies and not args.force:
            print("Not paper-trading a non-qualifying trade (use --force to record it anyway).")
            return
        try:
            print(PaperTrader(ctx.storage, ctx.settings).open_from_opportunity(opp, n, "paper").message)
        except TradeRejected as exc:
            print(f"Rejected: {exc}")


def cmd_paper(ctx: Ctx, args) -> None:
    trader = PaperTrader(ctx.storage, ctx.settings)
    if args.action == "status":
        print(Bankroll(ctx.storage, ctx.settings.risk).snapshot("paper").render())
        print("Paper trading is the default mode. Real orders are disabled (" + "; ".join(preflight(ctx.settings, ctx.client.authenticated)) + ").")
    elif args.action == "report":
        print(trader.performance("paper"))
        if args.real:
            print(trader.performance("real"))
    elif args.action == "settle":
        msgs = trader.settle_all(ctx.client)
        print("\n".join(msgs) or "No positions settled.")
    elif args.action == "sell":
        p = PositionStore(ctx.storage).get(args.position or "")
        if not p:
            print("Specify an open position id: paper sell <id>")
            return
        mk = ctx.analyzer.market_by_ticker(p.market_ticker)
        bid = mk.bid(p.side) if mk else None
        if bid is None:
            print("No bid available - a sale would not execute right now.")
            return
        try:
            print(trader.sell(p.id, bid, fee_model=mk.fee_model, reason="paper sell at best bid").message)
        except TradeRejected as exc:
            print(f"Rejected: {exc}")
    elif args.action == "auto":
        pairs, _ = ctx.analyzer.scan(account="paper")
        done = 0
        for o, a in pairs:
            if not o.qualifies or done >= args.max:
                continue
            try:
                print(trader.open_from_opportunity(o, o.contracts, "paper").message)
                done += 1
            except TradeRejected as exc:
                print(f"Skipped {o.market.ticker}: {exc}")
        print(f"Paper-traded {done} qualifying opportunit(ies)." if done else NO_TRADES)


def cmd_watch(ctx: Ctx, args) -> None:
    ctx.storage.watch(args.event)
    print(f"Watching '{args.event}'. Watched games are evaluated on every monitoring run.")
    if args.follow:
        Monitor(ctx.settings, ctx.storage, ctx.client, ctx.analyzer, ctx.notifier, ctx.account).run_loop(
            args.interval, only_event=args.event)


def cmd_unwatch(ctx: Ctx, args) -> None:
    ctx.storage.unwatch(args.event)
    print(f"Stopped watching '{args.event}'. Watchlist: {ctx.storage.watchlist() or 'empty'}")


def cmd_monitor(ctx: Ctx, args) -> None:
    mon = Monitor(ctx.settings, ctx.storage, ctx.client, ctx.analyzer, ctx.notifier, ctx.account)
    if args.loop:
        mon.run_loop(args.interval)
    else:
        for line in mon.tick(scan_pregame=args.pregame):
            print(line)


def cmd_stop(ctx: Ctx, args) -> None:
    ctx.storage.kv_set("monitor_stop", True)
    print("Stop flag set. A running `monitor --loop` will exit after its current pass. "
          "To stop GitHub Actions, disable the workflows (see README).")


def cmd_history(ctx: Ctx, args) -> None:
    if not args.trades:
        print("Recent alerts:")
        for r in ctx.notifier.history(args.limit):
            print(f"  {datetime.fromtimestamp(r['ts']):%m/%d %H:%M} {r['kind']:<16} {'sent' if r['sent'] else 'NOT SENT (' + str(r['error']) + ')'}: "
                  + r["preview"].replace("\n", " | "))
    print("Trades (all, including losses):")
    for p in PositionStore(ctx.storage).all(limit=args.limit):
        pnl = f"{p.realized_pnl:+.2f}" if p.realized_pnl is not None else "open"
        print(f"  {datetime.fromtimestamp(p.opened_at):%m/%d %H:%M} [{p.mode}] {p.contract} {p.side.upper()} x{p.contracts:g} "
              f"@ {p.entry_price:.2f} -> {p.status} {pnl}")


def cmd_test_alert(ctx: Ctx, args) -> None:
    if not ctx.notifier.configured and not args.dry_run:
        print("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see README).")
        return
    ok = ctx.notifier.send("test", Notifier.make_key("test", datetime.now().isoformat()),
                           "Test alert from kalshi-sports-assistant. Notifications are working.")
    print("Test alert sent." if ok else "Test alert FAILED - check token/chat id and `history`.")


def cmd_chat_id(ctx: Ctx, args) -> None:
    print("Chat id(s): " + ctx.notifier.get_chat_id())


def cmd_discover(ctx: Ctx, args) -> None:
    infos = ctx.analyzer.discovery.sports_series(refresh=True)
    print(f"{len(infos)} supported series:")
    for i in infos:
        print(f"  {i.ticker:<28} {i.sport:<6} {i.competition:<11} {i.market_type:<11} {i.period:<4} fee={i.fee_model.fee_type}x{i.fee_model.multiplier:g} | {i.title}")
    if args.verbose:
        print(f"\n{len(ctx.analyzer.discovery.skipped)} sports series skipped (unsupported or unclassified):")
        for t, title in ctx.analyzer.discovery.skipped:
            print(f"  {t:<28} {title}")
    print("\nIf a series is misclassified, pin it in config/settings.yaml under sports.series_overrides.")


def cmd_backtest(ctx: Ctx, args) -> None:
    sport = SPORT_ALIASES[args.sport]
    comp = args.competition or (sport if sport != "soccer" else "epl")
    print(f"Loading results for {sport}/{comp} ...")
    ctx.analyzer.refresh_results(sport, comp, force=args.refresh)
    rows = [dict(r) for r in ctx.storage.results(sport, comp if sport == "soccer" else None)]
    if not rows:
        print("No historical results available (provider unreachable?).")
        return
    from .college_football_model import CollegeFootballModel
    from .nfl_model import NFLModel
    from .soccer_model import SoccerModel
    m = ctx.settings.model
    factory = {"nfl": lambda: NFLModel(m), "ncaaf": lambda: CollegeFootballModel(m), "soccer": lambda: SoccerModel(m)}[sport]

    def predict(model, r):
        pred = model.predict(r["home"], r["away"], bool(r["neutral"]))
        if sport == "soccer":
            pr = pred.outcome_probs(m.soccer_rho)
            return pr["home"] / max(pr["home"] + pr["away"], 1e-9)
        return pred.win_prob_home()

    start = date.fromisoformat(rows[0]["date"]) + timedelta(days=args.warmup_days)
    rep = walk_forward(rows, factory, predict, start, sport=sport, competition=comp, version=m.version,
                       min_train=40 if sport != "ncaaf" else 150)
    print(rep.render())


def cmd_summary(ctx: Ctx, args) -> None:
    text = daily_summary(ctx.settings, ctx.storage)
    print(text)
    if args.send:
        ctx.notifier.send("summary", Notifier.make_key("summary", date.today()), text, dedup_minutes=20 * 60)


def cmd_kalshi_account(ctx: Ctx, args) -> None:
    if not ctx.client.authenticated:
        print("Kalshi API key not configured; balance/positions need KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY.")
        return
    bal = ctx.client.get_balance()
    print(f"Kalshi balance (read-only): {bal}")
    for p in ctx.client.get_positions():
        print(f"  {p.get('ticker')}: position {p.get('position_fp', p.get('position'))} exposure {p.get('market_exposure_dollars', p.get('market_exposure'))}")
    print("Shown for reference only; your entertainment bankroll ledger is kept separately.")


# --------------------------------------------------------------------- parser
def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalshi-assistant", description="Kalshi sports trading assistant (recommendations + paper trading only).")
    p.add_argument("--config", help="path to settings.yaml (default config/settings.yaml)")
    p.add_argument("--real", action="store_true", help="use your tracked REAL account instead of paper")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command")

    def add(name, fn, help_, aliases=()):
        sp = sub.add_parser(name, help=help_, aliases=list(aliases))
        sp.set_defaults(fn=fn)
        return sp

    for name, sport, h in (("scan", None, "scan all supported sports markets"), ("best", None, "show the best qualifying opportunities"),
                           ("nfl", "nfl", "scan NFL markets"), ("college", "college", "scan college football markets"),
                           ("soccer", "soccer", "scan soccer markets")):
        sp = add(name, cmd_scan, h)
        sp.set_defaults(sport=sport)
        if name == "scan":
            sp.add_argument("--sport", choices=sorted(SPORT_ALIASES))
        sp.add_argument("--limit", type=int, default=10)
        sp.add_argument("--all", action="store_true", help="also show non-qualifying candidates")
        sp.add_argument("--available", type=float, help="cash you are willing to use right now (caps sizing)")
        sp.add_argument("--html", help="also write a dashboard page to this path")
    sp = add("game", cmd_game, "analyze one game: game \"Arsenal vs Chelsea\"")
    sp.add_argument("event")
    sp.add_argument("--limit", type=int, default=3)
    sp.add_argument("--available", type=float)
    sp.add_argument("--explain", action="store_true", help="add a facts-only Claude explanation")
    sp = add("compare", cmd_compare, "compare winner/spread/total markets for a game")
    sp.add_argument("event")
    sp.add_argument("--types", help="comma list: winner,draw,total,spread,team_total,btts")
    sp.add_argument("--available", type=float, help="e.g. --available 25")
    sp = add("halftime", cmd_halftime, "halftime report for a game")
    sp.add_argument("event")
    sp.add_argument("--send", action="store_true", help="also send to Telegram")
    sp = add("live", cmd_live, "in-game ENTER/HOLD/SELL decisions for a game right now")
    sp.add_argument("event")
    sp = add("positions", cmd_positions, "list / add / close tracked positions")
    sp.add_argument("action", nargs="?", default="list", choices=["list", "add", "close"])
    sp.add_argument("--ticker", help="market ticker (add) or position id (close)")
    sp.add_argument("--side", choices=["yes", "no"], default="yes")
    sp.add_argument("--contracts", type=float, default=1)
    sp.add_argument("--price", type=float, help="fill price in dollars, e.g. 0.55")
    sp.add_argument("--fees", type=float, help="actual fees paid (default: computed)")
    sp.add_argument("--all-accounts", action="store_true")
    sp = add("cashout", cmd_cashout, "compare selling vs holding a position")
    sp.add_argument("position")
    sp = add("bankroll", cmd_bankroll, "show funds, exposure and P/L")
    sp.add_argument("--paper", action="store_true")
    sp = add("set-bankroll", cmd_set_bankroll, "set cash balance: set-bankroll 25 [--paper] [--reserve 5]")
    sp.add_argument("amount", type=float)
    sp.add_argument("--paper", action="store_true")
    sp.add_argument("--reserve", type=float)
    sp = add("plan", cmd_plan, "evaluate a proposed trade: plan \"Bills\" \"over 44.5\" --side yes --amount 1")
    sp.add_argument("event")
    sp.add_argument("market", help="market ticker or words from the contract")
    sp.add_argument("--side", choices=["yes", "no"], default="yes")
    sp.add_argument("--amount", type=float, help="dollars you are considering")
    sp.add_argument("--available", type=float)
    sp.add_argument("--paper-buy", action="store_true", help="record it as a paper trade")
    sp.add_argument("--force", action="store_true")
    sp = add("paper", cmd_paper, "paper trading: status | report | settle | sell <id> | auto")
    sp.add_argument("action", nargs="?", default="status", choices=["status", "report", "settle", "sell", "auto"])
    sp.add_argument("position", nargs="?")
    sp.add_argument("--max", type=int, default=3, help="auto: max new paper trades")
    sp = add("watch", cmd_watch, "monitor a game: watch \"Ohio State\" [--follow]")
    sp.add_argument("event")
    sp.add_argument("--follow", action="store_true", help="loop now in this terminal")
    sp.add_argument("--interval", type=int)
    sp = add("unwatch", cmd_unwatch, "stop watching a game")
    sp.add_argument("event")
    sp = add("monitor", cmd_monitor, "one monitoring pass (default) or --loop")
    sp.add_argument("--loop", action="store_true")
    sp.add_argument("--interval", type=int)
    sp.add_argument("--pregame", action="store_true", help="also scan pregame markets this pass")
    add("stop-monitor", cmd_stop, "ask a running monitor loop to stop")
    sp = add("history", cmd_history, "alerts and trade history")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--trades", action="store_true", help="trades only")
    add("test-alert", cmd_test_alert, "send a Telegram test notification")
    add("telegram-chat-id", cmd_chat_id, "print your Telegram chat id (message your bot first)")
    sp = add("discover", cmd_discover, "list Kalshi sports series and how they were classified")
    sp.add_argument("--verbose", action="store_true")
    sp = add("backtest", cmd_backtest, "walk-forward backtest: backtest --sport nfl")
    sp.add_argument("--sport", required=True, choices=sorted(SPORT_ALIASES))
    sp.add_argument("--competition", help="soccer competition key, e.g. epl")
    sp.add_argument("--warmup-days", type=int, default=90)
    sp.add_argument("--refresh", action="store_true")
    sp = add("summary", cmd_summary, "daily/weekly summary")
    sp.add_argument("--send", action="store_true")
    add("kalshi-account", cmd_kalshi_account, "read-only Kalshi balance/positions (needs API key)")
    add("help", lambda ctx, args: p.print_help(), "show all commands")
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 0
    for k in ("available", "real", "paper"):
        if not hasattr(args, k):
            setattr(args, k, None if k == "available" else False)
    ctx = build(args)
    try:
        args.fn(ctx, args)
    except KalshiError as exc:
        print(f"Kalshi API problem: {exc}. No recommendation produced (data could not be verified).")
        return 2
    finally:
        ctx.storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
