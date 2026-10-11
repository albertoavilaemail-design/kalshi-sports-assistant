"""Real order execution - INTENTIONALLY NOT IMPLEMENTED in v1.

The project is recommendation-only and paper-trading-first. This module defines the
guard rails a future, explicitly authorized version must satisfy before any order
could be sent. Calling `place_order` always raises.

Required before enabling (checklist, enforced by `preflight`):
  * execution.enabled = true AND KSA_EXECUTION_ENABLED=true AND kill_switch = false
  * authenticated Kalshi API key
  * manual approval per order (default)
  * max order size, max total exposure, daily loss limit (RiskConfig)
  * price limit (limit orders only, never market orders)
  * liquidity check and market-status verification immediately before sending
  * duplicate-order prevention (client_order_id) and order-status reconciliation
  * audit log of every decision; credentials never logged
"""
from __future__ import annotations

from .config import Settings


class ExecutionDisabled(RuntimeError):
    pass


def preflight(settings: Settings, authenticated: bool) -> list[str]:
    problems = []
    if not settings.execution.enabled:
        problems.append("execution.enabled is false")
    if settings.execution.kill_switch:
        problems.append("kill switch is ON")
    if not authenticated:
        problems.append("no authenticated Kalshi API key")
    if not settings.execution.require_manual_approval:
        problems.append("manual approval must remain on until you explicitly decide otherwise")
    problems.append("order placement code is not implemented in this version")
    return problems


def place_order(*_args, **_kwargs):
    raise ExecutionDisabled("Real order placement is disabled: this version is recommendation-only / paper trading.")
