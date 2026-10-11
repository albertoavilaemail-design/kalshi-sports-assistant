"""Entertainment bankroll tracking and risk limits.

Two separate accounts live in the ledger:
  * "paper" - hypothetical money for paper trading (starts at risk.starting_bankroll)
  * "real"  - your own record of real money set aside for Kalshi; it is NOT connected
              to your Kalshi or bank account. Update it with `set-bankroll`.

Cash moves through the ledger: buying debits cost + fees, selling/settlement credits
proceeds. Sizing shrinks automatically as the bankroll shrinks; it never increases
stakes to chase losses.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import RiskConfig
from .positions import PositionStore
from .storage import Storage


@dataclass
class BankrollSnapshot:
    account: str
    cash: float
    reserved: float
    available: float
    open_exposure: float
    equity: float
    realized_pnl: float
    fees_paid: float
    daily_pnl: float
    weekly_pnl: float
    open_positions: int

    def render(self) -> str:
        return "\n".join([
            f"Account: {self.account.upper()}" + ("  (paper money)" if self.account == "paper" else "  (your tracked entertainment funds)"),
            f"  Cash balance:        ${self.cash:8.2f}",
            f"  Reserved:            ${self.reserved:8.2f}",
            f"  Available to trade:  ${self.available:8.2f}",
            f"  Open exposure (max loss of {self.open_positions} open positions): ${self.open_exposure:.2f}",
            f"  Equity (cash + open cost basis): ${self.equity:.2f}",
            f"  Realized P/L:        ${self.realized_pnl:+8.2f}   fees paid: ${self.fees_paid:.2f}",
            f"  Today P/L:           ${self.daily_pnl:+8.2f}   7-day P/L: ${self.weekly_pnl:+.2f}",
        ])


class Bankroll:
    def __init__(self, storage: Storage, risk: RiskConfig):
        self.s = storage
        self.risk = risk
        self.positions = PositionStore(storage)

    def ensure_initialized(self, account: str) -> None:
        if not self.s.q("SELECT 1 FROM ledger WHERE account=? LIMIT 1", (account,)):
            self.s.conn.execute("INSERT INTO ledger(ts,account,kind,amount,note) VALUES(?,?,?,?,?)",
                                (time.time(), account, "initial", self.risk.starting_bankroll, "starting bankroll"))

    def cash(self, account: str) -> float:
        self.ensure_initialized(account)
        row = self.s.conn.execute("SELECT COALESCE(SUM(amount),0) AS c FROM ledger WHERE account=?", (account,)).fetchone()
        return round(row["c"], 4)

    def reserved(self, account: str) -> float:
        return float(self.s.kv_get(f"reserved:{account}", 0.0))

    def set_reserved(self, account: str, amount: float) -> None:
        if amount < 0:
            raise ValueError("reserved amount cannot be negative")
        self.s.kv_set(f"reserved:{account}", amount)

    def set_bankroll(self, account: str, amount: float, note: str = "manual update") -> float:
        """Set the CASH balance (money not currently in open positions) to `amount`."""
        if amount < 0:
            raise ValueError("bankroll cannot be negative")
        delta = round(amount - self.cash(account), 4)
        if delta:
            self.s.conn.execute("INSERT INTO ledger(ts,account,kind,amount,note) VALUES(?,?,?,?,?)",
                                (time.time(), account, "adjustment", delta, note))
        self.s.audit("set_bankroll", {"account": account, "amount": amount})
        return delta

    def available(self, account: str) -> float:
        return max(round(self.cash(account) - self.reserved(account), 4), 0.0)

    def open_exposure(self, account: str, event_ticker: str | None = None) -> float:
        ps = [p for p in self.positions.open(account) if event_ticker is None or p.event_ticker == event_ticker]
        return round(sum(p.cost_basis for p in ps), 4)

    def realized_since(self, account: str, since_ts: float) -> float:
        row = self.s.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) AS r FROM positions WHERE mode=? AND closed_at>=? AND realized_pnl IS NOT NULL",
            (account, since_ts)).fetchone()
        return round(row["r"], 4)

    @staticmethod
    def day_start() -> float:
        now = datetime.now(timezone.utc).astimezone()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def snapshot(self, account: str) -> BankrollSnapshot:
        cash = self.cash(account)
        exp = self.open_exposure(account)
        fees = self.s.conn.execute(
            "SELECT COALESCE(SUM(entry_fees),0)+COALESCE(SUM(exit_fees),0) AS f FROM positions WHERE mode=?",
            (account,)).fetchone()["f"]
        return BankrollSnapshot(
            account, cash, self.reserved(account), self.available(account), exp, round(cash + exp, 4),
            self.realized_since(account, 0), round(fees, 4), self.realized_since(account, self.day_start()),
            self.realized_since(account, time.time() - 7 * 86400), len(self.positions.open(account)))

    # ------------------------------------------------------------ risk checks
    def check_new_position(self, account: str, event_ticker: str | None, cost: float) -> list[str]:
        """Returns a list of violated limits (empty = allowed)."""
        problems: list[str] = []
        avail = self.available(account)
        if cost > avail + 1e-9:
            problems.append(f"cost ${cost:.2f} exceeds available ${avail:.2f}")
        if cost > self.risk.max_position_dollars + 0.05:
            problems.append(f"cost ${cost:.2f} exceeds max position ${self.risk.max_position_dollars:.2f}")
        equity = self.cash(account) + self.open_exposure(account)
        total_cap = self.risk.max_total_exposure_pct * equity
        if self.open_exposure(account) + cost > total_cap + 1e-9:
            problems.append(f"total exposure would exceed {self.risk.max_total_exposure_pct:.0%} of equity (${total_cap:.2f})")
        if event_ticker:
            ev = self.open_exposure(account, event_ticker)
            if ev + cost > self.risk.max_event_exposure_dollars + 1e-9:
                problems.append(f"exposure on this game would be ${ev + cost:.2f} (> ${self.risk.max_event_exposure_dollars:.2f} correlated-risk cap)")
        d = self.realized_since(account, self.day_start())
        if d <= -self.risk.daily_loss_limit_dollars:
            problems.append(f"daily loss limit reached (${d:.2f})")
        w = self.realized_since(account, time.time() - 7 * 86400)
        if w <= -self.risk.weekly_loss_limit_dollars:
            problems.append(f"weekly loss limit reached (${w:.2f})")
        return problems

    def correlated_positions(self, account: str, event_ticker: str | None) -> list[str]:
        if not event_ticker:
            return []
        return [f"{p.contract} ({p.side.upper()} x{p.contracts:g})" for p in self.positions.open(account)
                if p.event_ticker == event_ticker]

    def suggest_contracts(self, account: str, prob: float, price: float, fee_per_contract: float,
                          budget: float | None = None) -> tuple[float, str]:
        """Fractional-Kelly size clamped to [min, max] position dollars and the available bankroll."""
        p_eff = price + fee_per_contract
        if p_eff >= 1 or prob <= p_eff:
            return 0, "no positive edge after fees"
        kelly = (prob - p_eff) / (1 - p_eff)
        equity = self.cash(account) + self.open_exposure(account)
        stake = self.risk.kelly_fraction * kelly * equity
        stake = min(max(stake, self.risk.min_position_dollars), self.risk.max_position_dollars)
        cap = self.available(account)
        if budget is not None:
            cap = min(cap, budget)
        stake = min(stake, cap)
        if self.risk.allow_fractional_contracts:
            n = math.floor(stake / p_eff * 100) / 100
        else:
            n = math.floor(stake / p_eff + 1e-9)
            if n == 0 and p_eff <= min(self.risk.max_position_dollars, cap) + 1e-9:
                n = 1
        if n <= 0:
            return 0, f"one contract (${p_eff:.2f}) exceeds the allowed stake (${stake:.2f})"
        return n, f"fractional Kelly {self.risk.kelly_fraction:g}x on equity ${equity:.2f}, clamped to ${self.risk.min_position_dollars:.2f}-${self.risk.max_position_dollars:.2f}"
