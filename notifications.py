"""Telegram notifications with deduplication, thresholds and price re-checks.

Setup (see README): create a bot with @BotFather -> TELEGRAM_BOT_TOKEN; message the
bot once, then run `kalshi-assistant telegram-chat-id` -> TELEGRAM_CHAT_ID.
Secrets come only from environment variables / GitHub secrets and are never logged.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime
from typing import Callable

import requests

from .config import NotifyConfig
from .storage import Storage

log = logging.getLogger(__name__)
MAX_LEN = 4000  # Telegram limit is 4096 characters


class Notifier:
    def __init__(self, cfg: NotifyConfig, storage: Storage, session: requests.Session | None = None,
                 dry_run: bool = False):
        self.cfg = cfg
        self.storage = storage
        self.session = session or requests.Session()
        self.dry_run = dry_run

    @property
    def configured(self) -> bool:
        return bool(self.cfg.telegram_bot_token and self.cfg.telegram_chat_id)

    @staticmethod
    def make_key(*parts: object) -> str:
        return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]

    def recently_sent(self, key: str, minutes: float | None = None) -> bool:
        window = (minutes if minutes is not None else self.cfg.dedup_minutes) * 60
        row = self.storage.conn.execute("SELECT 1 FROM alerts WHERE dedup_key=? AND sent=1 AND ts>=? LIMIT 1",
                                        (key, time.time() - window)).fetchone()
        return row is not None

    def send(self, kind: str, key: str, text: str, recheck: Callable[[], bool] | None = None,
             dedup_minutes: float | None = None) -> bool:
        """Send unless a duplicate was sent within the window. `recheck` (if given) must return True
        immediately before sending, e.g. to confirm the price still qualifies."""
        if not self.cfg.enabled:
            return False
        if self.recently_sent(key, dedup_minutes):
            log.info("dedup: skipping %s alert", kind)
            return False
        if recheck is not None:
            try:
                if not recheck():
                    log.info("recheck failed: %s alert suppressed", kind)
                    self._record(kind, key, text, False, "suppressed by price recheck")
                    return False
            except Exception as exc:  # noqa: BLE001
                self._record(kind, key, text, False, f"recheck error: {type(exc).__name__}")
                return False
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        body = f"{text}\n\n[{stamp}] Estimates, not guarantees. Paper/recommendation mode."
        ok, err = self._deliver(body)
        self._record(kind, key, body, ok, err)
        return ok

    def _deliver(self, text: str) -> tuple[bool, str | None]:
        if self.dry_run:
            print("---- ALERT (dry run, not sent) ----\n" + text)
            return True, None
        if not self.configured:
            print("---- ALERT (not sent: Telegram not configured) ----\n" + text)
            return False, "telegram not configured"
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [""]
        for chunk in chunks:
            for attempt in range(3):
                try:
                    r = self.session.post(url, json={"chat_id": self.cfg.telegram_chat_id, "text": chunk,
                                                     "disable_web_page_preview": True}, timeout=15)
                    if r.status_code == 200:
                        break
                    if r.status_code == 429:
                        time.sleep(float((r.json().get("parameters") or {}).get("retry_after", 3)))
                        continue
                    return False, f"telegram HTTP {r.status_code}"
                except requests.RequestException as exc:
                    if attempt == 2:
                        return False, f"telegram error {type(exc).__name__}"
                    time.sleep(2)
            else:
                return False, "telegram rate limited"
        return True, None

    def _record(self, kind: str, key: str, text: str, sent: bool, error: str | None) -> None:
        self.storage.conn.execute("INSERT INTO alerts(ts,kind,dedup_key,message,sent,error) VALUES(?,?,?,?,?,?)",
                                  (time.time(), kind, key, text, int(sent), error))

    def get_chat_id(self) -> str:
        if not self.cfg.telegram_bot_token:
            return "Set TELEGRAM_BOT_TOKEN first."
        r = self.session.get(f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/getUpdates", timeout=15)
        if r.status_code != 200:
            return f"Telegram returned HTTP {r.status_code}. Check the bot token."
        ids = {str(u.get("message", {}).get("chat", {}).get("id")) for u in r.json().get("result", []) if u.get("message")}
        ids.discard("None")
        return ", ".join(sorted(ids)) or "No messages found. Send any message to your bot in Telegram, then retry."

    def history(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.storage.q("SELECT ts, kind, sent, error, substr(message,1,160) AS preview FROM alerts ORDER BY ts DESC LIMIT ?", (limit,))]


def opportunity_alert(o, a) -> str:
    g = a.game
    lines = [f"{'LIVE ' if o.context != 'pregame' else ''}{o.context.upper()} OPPORTUNITY ({a.market.sport.upper()})",
             f"{a.market.event_title}"]
    if g and g.is_live:
        lines.append(f"Score: {g.score_line()}")
    lines += [
        f"Market: {o.contract_label} -> BUY {o.side.upper()}",
        f"Price: ${o.price:.2f} (implied {o.implied_prob:.0%}) | Est. prob {o.prob:.0%} (estimate)",
        f"Net EV after fees: ${o.net_ev_total():+.3f} ({o.net_ev_per_dollar():+.1%} per $)",
        f"Size: {o.contracts:g} contract(s) | Max loss ${o.max_loss:.2f} | Profit if right ${o.profit_if_win:.2f}",
        f"Reliability: {o.reliability:.2f}",
        "Risks: " + ("; ".join(o.risks[:3]) or "model may be missing news/injuries"),
        a.market.url,
    ]
    return "\n".join(lines)
