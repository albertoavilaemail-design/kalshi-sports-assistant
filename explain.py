"""Optional plain-English matchup explanation using Claude (Anthropic Messages API).

The language model ONLY summarizes the verified facts and model numbers we pass in;
it is instructed not to add statistics, injuries or news. Probabilities always come
from the statistical models, never from the language model.
Enable with explain.enabled: true and ANTHROPIC_API_KEY (paid API usage).
"""
from __future__ import annotations

import json
import logging

import requests

from .config import ExplainConfig

log = logging.getLogger(__name__)

SYSTEM = ("You explain sports trading analysis for a cautious beginner. Use ONLY the facts in the JSON. "
          "Do not invent statistics, injuries, lineups, news or results. Do not change any probability. "
          "Label estimates as estimates. Mention the main risk. 120 words max.")


def explain(cfg: ExplainConfig, facts: dict) -> str | None:
    if not (cfg.enabled and cfg.anthropic_api_key):
        return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=30, headers={
            "x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": cfg.model, "max_tokens": 400, "system": SYSTEM,
                  "messages": [{"role": "user", "content": json.dumps(facts, default=str)}]})
        if r.status_code != 200:
            log.warning("explanation unavailable: HTTP %s", r.status_code)
            return None
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    except requests.RequestException as exc:
        log.warning("explanation unavailable: %s", type(exc).__name__)
        return None
