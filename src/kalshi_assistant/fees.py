"""Kalshi trading-fee calculations.

Source: Kalshi fee schedule (https://kalshi.com/docs/kalshi-fee-schedule.pdf):
    general taker fee = round_up(0.07 * C * P * (1 - P))   to the next cent
where C = contracts, P = price in dollars. There is no settlement fee.
Resting (maker) orders are free on most markets; series with
fee_type 'quadratic_with_maker_fees' charge makers 0.0175 * C * P * (1-P)
('quadratic_with_combo_maker_fees' uses a 0.5x maker multiplier instead of 0.25x).

Each series also reports a `fee_multiplier`. Kalshi does not document its scale
precisely, so we treat it as a multiplier on the base coefficient (1.0 = normal).
Always verify against https://kalshi.com/fee-schedule — fees change over time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FeeModel:
    taker_rate: float = 0.07
    maker_rate: float = 0.0175
    fee_type: str = "quadratic"
    multiplier: float = 1.0

    @classmethod
    def from_series(cls, series: dict | None, taker_rate: float = 0.07, maker_rate: float = 0.0175) -> "FeeModel":
        if not series:
            return cls(taker_rate, maker_rate)
        mult = series.get("fee_multiplier")
        try:
            mult = float(mult) if mult is not None else 1.0
        except (TypeError, ValueError):
            mult = 1.0
        if mult <= 0:
            mult = 1.0
        return cls(taker_rate, maker_rate, str(series.get("fee_type") or "quadratic"), mult)

    def taker_fee(self, contracts: float, price: float) -> float:
        """Fee in dollars for an order that immediately matches (taker)."""
        if self.fee_type == "flat":
            # Flat-fee products (e.g. index markets) are not sports; use general formula conservatively.
            pass
        return quadratic_fee(contracts, price, self.taker_rate * self.multiplier)

    def maker_fee(self, contracts: float, price: float) -> float:
        if self.fee_type == "quadratic_with_maker_fees":
            return quadratic_fee(contracts, price, self.maker_rate * self.multiplier)
        if self.fee_type == "quadratic_with_combo_maker_fees":
            return quadratic_fee(contracts, price, self.taker_rate * 0.5 * self.multiplier)
        return 0.0


def quadratic_fee(contracts: float, price: float, rate: float = 0.07) -> float:
    if contracts <= 0:
        return 0.0
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be strictly between 0 and 1 dollars, got {price}")
    raw = rate * contracts * price * (1.0 - price)
    # round up to the next cent; subtract a tiny epsilon to avoid float artifacts (e.g. 1.7500000001)
    return math.ceil(raw * 100 - 1e-9) / 100.0


def fee_per_contract(contracts: float, price: float, fee_model: FeeModel | None = None) -> float:
    fm = fee_model or FeeModel()
    if contracts <= 0:
        return 0.0
    return fm.taker_fee(contracts, price) / contracts
