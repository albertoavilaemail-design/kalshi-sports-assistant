"""Kalshi Trade API v2 client (https://docs.kalshi.com/).

Public market-data endpoints need no authentication. Portfolio endpoints
(balance, positions, orders) require an API key; requests are signed per
https://docs.kalshi.com/getting_started/api_keys :
    signature = sign(timestamp_ms + METHOD + path_without_query)
using Ed25519 or RSA-PSS(SHA-256, salt = digest length), base64-encoded.

ORDER PLACEMENT IS DELIBERATELY NOT IMPLEMENTED. See execution.py.
"""
from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlparse

import requests

from .config import KalshiConfig

log = logging.getLogger(__name__)


class KalshiError(Exception):
    pass


class KalshiAuthError(KalshiError):
    pass


class KalshiUnavailable(KalshiError):
    """Raised when the API cannot be reached after retries (outage, rate limit)."""


@dataclass
class OrderBook:
    ticker: str
    yes_bids: list[tuple[float, float]]  # (price $, contracts), best first
    no_bids: list[tuple[float, float]]
    fetched_at: float

    @property
    def best_yes_bid(self) -> tuple[float, float] | None:
        return self.yes_bids[0] if self.yes_bids else None

    @property
    def best_no_bid(self) -> tuple[float, float] | None:
        return self.no_bids[0] if self.no_bids else None

    @property
    def best_yes_ask(self) -> tuple[float, float] | None:
        """Buying YES matches a resting NO bid: YES ask = 1 - best NO bid."""
        b = self.best_no_bid
        return (round(1.0 - b[0], 4), b[1]) if b else None

    @property
    def best_no_ask(self) -> tuple[float, float] | None:
        b = self.best_yes_bid
        return (round(1.0 - b[0], 4), b[1]) if b else None

    def depth_to_buy(self, side: str, max_price: float) -> float:
        """Contracts purchasable on `side` at prices <= max_price."""
        book = self.no_bids if side == "yes" else self.yes_bids
        return sum(q for p, q in book if 1.0 - p <= max_price + 1e-9)

    def depth_to_sell(self, side: str, min_price: float) -> float:
        book = self.yes_bids if side == "yes" else self.no_bids
        return sum(q for p, q in book if p >= min_price - 1e-9)

    def sweep_sell(self, side: str, contracts: float) -> tuple[float, float]:
        """Walk the book selling `contracts`; returns (filled, gross proceeds $)."""
        book = self.yes_bids if side == "yes" else self.no_bids
        remaining, proceeds = contracts, 0.0
        for p, q in book:
            take = min(q, remaining)
            proceeds += take * p
            remaining -= take
            if remaining <= 1e-9:
                break
        return contracts - max(remaining, 0.0), proceeds


def _f(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_orderbook(ticker: str, payload: dict[str, Any]) -> OrderBook:
    """Supports the current fixed-point format (orderbook_fp: yes_dollars/no_dollars)
    and the legacy cents format (orderbook: yes/no)."""
    yes: list[tuple[float, float]] = []
    no: list[tuple[float, float]] = []
    fp = payload.get("orderbook_fp")
    if fp:
        yes = [(float(p), float(q)) for p, q in (fp.get("yes_dollars") or [])]
        no = [(float(p), float(q)) for p, q in (fp.get("no_dollars") or [])]
    else:
        ob = payload.get("orderbook") or {}
        yes = [(float(p) / 100.0, float(q)) for p, q in (ob.get("yes") or [])]
        no = [(float(p) / 100.0, float(q)) for p, q in (ob.get("no") or [])]
    yes.sort(key=lambda t: -t[0])
    no.sort(key=lambda t: -t[0])
    return OrderBook(ticker, yes, no, time.time())


class KalshiClient:
    def __init__(self, cfg: KalshiConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "kalshi-sports-assistant/1.0"})
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._private_key = None
        self._path_prefix = urlparse(cfg.base_url).path.rstrip("/")  # "/trade-api/v2"

    # ------------------------------------------------------------------ auth
    @property
    def authenticated(self) -> bool:
        return bool(self.cfg.api_key_id and (self.cfg.private_key_pem or self.cfg.private_key_path))

    def _load_key(self):
        if self._private_key is not None:
            return self._private_key
        from cryptography.hazmat.primitives import serialization

        pem = self.cfg.private_key_pem
        if not pem and self.cfg.private_key_path:
            with open(self.cfg.private_key_path, "rb") as fh:
                pem = fh.read().decode()
        if not pem:
            raise KalshiAuthError("No Kalshi private key configured")
        pem = pem.replace("\\n", "\n").strip()
        try:
            self._private_key = serialization.load_pem_private_key(pem.encode(), password=None)
        except Exception as exc:  # noqa: BLE001
            raise KalshiAuthError("Could not parse KALSHI private key (check PEM formatting)") from exc
        return self._private_key

    def sign_headers(self, method: str, path: str, ts_ms: int | None = None) -> dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = self._load_key()
        ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
        msg = (ts + method.upper() + path.split("?")[0]).encode()
        if isinstance(key, Ed25519PrivateKey):
            sig = key.sign(msg)
        else:
            sig = key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    # --------------------------------------------------------------- request
    def _throttle(self) -> None:
        with self._lock:
            wait = self.cfg.min_request_interval_seconds - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def request(self, method: str, endpoint: str, params: dict | None = None, auth: bool = False) -> dict:
        url = self.cfg.base_url.rstrip("/") + endpoint
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            self._throttle()
            headers = self.sign_headers(method, self._path_prefix + endpoint) if auth else {}
            try:
                resp = self.session.request(method, url, params=params, headers=headers,
                                            timeout=self.cfg.timeout_seconds)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("Kalshi request error (%s %s) attempt %d: %s", method, endpoint, attempt + 1, type(exc).__name__)
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise KalshiError(f"Invalid JSON from {endpoint}") from exc
                if resp.status_code in (401, 403):
                    raise KalshiAuthError(f"Kalshi auth failed ({resp.status_code}) on {endpoint}")
                if resp.status_code == 404:
                    raise KalshiError(f"Not found: {endpoint}")
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = _f(resp.headers.get("Retry-After"))
                    last_exc = KalshiUnavailable(f"HTTP {resp.status_code} on {endpoint}")
                    log.warning("Kalshi %s on %s; backing off", resp.status_code, endpoint)
                    if retry_after:
                        delay = max(delay, retry_after)
                else:
                    raise KalshiError(f"HTTP {resp.status_code} on {endpoint}: {resp.text[:200]}")
            if attempt < self.cfg.max_retries:
                time.sleep(min(delay, 30))
                delay *= 2
        raise KalshiUnavailable(f"Kalshi API unavailable for {endpoint}: {last_exc}")

    def paginate(self, endpoint: str, key: str, params: dict | None = None, max_pages: int = 50,
                 auth: bool = False) -> Iterator[dict]:
        params = dict(params or {})
        for _ in range(max_pages):
            data = self.request("GET", endpoint, params, auth=auth)
            yield from data.get(key) or []
            cursor = data.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor
        log.warning("Pagination limit reached for %s", endpoint)

    # -------------------------------------------------------------- market data
    def get_series_list(self, category: str | None = None, tags: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if tags:
            params["tags"] = tags
        return self.request("GET", "/series", params).get("series") or []

    def get_series(self, series_ticker: str) -> dict:
        return self.request("GET", f"/series/{series_ticker}").get("series") or {}

    def get_events(self, series_ticker: str | None = None, status: str | None = "open",
                   with_nested_markets: bool = True) -> list[dict]:
        params: dict[str, Any] = {"limit": 200, "with_nested_markets": str(with_nested_markets).lower()}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return list(self.paginate("/events", "events", params))

    def get_event(self, event_ticker: str) -> dict:
        data = self.request("GET", f"/events/{event_ticker}", {"with_nested_markets": "true"})
        ev = data.get("event") or {}
        if "markets" not in ev and data.get("markets") is not None:
            ev["markets"] = data["markets"]
        return ev

    def get_markets(self, series_ticker: str | None = None, event_ticker: str | None = None,
                    status: str | None = "open", tickers: list[str] | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": 1000}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if tickers:
            params["tickers"] = ",".join(tickers)
        return list(self.paginate("/markets", "markets", params))

    def get_market(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}").get("market") or {}

    def get_orderbook(self, ticker: str, depth: int = 0) -> OrderBook:
        params = {"depth": depth} if depth else None
        return parse_orderbook(ticker, self.request("GET", f"/markets/{ticker}/orderbook", params))

    # --------------------------------------------------------------- portfolio
    def _require_auth(self) -> None:
        if not self.authenticated:
            raise KalshiAuthError("Kalshi API key not configured (KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY)")

    def get_balance(self) -> dict:
        self._require_auth()
        return self.request("GET", "/portfolio/balance", auth=True)

    def get_positions(self) -> list[dict]:
        self._require_auth()
        return list(self.paginate("/portfolio/positions", "market_positions", {"limit": 200}, auth=True))

    def get_order(self, order_id: str) -> dict:
        self._require_auth()
        return self.request("GET", f"/portfolio/orders/{order_id}", auth=True).get("order") or {}
