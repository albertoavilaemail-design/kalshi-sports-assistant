"""Configuration loading: YAML file + environment variables (secrets only from env).

Precedence (highest first): environment variables > YAML file > built-in defaults.
Secrets (API keys, tokens) are NEVER read from the YAML file so they cannot be committed.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

KALSHI_PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"


@dataclass
class KalshiConfig:
    environment: str = "prod"  # "prod" or "demo"
    base_url: str = ""  # derived from environment when empty
    timeout_seconds: float = 15.0
    max_retries: int = 4
    min_request_interval_seconds: float = 0.12  # client-side throttle (~8 req/s)
    stale_after_seconds: int = 120  # price data older than this is "stale"
    # Secrets (env only)
    api_key_id: str = ""
    private_key_pem: str = ""
    private_key_path: str = ""


@dataclass
class RiskConfig:
    starting_bankroll: float = 25.0
    min_position_dollars: float = 0.50
    max_position_dollars: float = 1.00
    kelly_fraction: float = 0.25  # fraction of full Kelly used to size within [min, max]
    max_total_exposure_pct: float = 0.40  # of current bankroll
    max_event_exposure_dollars: float = 2.00  # correlated positions on one game
    daily_loss_limit_dollars: float = 5.00
    weekly_loss_limit_dollars: float = 10.00
    allow_fractional_contracts: bool = False


@dataclass
class ValueConfig:
    min_edge: float = 0.04  # model prob minus executable price
    min_net_ev_per_dollar: float = 0.05  # net EV / total cost
    max_spread: float = 0.08  # yes_ask - yes_bid
    min_depth_contracts: float = 5.0  # contracts available at the ask
    min_reliability: float = 0.45
    slippage_per_contract: float = 0.0  # extra execution cost assumption, dollars
    # Final probability = model_weight * model + (1 - model_weight) * market mid.
    # Simple rating models are usually less accurate than liquid markets; shrinking toward
    # the market cuts false "edges". Raise only if backtests/paper results justify it.
    model_weight: float = 0.6
    taker_fee_rate: float = 0.07  # Kalshi general schedule coefficient
    maker_fee_rate: float = 0.0175
    max_report: int = 10


@dataclass
class LiveConfig:
    interval_seconds: int = 60
    prob_change_alert: float = 0.06
    price_move_alert: float = 0.08
    momentum_window_minutes: int = 10
    # Momentum is REPORTED by default but does not change probabilities (weight 0).
    # Raise only after paper-trading evidence shows it helps.
    momentum_weight: float = 0.0
    hold_sell_margin: float = 0.01  # $/contract difference needed to prefer one action
    football_possession_value: float = 0.0  # points; assumption, off by default
    soccer_red_card_own_factor: float = 0.70  # assumption (reduced scoring)
    soccer_red_card_opp_factor: float = 1.25  # assumption (opponent scores more)
    soccer_stoppage_minutes: float = 6.0
    scan_all_live_games: bool = True


@dataclass
class ModelConfig:
    version: str = "v1.0"
    nfl_margin_sd: float = 13.5
    nfl_total_sd: float = 13.0
    nfl_team_total_sd: float = 9.0
    nfl_home_adv: float = 1.8
    cfb_margin_sd: float = 16.0
    cfb_total_sd: float = 15.5
    cfb_team_total_sd: float = 11.0
    cfb_home_adv: float = 2.8
    first_half_share: float = 0.48
    ridge_lambda: float = 3.0
    recency_half_life_days: float = 180.0
    min_games_for_full_reliability: int = 10
    cfb_unknown_team_prior: float = -10.0  # assumption: teams with little FBS data (often FCS) start below average
    soccer_first_half_share: float = 0.45  # share of goals scored in the first half (approx. league-wide)
    soccer_rho: float = -0.05  # Dixon-Coles low-score correction
    soccer_prior_matches: float = 4.0  # shrinkage pseudo-matches toward league average
    history_days: int = 420


@dataclass
class SportsConfig:
    enabled: list[str] = field(default_factory=lambda: ["nfl", "ncaaf", "soccer"])
    soccer_competitions: list[str] = field(
        default_factory=lambda: ["epl", "ucl", "laliga", "bundesliga", "seriea", "ligue1", "mls"]
    )
    provider: str = "espn"  # scoreboard/live provider
    soccer_results_provider: str = "espn"  # or "football-data"
    # Manual series overrides: {"SERIESTICKER": {"sport": "nfl", "market_type": "total"}}
    series_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    extra_series: list[str] = field(default_factory=list)
    # Secrets
    football_data_api_key: str = ""


@dataclass
class NotifyConfig:
    enabled: bool = True
    dedup_minutes: int = 90
    min_net_ev_per_dollar_alert: float = 0.05
    daily_summary: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class ExecutionConfig:
    # Real order placement is NOT implemented in v1. These guards exist so a future
    # version must pass every one of them.
    enabled: bool = False
    require_manual_approval: bool = True
    max_order_dollars: float = 1.0
    kill_switch: bool = True


@dataclass
class ExplainConfig:
    enabled: bool = False
    model: str = "claude-sonnet-5-5"
    anthropic_api_key: str = ""


@dataclass
class Settings:
    db_path: str = "data/assistant.db"
    log_level: str = "INFO"
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    value: ValueConfig = field(default_factory=ValueConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    sports: SportsConfig = field(default_factory=SportsConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    explain: ExplainConfig = field(default_factory=ExplainConfig)


SECRET_FIELDS = {
    "api_key_id", "private_key_pem", "private_key_path", "football_data_api_key",
    "telegram_bot_token", "telegram_chat_id", "anthropic_api_key",
}


def _apply(obj: Any, data: dict[str, Any], path: str = "") -> None:
    for f in fields(obj):
        if f.name not in data:
            continue
        val = data[f.name]
        current = getattr(obj, f.name)
        if is_dataclass(current):
            if isinstance(val, dict):
                _apply(current, val, f"{path}{f.name}.")
            continue
        if f.name in SECRET_FIELDS:
            log.warning("Ignoring secret '%s%s' in YAML; set it as an environment variable.", path, f.name)
            continue
        setattr(obj, f.name, val)


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if k.strip() and v and k.strip() not in os.environ:
            os.environ[k.strip()] = v


def load_settings(config_path: str | None = None) -> Settings:
    load_dotenv()
    s = Settings()
    path = config_path or os.getenv("KSA_CONFIG") or "config/settings.yaml"
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _apply(s, data)
    elif config_path:
        raise FileNotFoundError(f"Config file not found: {config_path}")

    env = os.environ
    s.db_path = env.get("KSA_DB_PATH", s.db_path)
    s.log_level = env.get("KSA_LOG_LEVEL", s.log_level)
    s.kalshi.environment = env.get("KALSHI_ENV", s.kalshi.environment)
    s.kalshi.api_key_id = env.get("KALSHI_API_KEY_ID", "")
    s.kalshi.private_key_pem = env.get("KALSHI_PRIVATE_KEY", "")
    s.kalshi.private_key_path = env.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not s.kalshi.base_url:
        s.kalshi.base_url = KALSHI_DEMO_URL if s.kalshi.environment == "demo" else KALSHI_PROD_URL
    s.notify.telegram_bot_token = env.get("TELEGRAM_BOT_TOKEN", "")
    s.notify.telegram_chat_id = env.get("TELEGRAM_CHAT_ID", "")
    s.sports.football_data_api_key = env.get("FOOTBALL_DATA_API_KEY", "")
    s.explain.anthropic_api_key = env.get("ANTHROPIC_API_KEY", "")
    if env.get("KSA_EXECUTION_ENABLED", "").lower() == "true":
        # Even if set, v1 has no order-placement code path; see execution.py.
        s.execution.enabled = True
    validate(s)
    return s


def validate(s: Settings) -> None:
    r = s.risk
    if r.min_position_dollars <= 0 or r.max_position_dollars < r.min_position_dollars:
        raise ValueError("risk.min/max_position_dollars invalid")
    if not 0 < r.kelly_fraction <= 1:
        raise ValueError("risk.kelly_fraction must be in (0, 1]")
    if not 0 < r.max_total_exposure_pct <= 1:
        raise ValueError("risk.max_total_exposure_pct must be in (0, 1]")
    if s.value.min_edge < 0 or s.value.max_spread <= 0:
        raise ValueError("value thresholds invalid")
    if s.kalshi.environment not in ("prod", "demo"):
        raise ValueError("kalshi.environment must be 'prod' or 'demo'")


class SecretRedactingFilter(logging.Filter):
    """Removes known secret values from every log record."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [x for x in secrets if x and len(x) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        changed = False
        for sec in self.secrets:
            if sec in msg:
                msg = msg.replace(sec, "***REDACTED***")
                changed = True
        if changed:
            record.msg, record.args = msg, ()
        return True


def setup_logging(s: Settings) -> None:
    root = logging.getLogger()
    if getattr(root, "_ksa_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
    ))
    handler.addFilter(SecretRedactingFilter([
        s.kalshi.api_key_id, s.kalshi.private_key_pem, s.notify.telegram_bot_token,
        s.sports.football_data_api_key, s.explain.anthropic_api_key,
    ]))
    root.addHandler(handler)
    root.setLevel(getattr(logging, s.log_level.upper(), logging.INFO))
    root._ksa_configured = True  # type: ignore[attr-defined]
