"""Storage layer: key handling and usage accounting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app import db


def test_issued_keys_are_unique():
    keys = {db.create_key(label=f"k{i}") for i in range(20)}
    assert len(keys) == 20


def test_key_has_recognisable_prefix():
    assert db.create_key().startswith("df_")


def test_lookup_returns_none_for_unknown_key():
    assert db.lookup_key("df_nope") is None


def test_lookup_returns_row_for_valid_key():
    raw = db.create_key(label="acme", plan="scale", monthly_quota=500_000)
    row = db.lookup_key(raw)
    assert row is not None
    assert row["label"] == "acme"
    assert row["plan"] == "scale"
    assert row["monthly_quota"] == 500_000


def test_hash_is_stable_and_not_reversible():
    raw = "df_example"
    assert db.hash_key(raw) == db.hash_key(raw)
    assert raw not in db.hash_key(raw)
    assert len(db.hash_key(raw)) == 64  # sha256 hex


def test_month_usage_counts_only_successes():
    raw = db.create_key()
    h = db.hash_key(raw)
    db.record(h, "image/render", 1, 200, 10)
    db.record(h, "image/render", 1, 500, 10)   # our failure
    db.record(h, "image/render", 1, 429, 10)   # rejected
    db.record(h, "image/render", 1, 201, 10)
    assert db.month_usage(h) == 2


def test_month_usage_excludes_other_months():
    raw = db.create_key()
    h = db.hash_key(raw)
    db.record(h, "image/render", 1, 200, 10)

    # Backdate a row by ~60 days; it must not count toward this month.
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    with db.cursor(write=True) as conn:
        conn.execute(
            "INSERT INTO usage (key_hash, ts, endpoint, units, status, ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (h, old, "image/render", 5, 200, 10),
        )
    assert db.month_usage(h) == 1


def test_usage_is_isolated_per_key():
    a, b = db.create_key(label="a"), db.create_key(label="b")
    db.record(db.hash_key(a), "x", 3, 200, 1)
    assert db.month_usage(db.hash_key(a)) == 3
    assert db.month_usage(db.hash_key(b)) == 0


def test_units_are_summed_not_counted():
    raw = db.create_key()
    h = db.hash_key(raw)
    db.record(h, "x", 5, 200, 1)
    db.record(h, "x", 3, 200, 1)
    assert db.month_usage(h) == 8


def test_revocation_preserves_usage_history():
    """Revoking is a flag, not a delete — billing history must survive."""
    raw = db.create_key(label="gone")
    h = db.hash_key(raw)
    db.record(h, "x", 2, 200, 1)
    with db.cursor(write=True) as conn:
        conn.execute("UPDATE api_keys SET active = 0 WHERE key_hash = ?", (h,))
    assert db.lookup_key(raw) is None
    assert db.month_usage(h) == 2
