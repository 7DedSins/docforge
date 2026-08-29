"""Storage for API keys and usage metering.

SQLite rather than Postgres on purpose: this runs alongside a media stack on a
4-core box, the write volume is one row per API call, and a file we can copy is
a better backup story than a second database daemon.

Keys are stored as SHA-256 hashes. A stolen database file therefore does not
hand an attacker working credentials.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("FORGE_DB_PATH", "/data/forge.db")

# SQLite tolerates concurrent readers but serialises writers. One lock around
# writes keeps us off "database is locked" under burst traffic.
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash    TEXT PRIMARY KEY,
    label       TEXT NOT NULL DEFAULT '',
    plan        TEXT NOT NULL DEFAULT 'free',
    monthly_quota INTEGER NOT NULL DEFAULT 250,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    -- Set once the customer exists in the billing provider, so usage can be
    -- reconciled against what they were actually charged.
    billing_ref TEXT
);

CREATE TABLE IF NOT EXISTS usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash  TEXT NOT NULL,
    ts        TEXT NOT NULL,
    endpoint  TEXT NOT NULL,
    units     INTEGER NOT NULL DEFAULT 1,
    status    INTEGER NOT NULL,
    ms        INTEGER NOT NULL DEFAULT 0
);

-- Every quota check filters on (key, month), so index that pair.
CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage(key_hash, ts);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL lets the quota reads proceed while a usage row is being written.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def cursor(write: bool = False):
    conn = _connect()
    try:
        if write:
            with _write_lock:
                yield conn
                conn.commit()
        else:
            yield conn
    finally:
        conn.close()


def init() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with cursor(write=True) as conn:
        conn.executescript(SCHEMA)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_key(label: str = "", plan: str = "free", monthly_quota: int = 250) -> str:
    """Mint a key. The raw value is returned exactly once and never stored."""
    raw = "forge_" + secrets.token_urlsafe(32)
    with cursor(write=True) as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, label, plan, monthly_quota, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (hash_key(raw), label, plan, monthly_quota,
             datetime.now(timezone.utc).isoformat()),
        )
    return raw


def lookup_key(raw: str) -> sqlite3.Row | None:
    with cursor() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND active = 1",
            (hash_key(raw),),
        ).fetchone()
    return row


def month_usage(key_hash: str) -> int:
    """Units consumed in the current UTC calendar month."""
    prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with cursor() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS n FROM usage "
            # Only successful calls count. Billing a customer for our own 500 is
            # how you earn a chargeback.
            "WHERE key_hash = ? AND ts LIKE ? AND status < 400",
            (key_hash, f"{prefix}%"),
        ).fetchone()
    return int(row["n"])


def record(key_hash: str, endpoint: str, units: int, status: int, ms: int) -> None:
    with cursor(write=True) as conn:
        conn.execute(
            "INSERT INTO usage (key_hash, ts, endpoint, units, status, ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key_hash, datetime.now(timezone.utc).isoformat(), endpoint,
             units, status, ms),
        )
