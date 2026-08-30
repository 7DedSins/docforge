"""Storage for API keys and usage metering.

SQLite rather than Postgres on purpose: this runs alongside other services on a
small box, the write volume is one row per API call, and a file you can copy is
a better backup story than a second database daemon.

Keys are stored as SHA-256 hashes. A stolen database file therefore does not
hand an attacker working credentials.

Usage rows carry the caller's IP and a *subject* — the upstream identity when
requests arrive through a marketplace like RapidAPI. Those two columns are what
make free-tier abuse detectable; without them every request looks like an
anonymous key and there is no way to tell one person with twenty free keys from
twenty people with one each.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime

DB_PATH = os.environ.get("DOCFORGE_DB_PATH", "/data/docforge.db")

# SQLite tolerates concurrent readers but serialises writers. One lock around
# writes keeps us off "database is locked" under burst traffic.
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash      TEXT PRIMARY KEY,
    label         TEXT NOT NULL DEFAULT '',
    plan          TEXT NOT NULL DEFAULT 'free',
    monthly_quota INTEGER NOT NULL DEFAULT 50,
    -- 0 means "fall back to the global default". Per-key limits let one
    -- customer be throttled without touching everyone else.
    rate_per_min  INTEGER NOT NULL DEFAULT 0,
    max_concurrent INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    -- Set once the customer exists in the billing provider, so usage can be
    -- reconciled against what they were actually charged.
    billing_ref   TEXT
);

CREATE TABLE IF NOT EXISTS usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash  TEXT NOT NULL,
    ts        TEXT NOT NULL,
    endpoint  TEXT NOT NULL,
    units     INTEGER NOT NULL DEFAULT 1,
    status    INTEGER NOT NULL,
    ms        INTEGER NOT NULL DEFAULT 0,
    ip        TEXT NOT NULL DEFAULT '',
    subject   TEXT NOT NULL DEFAULT ''
);

-- Anonymous demo hits, keyed by IP rather than by key. Kept separate so demo
-- traffic never pollutes customer usage or billing.
CREATE TABLE IF NOT EXISTS demo_usage (
    id  INTEGER PRIMARY KEY AUTOINCREMENT,
    ip  TEXT NOT NULL,
    ts  TEXT NOT NULL
);
"""

# Applied only after the migrations below, never in the same script as the table
# definitions. On an existing database `CREATE TABLE IF NOT EXISTS` is a no-op,
# so a column added in a later release does not exist yet — and indexing it in
# the same breath fails with "no such column", taking startup down on exactly
# the deployments that have data worth keeping.
INDEXES = """
-- Every quota check filters on (key, month), so index that pair.
CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage(key_hash, ts);
-- Abuse queries group by IP across keys.
CREATE INDEX IF NOT EXISTS idx_usage_ip_ts  ON usage(ip, ts);
CREATE INDEX IF NOT EXISTS idx_demo_ip_ts   ON demo_usage(ip, ts);
"""

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so each is applied only when missing — this keeps an existing
# database (with real keys and billing history in it) upgradable in place.
_MIGRATIONS = [
    ("usage", "ip", "TEXT NOT NULL DEFAULT ''"),
    ("usage", "subject", "TEXT NOT NULL DEFAULT ''"),
    ("api_keys", "rate_per_min", "INTEGER NOT NULL DEFAULT 0"),
    ("api_keys", "max_concurrent", "INTEGER NOT NULL DEFAULT 0"),
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL lets quota reads proceed while a usage row is being written.
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
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with cursor(write=True) as conn:
        # Order is load-bearing: tables, then columns, then indexes.
        conn.executescript(SCHEMA)
        for table, column, decl in _MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.executescript(INDEXES)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_key(
    label: str = "",
    plan: str = "free",
    monthly_quota: int = 50,
    rate_per_min: int = 0,
    max_concurrent: int = 0,
) -> str:
    """Mint a key. The raw value is returned exactly once and never stored."""
    raw = "df_" + secrets.token_urlsafe(32)
    with cursor(write=True) as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, label, plan, monthly_quota, "
            "rate_per_min, max_concurrent, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (hash_key(raw), label, plan, monthly_quota, rate_per_min,
             max_concurrent, datetime.now(UTC).isoformat()),
        )
    return raw


def lookup_key(raw: str) -> sqlite3.Row | None:
    with cursor() as conn:
        return conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND active = 1",
            (hash_key(raw),),
        ).fetchone()


def month_usage(key_hash: str) -> int:
    """Units consumed in the current UTC calendar month."""
    prefix = datetime.now(UTC).strftime("%Y-%m")
    with cursor() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS n FROM usage "
            # Only successful calls count. Billing a customer for our own 500 is
            # how you earn a chargeback.
            "WHERE key_hash = ? AND ts LIKE ? AND status < 400",
            (key_hash, f"{prefix}%"),
        ).fetchone()
    return int(row["n"])


def month_usage_by_subject(key_hash: str, subject: str) -> int:
    """Units this *subject* consumed under a shared key, this UTC month.

    One marketplace key fronts every subscriber, so the key's total is the sum
    of everyone's usage. Reporting that back to a caller would show them other
    customers' volume, so anything subject-scoped must be counted separately.
    """
    prefix = datetime.now(UTC).strftime("%Y-%m")
    with cursor() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS n FROM usage "
            "WHERE key_hash = ? AND subject = ? AND ts LIKE ? AND status < 400",
            (key_hash, subject, f"{prefix}%"),
        ).fetchone()
    return int(row["n"])


def record(
    key_hash: str,
    endpoint: str,
    units: int,
    status: int,
    ms: int,
    ip: str = "",
    subject: str = "",
) -> None:
    with cursor(write=True) as conn:
        conn.execute(
            "INSERT INTO usage (key_hash, ts, endpoint, units, status, ms, ip, subject) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key_hash, datetime.now(UTC).isoformat(), endpoint, units,
             status, ms, ip, subject),
        )


# --------------------------------------------------------------------------
# Anonymous demo quota, by IP
# --------------------------------------------------------------------------

def demo_count_today(ip: str) -> int:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    with cursor() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM demo_usage WHERE ip = ? AND ts LIKE ?",
            (ip, f"{day}%"),
        ).fetchone()
    return int(row["n"])


def demo_record(ip: str) -> None:
    with cursor(write=True) as conn:
        conn.execute(
            "INSERT INTO demo_usage (ip, ts) VALUES (?, ?)",
            (ip, datetime.now(UTC).isoformat()),
        )
        # The demo table is a rate-limit ledger, not history. Trim aggressively
        # so an attacker cannot grow the database by hammering the demo.
        conn.execute(
            "DELETE FROM demo_usage WHERE ts < datetime('now', '-3 days')"
        )


# --------------------------------------------------------------------------
# Abuse signals
# --------------------------------------------------------------------------

def abuse_report(days: int = 30) -> dict:
    """Signals that one person is farming several free keys.

    None of these prove abuse on their own — a shared office NAT looks exactly
    like one person with many keys. They are a shortlist to review by hand, not
    grounds for automatic action.
    """
    with cursor() as conn:
        shared_ip = conn.execute(
            "SELECT u.ip, COUNT(DISTINCT u.key_hash) AS keys, COUNT(*) AS calls "
            "FROM usage u JOIN api_keys k ON k.key_hash = u.key_hash "
            "WHERE u.ip != '' AND k.plan = 'free' "
            "  AND u.ts > datetime('now', ?) "
            "GROUP BY u.ip HAVING keys > 1 ORDER BY keys DESC LIMIT 20",
            (f"-{days} days",),
        ).fetchall()

        many_ips = conn.execute(
            "SELECT key_hash, COUNT(DISTINCT ip) AS ips, COUNT(*) AS calls "
            "FROM usage WHERE ip != '' AND ts > datetime('now', ?) "
            "GROUP BY key_hash HAVING ips > 5 ORDER BY ips DESC LIMIT 20",
            (f"-{days} days",),
        ).fetchall()

        # A free key that burns its whole month in minutes is a script, not a
        # developer evaluating the product.
        burst = conn.execute(
            "SELECT u.key_hash, k.label, COUNT(*) AS calls, "
            "       MIN(u.ts) AS first_seen, MAX(u.ts) AS last_seen "
            "FROM usage u JOIN api_keys k ON k.key_hash = u.key_hash "
            "WHERE k.plan = 'free' AND u.ts > datetime('now', ?) "
            "GROUP BY u.key_hash "
            "HAVING calls >= 20 AND (julianday(MAX(u.ts)) - julianday(MIN(u.ts))) * 1440 < 10 "
            "ORDER BY calls DESC LIMIT 20",
            (f"-{days} days",),
        ).fetchall()

    return {
        "shared_ip": [dict(r) for r in shared_ip],
        "many_ips": [dict(r) for r in many_ips],
        "burst": [dict(r) for r in burst],
    }
