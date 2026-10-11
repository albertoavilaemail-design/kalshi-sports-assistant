"""College-football (FBS) probability model.

Differences from the NFL model:
  * wider outcome distributions (bigger talent gaps, more blowouts);
  * larger home-field advantage;
  * teams with fewer than 3 weighted games in the data (typically FCS opponents)
    start from a below-average prior (`cfb_unknown_team_prior`, an explicit assumption)
    instead of the league average, and the matchup's reliability is reduced.

Rankings are reported for context but are NOT model inputs: the ratings come from
results, so a ranked team that has not played anyone strong is not overrated by
its poll position. Whether the market already prices a mismatch is decided by
comparing the model probability with the executable price, never by ranking alone.
"""
from __future__ import annotations

from .config import ModelConfig
from .football_model import FootballModel


class CollegeFootballModel(FootballModel):
    sport = "ncaaf"

    def __init__(self, cfg: ModelConfig):
        super().__init__(cfg, cfg.cfb_margin_sd, cfg.cfb_total_sd, cfg.cfb_team_total_sd, cfg.cfb_home_adv,
                         unknown_prior=cfg.cfb_unknown_team_prior)
