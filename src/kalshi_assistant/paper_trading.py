"""Paper trading: hypothetical fills at real executable prices, with fees, tracked to
settlement or hypothetical sale. Losing trades are always included in reports.

The same functions record *real* trades you placed yourself (account="real") so the
hold/sell engine can follow them; nothing here sends orders to Kalshi.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from .bankroll import Bankroll
from .calibration import brier, log_loss
from .config import Settings
from .fees import FeeModel
from .kalshi_client import KalshiClient
from .positions import Position, PositionStore
from .storage import Storage
from .value_engine import Opportunity


class TradeRejected(Exception):
    pass


@dataclass
class TradeResult:
    position: Position
    cost: float
    message: str


class PaperTrader:
    def __init__(self, storage: Storage, settings: Settings):
        self.s = storage
        self.settings = settings
        self.bankroll = Bankroll(storage, settings.risk)
        self.positions = PositionStore(storage)

    # ---------------------------------------------------------------- entries
    def open_from_opportunity(self, opp: Opportunity, contracts: float, account: str = "paper",
                              allow_add: bool = False) -> TradeResult:
        mk = opp.market
        return self.open_position(
            account=account, ticker=mk.ticker, side=opp.side, contracts=contracts, price=opp.price,
            fee_model=mk.fee_model, event_ticker=mk.event_ticker, series_ticker=mk.series_ticker,
            sport=mk.sport, competition=mk.competition, market_type=mk.spec.kind if mk.spec else None,
            contract=opp.contract_label, context=opp.context, model_prob=opp.prob,
            market_price=opp.price, reliability=opp.reliability, allow_add=allow_add)

    def open_position(self, account: str, ticker: str, side: str, contracts: float, price: float,
                      fee_model: FeeModel | None = None, fees: float | None = None, event_ticker: str | None = None,
                      series_ticker: str | None = None, sport: str | None = None, competition: str | None = None,
                      market_type: str | None = None, contract: str | None = None, context: str | None = None,
                      model_prob: float | None = None, market_price: float | None = None,
                      reliability: float | None = None, allow_add: bool = False, enforce_limits: bool = True,
                      notes: str | None = None) -> TradeResult:
        if side not in ("yes", "no"):
            raise TradeRejected("side must be 'yes' or 'no'")
        if contracts <= 0 or not 0 < price < 1:
            raise TradeRejected("contracts must be > 0 and price strictly between $0 and $1")
        fm = fee_model or FeeModel(self.settings.value.taker_fee_rate)
        fee = fm.taker_fee(contracts, price) if fees is None else fees
        cost = round(contracts * price + fee, 4)
        with self.s.tx() as c:
            # duplicate protection inside the write lock
            if not allow_add and self.positions.find_open(account, ticker, side):
                raise TradeRejected(f"An open {account} position on {ticker} {side.upper()} already exists (use --add to add).")
            if enforce_limits:
                problems = self.bankroll.check_new_position(account, event_ticker, cost)
                if problems:
                    raise TradeRejected("; ".join(problems))
            p = Position(
                id=PositionStore.new_id(), mode=account, market_ticker=ticker, event_ticker=event_ticker,
                series_ticker=series_ticker, sport=sport, competition=competition, market_type=market_type,
                contract=contract or ticker, side=side, contracts=contracts, entry_price=price, entry_fees=fee,
                opened_at=time.time(), context=context, model_prob=model_prob,
                model_version=self.settings.model.version, notes=notes)
            self.positions.insert(p, c)
            c.execute("INSERT INTO ledger(ts,account,kind,amount,position_id,note) VALUES(?,?,?,?,?,?)",
                      (time.time(), account, "buy", -cost, p.id, f"{side} x{contracts:g} @ {price:.2f}"))
            if model_prob is not None:
                c.execute("""INSERT INTO predictions(ts,model_version,sport,competition,market_ticker,market_type,side,context,
                             model_prob,market_price,reliability,position_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (time.time(), self.settings.model.version, sport, competition, ticker, market_type, side,
                           context, model_prob, market_price, reliability, p.id))
        self.s.audit("open_position", {"account": account, "ticker": ticker, "side": side, "contracts": contracts,
                                       "price": price, "fee": fee})
        return TradeResult(p, cost, f"Opened {account} position {p.id}: {side.upper()} x{contracts:g} {ticker} @ ${price:.2f} "
                                    f"+ fees ${fee:.2f} = ${cost:.2f} (max loss ${cost:.2f})")

    # ------------------------------------------------------------------ exits
    def sell(self, position_id: str, price: float, contracts: float | None = None,
             fee_model: FeeModel | None = None, reason: str = "manual sell", fees: float | None = None) -> TradeResult:
        p = self.positions.get(position_id)
        if not p or p.status != "open":
            raise TradeRejected(f"No open position {position_id}")
        n = p.contracts if contracts is None else min(contracts, p.contracts)
        if n <= 0 or not 0 < price < 1:
            raise TradeRejected("invalid sell size or price")
        fm = fee_model or FeeModel(self.settings.value.taker_fee_rate)
        fee = fm.taker_fee(n, price) if fees is None else fees
        proceeds = round(n * price - fee, 4)
        entry_share = n / p.contracts
        realized = round(proceeds - (p.contracts * p.entry_price + p.entry_fees) * entry_share, 4)
        with self.s.tx() as c:
            c.execute("INSERT INTO ledger(ts,account,kind,amount,position_id,note) VALUES(?,?,?,?,?,?)",
                      (time.time(), p.mode, "sell", proceeds, p.id, reason))
            if n >= p.contracts - 1e-9:
                self.positions.close(p.id, "closed", price, fee, realized, reason, c)
            else:
                # split: close the sold part as its own record, keep the remainder open
                part = Position(**{**p.__dict__, "id": PositionStore.new_id(), "contracts": n,
                                   "entry_fees": round(p.entry_fees * entry_share, 4), "status": "open"})
                self.positions.insert(part, c)
                self.positions.close(part.id, "closed", price, fee, realized, reason + " (partial)", c)
                self.positions.reduce(p.id, p.contracts - n, round(p.entry_fees * (1 - entry_share), 4), c)
        self.s.record_decision("exit", "SELL", ticker=p.market_ticker, position_id=p.id,
                               detail={"price": price, "contracts": n, "realized": realized})
        return TradeResult(p, proceeds, f"Sold {n:g} x {p.market_ticker} {p.side.upper()} @ ${price:.2f}; "
                                        f"proceeds ${proceeds:.2f} after fee ${fee:.2f}; realized P/L ${realized:+.2f}")

    def settle_position(self, p: Position, result: str) -> float:
        """result: 'yes' or 'no' (Kalshi market result)."""
        won = (result == p.side)
        payout = p.contracts * (1.0 if won else 0.0)
        realized = round(payout - p.cost_basis, 4)
        with self.s.tx() as c:
            c.execute("INSERT INTO ledger(ts,account,kind,amount,position_id,note) VALUES(?,?,?,?,?,?)",
                      (time.time(), p.mode, "settle", payout, p.id, f"market result {result}"))
            self.positions.close(p.id, "settled", 1.0 if won else 0.0, 0.0, realized, f"settled {result}", c)
        return realized

    def settle_all(self, client: KalshiClient) -> list[str]:
        msgs = []
        for p in self.positions.open():
            try:
                m = client.get_market(p.market_ticker)
            except Exception as exc:  # noqa: BLE001
                msgs.append(f"{p.id}: could not check market ({exc})")
                continue
            result = str(m.get("result") or "")
            if result in ("yes", "no") and m.get("status") in ("determined", "finalized", "settled", "amended"):
                pnl = self.settle_position(p, result)
                msgs.append(f"{p.id} {p.market_ticker}: settled {result.upper()} -> realized ${pnl:+.2f}")
        self.record_outcomes(client)
        return msgs

    def record_outcomes(self, client: KalshiClient) -> int:
        rows = self.s.q("SELECT DISTINCT market_ticker FROM predictions WHERE outcome IS NULL")
        n = 0
        for r in rows:
            try:
                m = client.get_market(r["market_ticker"])
            except Exception:  # noqa: BLE001
                continue
            res = str(m.get("result") or "")
            if res in ("yes", "no"):
                self.s.conn.execute(
                    "UPDATE predictions SET outcome = CASE WHEN side=? THEN 1 ELSE 0 END, settled_at=? WHERE market_ticker=? AND outcome IS NULL",
                    (res, time.time(), r["market_ticker"]))
                n += 1
        return n

    # ------------------------------------------------------------ performance
    def performance(self, account: str = "paper") -> str:
        ps = self.positions.all(account, limit=100000)
        closed = [p for p in ps if p.status in ("closed", "settled")]
        opened = [p for p in ps if p.status == "open"]
        lines = [f"Performance - {account.upper()} account (all trades, including losses)"]
        if not ps:
            return lines[0] + "\n  No trades recorded yet."
        wins = [p for p in closed if (p.realized_pnl or 0) > 0]
        net = sum(p.realized_pnl or 0 for p in closed)
        staked = sum(p.cost_basis for p in closed)
        snap = self.bankroll.snapshot(account)
        start = self.settings.risk.starting_bankroll
        lines += [
            f"  Trades: {len(ps)} ({len(closed)} closed, {len(opened)} open)",
            f"  Win rate (closed): {len(wins) / len(closed):.1%}" if closed else "  Win rate: n/a",
            f"  Net realized P/L: ${net:+.2f} on ${staked:.2f} staked" + (f" (ROI {net / staked:+.1%})" if staked else ""),
            f"  Return on starting bankroll (${start:.2f}): {net / start:+.1%}",
            f"  Fees paid: ${snap.fees_paid:.2f}",
        ]
        preds = [dict(r) for r in self.s.q("SELECT * FROM predictions WHERE position_id IN (SELECT id FROM positions WHERE mode=?)", (account,))]
        done = [r for r in preds if r["outcome"] is not None]
        if preds:
            avg_ev = sum(r["model_prob"] - (r["market_price"] or 0) for r in preds) / len(preds)
            lines.append(f"  Avg estimated edge at entry: {avg_ev:+.3f} per $1 contract (estimate, before fees)")
        if closed:
            lines.append(f"  Avg realized result per trade: ${net / len(closed):+.3f}")
        if done:
            p = [r["model_prob"] for r in done]
            o = [r["outcome"] for r in done]
            m = [r["market_price"] for r in done]
            lines.append(f"  Model Brier {brier(p, o):.4f} / log loss {log_loss(p, o):.4f} vs market-implied Brier {brier(m, o):.4f} (n={len(done)})")
            if len(done) < 100:
                lines.append("  WARNING: fewer than 100 settled predictions - results are mostly noise so far.")
        for label, keyf in (("sport", lambda p: p.sport), ("market type", lambda p: p.market_type),
                            ("timing", lambda p: p.context), ("model version", lambda p: p.model_version)):
            groups: dict = defaultdict(list)
            for p in closed:
                groups[keyf(p) or "?"].append(p)
            if groups:
                parts = [f"{k}: n={len(v)} P/L ${sum(x.realized_pnl or 0 for x in v):+.2f}" for k, v in sorted(groups.items())]
                lines.append(f"  By {label}: " + " | ".join(parts))
        lines.append("  Reminder: no strategy is proven profitable until a large sample with realistic fees says so.")
        return "\n".join(lines)


def validation_status(storage: Storage, min_settled: int = 100) -> tuple[bool, str]:
    """The model counts as validated only after enough settled paper predictions AND a Brier score
    at least as good as the market's own prices on the same contracts."""
    rows = [dict(r) for r in storage.q("SELECT model_prob, market_price, outcome FROM predictions WHERE outcome IS NOT NULL AND market_price IS NOT NULL")]
    n = len(rows)
    if n < min_settled:
        return False, f"MODEL NOT YET VALIDATED: {n}/{min_settled} settled paper predictions. Treat picks as experiments, not advice."
    mb = brier([r["model_prob"] for r in rows], [r["outcome"] for r in rows])
    kb = brier([r["market_price"] for r in rows], [r["outcome"] for r in rows])
    if mb > kb:
        return False, f"MODEL UNDERPERFORMING THE MARKET (Brier {mb:.4f} vs market {kb:.4f}, n={n}). Do not trade real money on these picks."
    return True, f"Model validated on {n} settled predictions (Brier {mb:.4f} vs market {kb:.4f})."
