"""Two fast checks on whether the picks deserve trust.

1. Price-movement check ("closing line value"): every pick is recorded with the Kalshi
   midpoint when it was first flagged. On later scans, until kickoff, the midpoint for the
   same side is updated. If the market keeps moving TOWARD our side before the game starts,
   the model is likely seeing something real; if it moves away, the "edge" was probably
   model error. This gives evidence within days, long before 100 paper trades settle.

2. Injury filter: for NFL / college picks, the game's injury report (ESPN) is checked.
   If a quarterback is listed Out, Doubtful or on injured reserve, the pick is removed,
   because the rating model cannot account for him missing the game.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .storage import Storage

QB_OUT = ("out", "doubtful", "injured reserve", "ir")


def _mid(mk, side: str) -> float | None:
    if mk.yes_bid is None or mk.yes_ask is None:
        return None
    m = (mk.yes_bid + mk.yes_ask) / 2
    return m if side == "yes" else 1 - m


def apply_injury_filter(analyzer, pairs) -> int:
    """Disqualify pregame football picks whose game has a QB listed out. Returns # removed."""
    cache: dict[str, dict] = {}
    removed = 0
    for o, a in pairs:
        g = a.game
        if not o.qualifies or not g or g.sport not in ("nfl", "ncaaf"):
            continue
        if g.game_id not in cache:
            try:
                cache[g.game_id] = analyzer.provider.game_detail(g.sport, g.competition, g.game_id)
            except Exception:  # noqa: BLE001
                cache[g.game_id] = {"available": False}
        det = cache[g.game_id]
        if not det.get("available"):
            o.notes.append("Injury report unavailable - check quarterback status yourself.")
            continue
        outs = [x for x in det.get("injuries") or []
                if (x.get("position") or "").upper() == "QB" and (x.get("status") or "").lower() in QB_OUT]
        if outs:
            names = ", ".join(f"{x['player']} ({x['team']}, {x['status']})" for x in outs[:3])
            o.qualifies = False
            o.fail_reasons.append(f"quarterback listed out: {names} - the model can't price that")
            removed += 1
    return removed


def record_picks(storage: Storage, pairs) -> None:
    """Remember the first price of each qualifying pick; keep updating the price until kickoff."""
    now = time.time()
    for o, a in pairs:
        mk = a.market
        mid = _mid(mk, o.side)
        started = a.context != "pregame"
        row = storage.conn.execute("SELECT started FROM picks WHERE market_ticker=? AND side=?",
                                   (mk.ticker, o.side)).fetchone()
        if row is None:
            if o.qualifies and not started and mid is not None:
                storage.conn.execute(
                    "INSERT INTO picks(market_ticker,side,sport,market_type,label,first_ts,first_price,first_mid,model_prob,last_ts,last_mid,kickoff) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mk.ticker, o.side, mk.sport, mk.spec.kind if mk.spec else None, o.contract_label, now, o.price, mid,
                     o.model_prob_raw, now, mid, a.game.start_time.isoformat() if a.game and a.game.start_time else None))
        elif not row["started"]:
            if started:
                storage.conn.execute("UPDATE picks SET started=1 WHERE market_ticker=? AND side=?", (mk.ticker, o.side))
            elif mid is not None:
                storage.conn.execute("UPDATE picks SET last_ts=?, last_mid=? WHERE market_ticker=? AND side=?",
                                     (now, mid, mk.ticker, o.side))


@dataclass
class PriceCheck:
    n: int
    n_closed: int
    avg_move: float | None
    pct_toward: float | None
    verdict: str


def price_check(storage: Storage, min_n: int = 30) -> PriceCheck:
    rows = storage.q("SELECT first_mid, last_mid, started, last_ts, first_ts FROM picks WHERE first_mid IS NOT NULL AND last_mid IS NOT NULL")
    # only count picks that have had at least an hour for the price to move
    rows = [r for r in rows if r["last_ts"] - r["first_ts"] >= 3600 or r["started"]]
    moves = [r["last_mid"] - r["first_mid"] for r in rows]
    closed = sum(1 for r in rows if r["started"])
    if not moves:
        return PriceCheck(0, 0, None, None, "Price check: no picks old enough yet. Check back after a few hourly scans.")
    avg = sum(moves) / len(moves)
    toward = sum(1 for m in moves if m > 0.005) / len(moves)
    away = sum(1 for m in moves if m < -0.005) / len(moves)
    if len(moves) < min_n:
        verdict = f"Price check: {len(moves)}/{min_n} picks tracked so far - too early to judge."
    elif avg > 0.005 and toward > away:
        verdict = (f"Price check: PROMISING - prices moved toward our picks {toward:.0%} of the time "
                   f"(avg {avg * 100:+.1f}c) across {len(moves)} picks.")
    else:
        verdict = (f"Price check: NOT CONVINCING - prices moved toward our picks only {toward:.0%} of the time "
                   f"(away {away:.0%}, avg {avg * 100:+.1f}c) across {len(moves)} picks. The 'edges' look like model error.")
    return PriceCheck(len(moves), closed, avg, toward, verdict)
