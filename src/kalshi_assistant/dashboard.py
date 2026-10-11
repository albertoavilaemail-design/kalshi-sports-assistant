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

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#5d6470;--line:#e3e6eb;--good:#13795b;--warn:#a15c00;--accent:#2d5bd7}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--ink:#eceef2;--mute:#9aa2ae;--line:#2a2f38;--good:#4cc79a;--warn:#f0b35a;--accent:#7ea2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0}h2{font-size:15px;margin:22px 0 8px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.meta{color:var(--mute);font-size:13px}
.banner{background:color-mix(in srgb,var(--warn) 14%,var(--card));border:1px solid var(--warn);color:var(--ink);padding:10px 12px;border-radius:10px;margin:12px 0;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0}
.top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.match{font-weight:600}.when{color:var(--mute);font-size:13px;white-space:nowrap}
h2+.card{margin-top:4px}
.bet{margin:6px 0 8px;font-size:16px}.bet b{color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;font-size:13px}
.grid div span{display:block;color:var(--mute);font-size:11px;text-transform:uppercase}
.live{color:var(--good);font-size:13px;margin-top:6px}
.empty{color:var(--mute);padding:16px;text-align:center}
a{color:var(--accent)}table{width:100%;border-collapse:collapse;font-size:13px}td{padding:6px 4px;border-top:1px solid var(--line)}
"""


def _t(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%I:%M %p").lstrip("0") if dt else "time n/a"


def render(pairs: list[tuple[Opportunity, MarketAnalysis]], settings: Settings, storage: Storage,
           banner: str, scanned: int, priced: int, limit: int = 20) -> str:
    e = html.escape
    now = datetime.now(TZ).strftime("%a %b %d, %I:%M %p %Z")
    picks = [(o, a) for o, a in pairs if o.qualifies][:limit]
    far = datetime.max.replace(tzinfo=TZ)
    # Live games first, then soonest kickoff first; day headers between dates.
    picks.sort(key=lambda t: (0 if t[1].game and t[1].game.is_live else 1,
                              t[1].game.start_time if t[1].game and t[1].game.start_time else far))
    cards = []
    last_day = None
    for o, a in picks:
        g = a.game
        day = "Live now" if g and g.is_live else (g.start_time.astimezone(TZ).strftime("%A, %B %d") if g and g.start_time else "Date unknown")
        if day != last_day:
            cards.append(f"<h2>{e(day)}</h2>")
            last_day = day
        what = a.market.spec.describe() if a.market.spec else a.market.title
        live = ""
        if g and g.is_live:
            live = f'<div class="live">LIVE: {e(g.score_line())}</div>'
        cards.append(f"""<div class="card">
<div class="top"><div class="match">{e(a.market.event_title)}</div><div class="when">{e(_t(g.start_time if g else None))}</div></div>
<div class="meta">{e(a.market.sport.upper())} · {e(a.market.competition)} · {e(o.context)}</div>
<div class="bet">Buy <b>{o.side.upper()}</b> on “{e(what)}” at <b>${o.price:.2f}</b></div>
<div class="grid">
<div><span>Est. chance</span>{o.prob:.0%}</div><div><span>Price implies</span>{o.implied_prob:.0%}</div><div><span>Edge</span>{o.edge:+.1%}</div>
<div><span>Size</span>{o.contracts:g} contract(s)</div><div><span>Max loss</span>${o.max_loss:.2f}</div><div><span>Profit if right</span>${o.profit_if_win:.2f}</div>
</div>{live}
<div class="meta" style="margin-top:6px"><a href="{e(a.market.url)}">Open on Kalshi</a> · {e(a.market.ticker)}</div>
</div>""")
    body = "\n".join(cards) or '<div class="card empty">No qualifying trades found.</div>'

    br = Bankroll(storage, settings.risk).snapshot("paper")
    rows = "".join(
        f"<tr><td>{e(p.contract or p.market_ticker)}</td><td>{p.side.upper()} x{p.contracts:g}</td><td>${p.entry_price:.2f}</td></tr>"
        for p in PositionStore(storage).open("paper"))
    paper = (f'<div class="card"><div>Paper cash <b>${br.cash:.2f}</b> · open risk ${br.open_exposure:.2f} · '
             f'realized P/L ${br.realized_pnl:+.2f}</div>'
             + (f"<table>{rows}</table>" if rows else '<div class="meta">No open paper trades.</div>') + "</div>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kalshi Picks</title>
<style>{CSS}</style></head><body><main>
<h1>Kalshi Picks</h1>
<div class="meta">Updated {e(now)} · {scanned} markets scanned, {priced} priced · refreshes about every hour</div>
<div class="banner">{e(banner)}</div>
<h2 style="margin-top:14px">Recommended now ({len(picks)})</h2>
{body}
<h2>Paper trading (fake money)</h2>
{paper}
<p class="meta">Estimates, not guarantees. Prices can change after this page was generated — check Kalshi before acting.
Only risk money set aside for entertainment.</p>
</main></body></html>"""
