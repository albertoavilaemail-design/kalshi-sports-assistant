"""Position records (paper trades and manually tracked real trades)."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, fields
from typing import Any

from .storage import Storage


@dataclass
class Position:
    id: str
    mode: str  # paper | real  (real = a trade you placed yourself on Kalshi and asked us to track)
    market_ticker: str
    event_ticker: str | None
    series_ticker: str | None
    sport: str | None
    competition: str | None
    market_type: str | None
    contract: str | None
    side: str
    contracts: float
    entry_price: float
    entry_fees: float
    opened_at: float
    context: str | None = None
    model_prob: float | None = None
    model_version: str | None = None
    status: str = "open"  # open | closed | settled
    exit_price: float | None = None
    exit_fees: float | None = None
    closed_at: float | None = None
    realized_pnl: float | None = None
    close_reason: str | None = None
    notes: str | None = None

    @property
    def cost_basis(self) -> float:
        return round(self.contracts * self.entry_price + self.entry_fees, 4)

    @property
    def max_payout(self) -> float:
        return self.contracts * 1.0


class PositionStore:
    def __init__(self, storage: Storage):
        self.s = storage

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:10]

    def insert(self, p: Position, conn=None) -> None:
        c = conn or self.s.conn
        cols = [f.name for f in fields(Position)]
        c.execute(f"INSERT INTO positions({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  tuple(getattr(p, k) for k in cols))

    def get(self, pid: str) -> Position | None:
        rows = self.s.q("SELECT * FROM positions WHERE id=? OR id LIKE ?", (pid, pid + "%"))
        return _row(rows[0]) if len(rows) == 1 else None

    def open(self, mode: str | None = None) -> list[Position]:
        if mode:
            return [_row(r) for r in self.s.q("SELECT * FROM positions WHERE status='open' AND mode=? ORDER BY opened_at", (mode,))]
        return [_row(r) for r in self.s.q("SELECT * FROM positions WHERE status='open' ORDER BY opened_at")]

    def all(self, mode: str | None = None, limit: int = 500) -> list[Position]:
        if mode:
            rows = self.s.q("SELECT * FROM positions WHERE mode=? ORDER BY opened_at DESC LIMIT ?", (mode, limit))
        else:
            rows = self.s.q("SELECT * FROM positions ORDER BY opened_at DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows]

    def find_open(self, mode: str, ticker: str, side: str) -> Position | None:
        rows = self.s.q("SELECT * FROM positions WHERE status='open' AND mode=? AND market_ticker=? AND side=?",
                        (mode, ticker, side))
        return _row(rows[0]) if rows else None

    def close(self, pid: str, status: str, exit_price: float, exit_fees: float, realized: float, reason: str,
              conn=None) -> None:
        c = conn or self.s.conn
        c.execute("UPDATE positions SET status=?, exit_price=?, exit_fees=?, closed_at=?, realized_pnl=?, close_reason=? WHERE id=?",
                  (status, exit_price, exit_fees, time.time(), realized, reason, pid))

    def reduce(self, pid: str, new_contracts: float, new_entry_fees: float, conn=None) -> None:
        c = conn or self.s.conn
        c.execute("UPDATE positions SET contracts=?, entry_fees=? WHERE id=?", (new_contracts, new_entry_fees, pid))


def _row(r: Any) -> Position:
    return Position(**{k: r[k] for k in r.keys()})
