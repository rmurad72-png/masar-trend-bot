"""Persistence: PostgreSQL (Supabase) in production via DATABASE_URL, SQLite locally.
Railway's disk is wiped on redeploy, so production must use Postgres."""
import json
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS positions (
        account TEXT NOT NULL, asset TEXT NOT NULL, side INTEGER NOT NULL, market TEXT NOT NULL,
        qty DOUBLE PRECISION NOT NULL, entry_px DOUBLE PRECISION NOT NULL, stop_px DOUBLE PRECISION NOT NULL,
        risk_cash DOUBLE PRECISION NOT NULL, risk_pct DOUBLE PRECISION NOT NULL, entry_ts BIGINT NOT NULL,
        clid TEXT, algo_id TEXT, checked_ts BIGINT NOT NULL DEFAULT 0, fees DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY (account, asset))""",
    """CREATE TABLE IF NOT EXISTS trades (
        id {serial}, account TEXT NOT NULL, asset TEXT NOT NULL, side INTEGER NOT NULL,
        entry_ts BIGINT, exit_ts BIGINT, entry_px DOUBLE PRECISION, exit_px DOUBLE PRECISION,
        qty DOUBLE PRECISION, pnl DOUBLE PRECISION, r DOUBLE PRECISION, reason TEXT)""",
    """CREATE TABLE IF NOT EXISTS events (
        id {serial}, ts BIGINT NOT NULL, account TEXT, kind TEXT NOT NULL, asset TEXT, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS equity (
        account TEXT NOT NULL, day INTEGER NOT NULL, equity DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (account, day))""",
    """CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)""",
]

POS_COLS = ["account", "asset", "side", "market", "qty", "entry_px", "stop_px", "risk_cash", "risk_pct",
            "entry_ts", "clid", "algo_id", "checked_ts", "fees"]


class Store:
    def __init__(self, url: str):
        self.pg = url.startswith("postgres")
        if self.pg:
            import psycopg
            self.conn = psycopg.connect(url, autocommit=True, prepare_threshold=None)  # Supabase pooler-safe
            serial = "BIGSERIAL PRIMARY KEY"
        else:
            path = url.replace("sqlite:///", "") or ":memory:"
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.isolation_level = None
            serial = "INTEGER PRIMARY KEY AUTOINCREMENT"
        for s in SCHEMA:
            self._exec(s.replace("{serial}", serial))

    def try_single_instance_lock(self):
        """Postgres session advisory lock: only one bot process may trade. SQLite: always True."""
        if not self.pg:
            return True
        return bool(self._exec("SELECT pg_try_advisory_lock(727274)").fetchone()[0])

    def _q(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def _exec(self, sql, args=()):
        cur = self.conn.cursor()
        cur.execute(self._q(sql), args)
        return cur

    def _rows(self, sql, args=()):
        cur = self._exec(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---------- kv ----------
    def get(self, k, default=None):
        r = self._rows("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def set(self, k, v):
        self._exec("DELETE FROM kv WHERE k=?", (k,))
        self._exec("INSERT INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))

    # ---------- positions ----------
    def positions(self, account):
        return self._rows("SELECT * FROM positions WHERE account=? ORDER BY entry_ts", (account,))

    def position(self, account, asset):
        r = self._rows("SELECT * FROM positions WHERE account=? AND asset=?", (account, asset))
        return r[0] if r else None

    def save_position(self, p: dict):
        self._exec("DELETE FROM positions WHERE account=? AND asset=?", (p["account"], p["asset"]))
        cols = [c for c in POS_COLS if c in p]
        self._exec(f"INSERT INTO positions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   tuple(p[c] for c in cols))

    def delete_position(self, account, asset):
        self._exec("DELETE FROM positions WHERE account=? AND asset=?", (account, asset))

    # ---------- trades / events / equity ----------
    def add_trade(self, t: dict):
        cols = ["account", "asset", "side", "entry_ts", "exit_ts", "entry_px", "exit_px", "qty", "pnl", "r", "reason"]
        self._exec(f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   tuple(t[c] for c in cols))

    def trades(self, account, since_ts=0):
        return self._rows("SELECT * FROM trades WHERE account=? AND exit_ts>=? ORDER BY exit_ts", (account, since_ts))

    def event(self, kind, account=None, asset=None, detail=None):
        self._exec("INSERT INTO events (ts, account, kind, asset, detail) VALUES (?, ?, ?, ?, ?)",
                   (int(time.time() * 1000), account, kind, asset,
                    json.dumps(detail, ensure_ascii=False) if detail is not None else None))

    def events(self, since_ts=0, kind=None):
        if kind:
            return self._rows("SELECT * FROM events WHERE ts>=? AND kind=? ORDER BY ts", (since_ts, kind))
        return self._rows("SELECT * FROM events WHERE ts>=? ORDER BY ts", (since_ts,))

    def save_equity(self, account, day, equity):
        self._exec("DELETE FROM equity WHERE account=? AND day=?", (account, day))
        self._exec("INSERT INTO equity (account, day, equity) VALUES (?, ?, ?)", (account, day, equity))

    def equity_curve(self, account):
        return self._rows("SELECT day, equity FROM equity WHERE account=? ORDER BY day", (account,))
