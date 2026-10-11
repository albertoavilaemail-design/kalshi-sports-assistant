"""Static mobile-friendly dashboard (published to GitHub Pages after every scan).

It shows the picks from the most recent scan, so it opens instantly at any time;
the timestamp at the top says how fresh it is.
"""
from __future__ import annotations

import html
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from .analysis import MarketAnalysis
from .bankroll import Bankroll
from .config import Settings
from .positions import PositionStore
from .storage import Storage
from .value_engine import Opportunity

TZ = ZoneInfo(os.getenv("KSA_TIMEZONE", "America/Chicago"))

BIG_CLUBS = ["barcelona", "real madrid", "atletico", "atlético", "bayern", "dortmund", "paris saint", "psg",
             "manchester city", "man city", "manchester united", "man united", "liverpool", "arsenal", "chelsea",
             "tottenham", "inter", "milan", "juventus", "napoli"]

CSS = """
:root{--bg:#f4f5f8;--card:#fff;--ink:#14161b;--mute:#5d6470;--line:#e2e5ea;--good:#13795b;--bad:#c2341f;--warn:#9a5800;
--accent:#2d5bd7;--soft:#e9eefc;--track:#eceff3}
@media (prefers-color-scheme:dark){:root{--bg:#0e1014;--card:#171a20;--ink:#eceef2;--mute:#9aa2ae;--line:#2a2f38;
--good:#4cc79a;--bad:#ff6b57;--warn:#f0b35a;--accent:#7ea2ff;--soft:#1d2433;--track:#252a33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px 16px 40px}
h1{font-size:21px;margin:4px 0 2px}h2{font-size:13px;margin:22px 0 8px;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
h3{font-size:15px;margin:18px 0 6px}
.meta{color:var(--mute);font-size:13px}
.banner{background:color-mix(in srgb,var(--warn) 13%,var(--card));border:1px solid color-mix(in srgb,var(--warn) 55%,transparent);padding:10px 12px;border-radius:10px;margin:12px 0;font-size:14px}
.tabs{display:flex;gap:6px;position:sticky;top:0;background:var(--bg);padding:10px 0;z-index:5;overflow-x:auto}
.tab{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:7px 14px;font:inherit;font-size:14px;cursor:pointer;white-space:nowrap}
.tab[aria-selected=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.tab .n{opacity:.75;margin-left:4px}
.panel{display:none}.panel.on{display:block}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:6px 0 4px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px}
.stat b{display:block;font-size:20px}.stat span{color:var(--mute);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin:10px 0}
.card.big{border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}
.top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.match{font-weight:650}.when{color:var(--mute);font-size:13px;white-space:nowrap}
.bet{margin:6px 0 10px;font-size:16px}.bet b{color:var(--accent)}
.bars{display:grid;gap:6px;margin:4px 0 10px}
.bar{display:grid;grid-template-columns:96px 1fr 44px;align-items:center;gap:8px;font-size:12px;color:var(--mute)}
.track{height:10px;background:var(--track);border-radius:6px;overflow:hidden}
.fill{height:100%;border-radius:6px}.fill.us{background:var(--accent)}.fill.mk{background:var(--mute);opacity:.55}
.bar b{color:var(--ink);text-align:right;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;font-size:13px}
.grid div span{display:block;color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.03em}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;background:color-mix(in srgb,var(--good) 15%,transparent);color:var(--good)}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:var(--bad);letter-spacing:.06em}
.dot{width:8px;height:8px;border-radius:50%;background:var(--bad);animation:p 1.4s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.dot{animation:none}}
.score{display:grid;grid-template-columns:1fr auto;gap:2px 10px;background:var(--soft);border-radius:10px;padding:8px 10px;margin:8px 0;font-size:14px}
.score b{font-size:17px;text-align:right}.score .clk{grid-column:1/3;color:var(--mute);font-size:12px}
.empty{color:var(--mute);padding:16px;text-align:center}
.logo{width:22px;height:22px;object-fit:contain;vertical-align:-6px;margin-right:6px}
.score .logo{width:18px;height:18px;vertical-align:-4px}
.teams{display:inline-flex;align-items:center;flex-wrap:wrap;gap:2px}.at{color:var(--mute);font-weight:400;margin:0 8px}
a{color:var(--accent)}table{width:100%;border-collapse:collapse;font-size:13px}td{padding:6px 4px;border-top:1px solid var(--line)}
.vs{display:grid;grid-template-columns:1fr;gap:6px}
@media (max-width:480px){.grid{grid-template-columns:repeat(2,1fr)}.bar{grid-template-columns:84px 1fr 40px}}
"""


def _t(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%I:%M %p").lstrip("0") if dt else "time n/a"


def _is_big(a: MarketAnalysis) -> bool:
    names = [a.market.event_title]
    if a.game:
        names += [a.game.home.name, a.game.away.name]
    hay = " ".join(names).lower()
    return any(c in hay for c in BIG_CLUBS)


def _order(items):
    far = datetime.max.replace(tzinfo=TZ)
    return sorted(items, key=lambda t: (0 if t[1].game and t[1].game.is_live else 1,
                                        t[1].game.start_time if t[1].game and t[1].game.start_time else far))


def _bar(label: str, p: float, cls: str) -> str:
    w = max(min(p, 1), 0) * 100
    return (f'<div class="bar"><span>{label}</span><div class="track"><div class="fill {cls}" style="width:{w:.0f}%">'
            f'</div></div><b>{p:.0%}</b></div>')


def _logo(t, size: int = 22) -> str:
    if not t or not getattr(t, "logo", ""):
        return ""
    return (f'<img class="logo" src="{html.escape(t.logo)}" alt="" width="{size}" height="{size}" loading="lazy" '
            f'onerror="this.remove()">')


def _matchup(a: MarketAnalysis) -> str:
    g = a.game
    if not g:
        return html.escape(a.market.event_title)
    return (f'<span class="teams">{_logo(g.away)}{html.escape(g.away.short_name or g.away.name)}'
            f'<span class="at">{"vs" if g.sport == "soccer" else "@"}</span>'
            f'{_logo(g.home)}{html.escape(g.home.short_name or g.home.name)}</span>')


def _score(g) -> str:
    e = html.escape
    if not g or not g.is_live or g.home_score is None:
        return ""
    clock = "Halftime" if g.status == "halftime" else f"{g.display_clock} · period {g.period}"
    extra = ""
    if g.sport == "soccer" and (g.red_cards_home or g.red_cards_away):
        extra = f" · red cards {g.red_cards_home}-{g.red_cards_away}"
    return (f'<div class="score"><span>{_logo(g.home, 18)}{e(g.home.name)}</span><b>{g.home_score:g}</b>'
            f'<span>{_logo(g.away, 18)}{e(g.away.name)}</span><b>{g.away_score:g}</b><span class="clk">{e(clock + extra)}</span></div>')


def _card(o: Opportunity, a: MarketAnalysis) -> str:
    e = html.escape
    g = a.game
    what = a.market.spec.describe() if a.market.spec else a.market.title
    live = '<span class="badge"><span class="dot"></span>LIVE</span> ' if g and g.is_live else ""
    return f"""<div class="card{' big' if a.market.sport == 'soccer' and _is_big(a) else ''}">
<div class="top"><div class="match">{live}{_matchup(a)}</div><div class="when">{e(_t(g.start_time if g else None))}</div></div>
<div class="meta">{e(a.market.sport.upper())} · {e(a.market.competition)} · {e(a.market.event_title)}</div>
{_score(g)}
<div class="bet">Buy <b>{o.side.upper()}</b> on “{e(what)}” at <b>${o.price:.2f}</b> <span class="pill">edge {o.edge:+.1%}</span></div>
<div class="bars">{_bar("Our estimate", o.prob, "us")}{_bar("Kalshi price", o.implied_prob, "mk")}</div>
<div class="grid">
<div><span>Size</span>{o.contracts:g} contract(s)</div><div><span>Cost</span>${o.cost_total:.2f}</div>
<div><span>Max loss</span>${o.max_loss:.2f}</div><div><span>If right</span>+${o.profit_if_win:.2f}</div>
</div>
<div class="meta" style="margin-top:8px"><a href="{e(a.market.url)}">Open on Kalshi</a> · {e(a.market.ticker)}</div>
</div>"""


def _list(items, empty: str = "No qualifying trades found.") -> str:
    out, last = [], None
    for o, a in _order(items):
        g = a.game
        day = "Live now" if g and g.is_live else (g.start_time.astimezone(TZ).strftime("%A, %B %d") if g and g.start_time else "Date unknown")
        if day != last:
            out.append(f"<h2>{html.escape(day)}</h2>")
            last = day
        out.append(_card(o, a))
    return "\n".join(out) or f'<div class="card empty">{html.escape(empty)}</div>'


def _watch(pairs) -> str:
    """Big-club matches with their winner markets: model vs market, even when nothing qualifies."""
    e = html.escape
    events: dict[str, list] = {}
    for o, a in pairs:
        if a.market.sport != "soccer" or not _is_big(a) or not a.market.spec or a.market.spec.kind not in ("winner", "draw"):
            continue
        if a.market.spec.period != "full" or o.side != "yes":
            continue
        events.setdefault(a.market.event_ticker, []).append((o, a))
    cards = []
    for ev, rows in sorted(events.items(), key=lambda kv: (kv[1][0][1].game.start_time if kv[1][0][1].game and kv[1][0][1].game.start_time
                                                           else datetime.max.replace(tzinfo=TZ)))[:12]:
        a0 = rows[0][1]
        g = a0.game
        live = '<span class="badge"><span class="dot"></span>LIVE</span> ' if g and g.is_live else ""
        when = g.start_time.astimezone(TZ).strftime("%a %b %d, ") + _t(g.start_time) if g and g.start_time else ""
        lines = "".join(
            f'<div class="meta" style="margin-top:4px">{e(a.market.spec.describe())}</div>'
            f'{_bar("Our estimate", o.model_prob_raw, "us")}{_bar("Kalshi price", o.implied_prob, "mk")}'
            for o, a in rows)
        cards.append(f'<div class="card big"><div class="top"><div class="match">{live}{_matchup(a0)}</div>'
                     f'<div class="when">{e(when)}</div></div>{_score(g)}<div class="bars">{lines}</div>'
                     f'<div class="meta">No pick unless it also appears above. <a href="{e(a0.market.url)}">Kalshi</a></div></div>')
    return "\n".join(cards) or '<div class="card empty">No big-club matches listed on Kalshi right now.</div>'


def render(pairs: list[tuple[Opportunity, MarketAnalysis]], settings: Settings, storage: Storage,
           banner: str, scanned: int, priced: int, limit: int = 20) -> str:
    e = html.escape
    now = datetime.now(TZ).strftime("%a %b %d, %I:%M %p %Z")
    q = [(o, a) for o, a in pairs if o.qualifies]
    by = {k: [t for t in q if t[1].market.sport == k][:limit] for k in ("nfl", "ncaaf", "soccer")}
    all_items = _order([t for k in by for t in by[k]])[:30]
    live_n = sum(1 for _, a in all_items if a.game and a.game.is_live)
    big = [t for t in by["soccer"] if _is_big(t[1])]
    other = [t for t in by["soccer"] if not _is_big(t[1])]

    br = Bankroll(storage, settings.risk).snapshot("paper")
    rows = "".join(
        f"<tr><td>{e(p.contract or p.market_ticker)}</td><td>{p.side.upper()} x{p.contracts:g}</td><td>${p.entry_price:.2f}</td></tr>"
        for p in PositionStore(storage).open("paper"))
    total = max(br.cash + br.open_exposure, 0.01)
    paper = (f'<div class="card"><div class="bars">{_bar("Cash", br.cash / total, "us")}{_bar("At risk", br.open_exposure / total, "mk")}</div>'
             f'<div class="meta">Cash ${br.cash:.2f} · at risk ${br.open_exposure:.2f} · realized P/L ${br.realized_pnl:+.2f}</div>'
             + (f"<table>{rows}</table>" if rows else '<div class="meta">No open paper trades.</div>') + "</div>")

    tabs = [("all", "All", len(all_items)), ("nfl", "NFL", len(by["nfl"])), ("ncaaf", "College", len(by["ncaaf"])),
            ("soccer", "Soccer", len(by["soccer"]))]
    tab_html = "".join(f'<button class="tab" role="tab" data-t="{k}" aria-selected="false">{n}<span class="n">{c}</span></button>'
                       for k, n, c in tabs)
    panels = {
        "all": _list(all_items),
        "nfl": _list(by["nfl"]),
        "ncaaf": _list(by["ncaaf"]),
        "soccer": ("<h3>Big clubs</h3>" + _list(big, "No big-club picks right now.")
                   + "<h3>Big-club matches to watch</h3>" + _watch(pairs)
                   + "<h3>Other soccer picks</h3>" + _list(other)),
    }
    panel_html = "".join(f'<section class="panel" data-p="{k}">{v}</section>' for k, v in panels.items())

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kalshi Picks</title>
<style>{CSS}</style></head><body><main>
<h1>Kalshi Picks</h1>
<div class="meta">Updated {e(now)} · refreshes about every hour</div>
<div class="banner">{e(banner)}</div>
<div class="stats"><div class="stat"><b>{len(q)}</b><span>picks found</span></div>
<div class="stat"><b>{live_n}</b><span>live now</span></div>
<div class="stat"><b>{priced:,}</b><span>markets priced</span></div></div>
<div class="tabs" role="tablist">{tab_html}</div>
{panel_html}
<h2>Paper trading (fake money)</h2>
{paper}
<p class="meta">Estimates, not guarantees. Prices can change after this page was generated — check Kalshi before acting.
Only risk money set aside for entertainment.</p>
</main>
<script>
(function(){{
  var tabs=document.querySelectorAll('.tab'),panels=document.querySelectorAll('.panel');
  function show(k){{tabs.forEach(function(t){{t.setAttribute('aria-selected',t.dataset.t===k)}});
    panels.forEach(function(p){{p.classList.toggle('on',p.dataset.p===k)}});
    try{{localStorage.setItem('ksa-tab',k)}}catch(e){{}}}}
  tabs.forEach(function(t){{t.addEventListener('click',function(){{show(t.dataset.t)}})}});
  var k='all';try{{k=localStorage.getItem('ksa-tab')||'all'}}catch(e){{}}
  show(k);
}})();
</script></body></html>"""
