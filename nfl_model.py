"""NFL probability model: FootballModel with NFL-specific spreads of outcomes.

Default standard deviations (margin ~13.5, total ~13) and home advantage (~1.8 pts,
lower than the historical ~3 pts since 2020) are configurable in settings.yaml and
should be re-checked with `kalshi-assistant backtest --sport nfl`.
"""
from __future__ import annotations

from .config import ModelConfig
from .football_model import FootballModel


class NFLModel(FootballModel):
    sport = "nfl"

    def __init__(self, cfg: ModelConfig):
        super().__init__(cfg, cfg.nfl_margin_sd, cfg.nfl_total_sd, cfg.nfl_team_total_sd, cfg.nfl_home_adv,
                         unknown_prior=0.0)
