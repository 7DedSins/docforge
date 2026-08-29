"""Authentication and quota accounting.

A valid API key is not a trust boundary — free-tier keys are handed out on
signup. These tests pin the behaviour that decides whether someone can use the
service at all, and whether they get billed correctly for it.
"""

from __future__ import annotations

import pytest
from app import db


def _render(client, headers):
    return client.post(
        "/v1/image/render",
        json={"template": "<p>{{ x }}</p>", "data": {"x": "hi"}},
        headers=headers,
    )


def test_missing_key_rejected(client):
    assert _render(client, {}).status_code == 401


def test_unknown_key_rejected(client):
    r = _render(client, {"Authorization": "Bearer forge_not_a_real_key"})
    assert r.status_code == 401


def test_malformed_authorization_header_rejected(client, key):
    # Correct key, wrong scheme.
    assert _render(client, {"Authorization": key}).status_code == 401
    assert _render(client, {"Authorization": f"Basic {key}"}).status_code == 401


def test_valid_key_accepted(client, auth):
    assert _render(client, auth).status_code == 200


def test_x_api_key_header_also_works(client, key):
    """RapidAPI and several agent frameworks send this instead of Bearer."""
    assert _render(client, {"X-API-Key": key}).status_code == 200


def test_revoked_key_rejected(client, key, auth):
    assert _render(client, auth).status_code == 200
    with db.cursor(write=True) as conn:
        conn.execute("UPDATE api_keys SET active = 0 WHERE key_hash = ?",
                     (db.hash_key(key),))
    assert _render(client, auth).status_code == 401


def test_raw_key_is_never_stored(key):
    """A leaked database file must not yield working credentials."""
    with db.cursor() as conn:
        rows = conn.execute("SELECT key_hash FROM api_keys").fetchall()
    stored = [r["key_hash"] for r in rows]
    assert key not in stored
    assert db.hash_key(key) in stored


# --------------------------------------------------------------------------
# Quota
# --------------------------------------------------------------------------

def test_usage_is_recorded(client, auth, key):
    for _ in range(3):
        _render(client, auth)
    assert db.month_usage(db.hash_key(key)) == 3


def test_quota_exhaustion_returns_429(client):
    k = db.create_key(label="tiny", plan="free", monthly_quota=2)
    h = {"Authorization": f"Bearer {k}"}
    assert _render(client, h).status_code == 200
    assert _render(client, h).status_code == 200
    r = _render(client, h)
    assert r.status_code == 429
    assert "quota" in r.json()["detail"].lower()


def test_failed_calls_do_not_consume_quota(client, auth, key):
    """Billing a customer for our own rejection is how you earn a chargeback."""
    bad = client.post("/v1/image/render", json={"template": ""}, headers=auth)
    assert bad.status_code == 400
    assert db.month_usage(db.hash_key(key)) == 0


def test_unlimited_plan_has_no_cap(client):
    k = db.create_key(label="unl", plan="unlimited", monthly_quota=0)
    h = {"Authorization": f"Bearer {k}"}
    for _ in range(5):
        assert _render(client, h).status_code == 200


def test_usage_endpoint_reports_remaining(client, auth):
    _render(client, auth)
    body = client.get("/v1/usage", headers=auth).json()
    assert body["plan"] == "pro"
    assert body["monthly_quota"] == 1000
    assert body["used_this_month"] == 1
    assert body["remaining"] == 999


@pytest.mark.parametrize("path", ["/v1/usage", "/v1/convert/html", "/v1/image/render"])
def test_every_billable_route_requires_auth(client, path):
    r = client.get(path) if path == "/v1/usage" else client.post(path, json={})
    assert r.status_code == 401
