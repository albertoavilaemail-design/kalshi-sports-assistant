"""SQLite persistence: bankroll ledger, positions, predictions, alerts, snapshots,
historical results, caches, locks and an audit log.

SQLite in WAL mode with a busy timeout handles concurrent readers/writers on one
host. Cross-run locking (e.g. overlapping GitHub Actions runs) uses the `locks`
table plus the workflow-level `concurrency` group.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, account TEXT NOT NULL DEFAULT 'real',
    kind TEXT NOT NULL, amount REAL NOT NULL, position_id TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY, mode TEXT NOT NULL, market_ticker TEXT NOT NULL, event_ticker TEXT,
    series_ticker TEXT, sport TEXT, competition TEXT, market_type TEXT, contract TEXT,
    side TEXT NOT NULL, contracts REAL NOT NULL, entry_price REAL NOT NULL, entry_fees REAL NOT NULL,
    opened_at REAL NOT NULL, context TEXT, model_prob REAL, model_version TEXT,
    status TEXT NOT NULL DEFAULT 'open', exit_price REAL, exit_fees REAL, closed_at REAL,
    realized_pnl REAL, close_reason TEXT, notes TEXT);
CREATE INDEX IF NOT EXISTS ix_pos_status ON positions(status);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, model_version TEXT, sport TEXT,
    competition TEXT, market_ticker TEXT, market_type TEXT, side TEXT, context TEXT,
    model_prob REAL NOT NULL, market_price REAL, reliability REAL, outcome INTEGER, settled_at REAL,
    position_id TEXT);
CREATE INDEX IF NOT EXISTS ix_pred_ticker ON predictions(market_ticker);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
    dedup_key TEXT NOT NULL, message TEXT NOT NULL, sent INTEGER NOT NULL, error TEXT);
CREATE INDEX IF NOT EXISTS ix_alert_key ON alerts(dedup_key, ts);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, market_ticker TEXT NOT NULL,
    game_id TEXT, yes_bid REAL, yes_ask REAL, model_prob REAL, home_score REAL, away_score REAL,
    elapsed_fraction REAL);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(market_ticker, ts);
CREATE TABLE IF NOT EXISTS results (
    game_id TEXT PRIMARY KEY, sport TEXT NOT NULL, competition TEXT NOT NULL, date TEXT NOT NULL,
    home TEXT NOT NULL, away TEXT NOT NULL, home_score REAL NOT NULL, away_score REAL NOT NULL,
    neutral INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_results ON results(sport, competition, date);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, game_id TEXT,
    market_ticker TEXT, position_id TEXT, decision TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS watchlist (query TEXT PRIMARY KEY, added REAL NOT NULL);
CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, holder TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS picks (
    market_ticker TEXT NOT NULL, side TEXT NOT NULL, sport TEXT, market_type TEXT, label TEXT,
    first_ts REAL NOT NULL, first_price REAL NOT NULL, first_mid REAL, model_prob REAL,
    last_ts REAL, last_mid REAL, started INTEGER NOT NULL DEFAULT 0, kickoff TEXT,
    PRIMARY KEY (market_ticker, side));
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT, action TEXT, detail TEXT);
"""


class Storage:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        try:
            if self.path != ":memory:":
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Immediate write transaction: serializes writers so checks + inserts are atomic."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    # -------------------------------------------------------------- kv / cache
    def kv_get(self, k: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def kv_set(self, k: str, v: Any) -> None:
        self.conn.execute("INSERT INTO kv(k,v,updated) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
                          (k, json.dumps(v), time.time()))

    def cache_get(self, k: str, max_age: float) -> Any:
        row = self.conn.execute("SELECT v, updated FROM kv WHERE k=?", ("cache:" + k,)).fetchone()
        if row and time.time() - row["updated"] <= max_age:
            return json.loads(row["v"])
        return None

    def cache_set(self, k: str, v: Any) -> None:
        self.kv_set("cache:" + k, v)

    # ------------------------------------------------------------------ locks
    def acquire_lock(self, name: str, ttl: float = 900) -> str | None:
        holder = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT holder, expires FROM locks WHERE name=?", (name,)).fetchone()
            if row and row["expires"] > now:
                return None
            c.execute("INSERT INTO locks(name,holder,expires) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, expires=excluded.expires",
                      (name, holder, now + ttl))
        return holder

    def release_lock(self, name: str, holder: str) -> None:
        self.conn.execute("DELETE FROM locks WHERE name=? AND holder=?", (name, holder))

    # ------------------------------------------------------------------ audit
    def audit(self, action: str, detail: Any = None, actor: str = "system") -> None:
        self.conn.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
                          (time.time(), actor, action, json.dumps(detail, default=str) if detail is not None else None))

    # -------------------------------------------------------------- snapshots
    def add_snapshot(self, ticker: str, game_id: str | None, yes_bid: float | None, yes_ask: float | None,
                     model_prob: float | None, home_score: float | None, away_score: float | None,
                     elapsed_fraction: float | None) -> None:
        self.conn.execute("INSERT INTO snapshots(ts,market_ticker,game_id,yes_bid,yes_ask,model_prob,home_score,away_score,elapsed_fraction) VALUES(?,?,?,?,?,?,?,?,?)",
                          (time.time(), ticker, game_id, yes_bid, yes_ask, model_prob, home_score, away_score, elapsed_fraction))

    def snapshots_since(self, ticker: str, since_ts: float) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM snapshots WHERE market_ticker=? AND ts>=? ORDER BY ts", (ticker, since_ts))

    def prune(self, days: int = 21) -> None:
        cutoff = time.time() - days * 86400
        self.conn.execute("DELETE FROM snapshots WHERE ts<?", (cutoff,))
        self.conn.execute("DELETE FROM alerts WHERE ts<?", (time.time() - 120 * 86400,))

    # ---------------------------------------------------------------- results
    def upsert_results(self, rows: list[dict]) -> int:
        with self.tx() as c:
            for r in rows:
                c.execute("""INSERT INTO results(game_id,sport,competition,date,home,away,home_score,away_score,neutral,source,fetched_at)
                             VALUES(:game_id,:sport,:competition,:date,:home,:away,:home_score,:away_score,:neutral,:source,:fetched_at)
                             ON CONFLICT(game_id) DO UPDATE SET home_score=excluded.home_score, away_score=excluded.away_score,
                             fetched_at=excluded.fetched_at""", r)
        return len(rows)

    def results(self, sport: str, competition: str | None = None, since: str | None = None,
                before: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM results WHERE sport=?", [sport]
        if competition:
            sql += " AND competition=?"
            args.append(competition)
        if since:
            sql += " AND date>=?"
            args.append(since)
        if before:
            sql += " AND date<?"
            args.append(before)
        return self.q(sql + " ORDER BY date", tuple(args))

    # -------------------------------------------------------------- watchlist
    def watch(self, query: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO watchlist(query,added) VALUES(?,?)", (query, time.time()))

    def unwatch(self, query: str) -> None:
        self.conn.execute("DELETE FROM watchlist WHERE query=?", (query,))

    def watchlist(self) -> list[str]:
        return [r["query"] for r in self.q("SELECT query FROM watchlist ORDER BY added")]

    def record_decision(self, kind: str, decision: str, game_id: str | None = None, ticker: str | None = None,
                        position_id: str | None = None, detail: Any = None) -> None:
        self.conn.execute("INSERT INTO decisions(ts,kind,game_id,market_ticker,position_id,decision,detail) VALUES(?,?,?,?,?,?,?)",
                          (time.time(), kind, game_id, ticker, position_id, decision, json.dumps(detail, default=str)))

    def last_decision(self, kind: str, ticker: str | None = None, position_id: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT decision FROM decisions WHERE kind=? AND (market_ticker IS ? OR ? IS NULL) AND (position_id IS ? OR ? IS NULL) ORDER BY ts DESC LIMIT 1",
            (kind, ticker, ticker, position_id, position_id)).fetchone()
        return row["decision"] if row else None
