"""Rate limiting, concurrency fairness, marketplace auth, and the demo cap.

Monthly quota says how *much* a caller may use. These cover how *fast* — the
controls that decide whether one caller can make the service unusable for
everyone else, and whether the free tier can be farmed.
"""

from __future__ import annotations

import io

import pytest
from app import db
from app import main as main_module


def _render(client, headers):
    return client.post(
        "/v1/image/render", json={"template": "<p>ok</p>"}, headers=headers
    )


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

def test_rate_limit_rejects_beyond_ceiling(client):
    raw = db.create_key(label="slow", plan="pro", monthly_quota=1000, rate_per_min=3)
    h = {"Authorization": f"Bearer {raw}"}
    for _ in range(3):
        assert _render(client, h).status_code == 200
    r = _render(client, h)
    assert r.status_code == 429
    assert "rate limit" in r.json()["detail"].lower()


def test_rate_limited_response_tells_client_when_to_retry(client):
    raw = db.create_key(label="r", plan="pro", monthly_quota=1000, rate_per_min=1)
    h = {"Authorization": f"Bearer {raw}"}
    _render(client, h)
    r = _render(client, h)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


def test_rate_limit_does_not_consume_quota(client):
    """A rejected request costs the caller nothing — they got no work done."""
    raw = db.create_key(label="r", plan="pro", monthly_quota=1000, rate_per_min=1)
    h = {"Authorization": f"Bearer {raw}"}
    _render(client, h)
    assert _render(client, h).status_code == 429
    assert db.month_usage(db.hash_key(raw)) == 1


def test_rate_limits_are_per_key_not_global(client):
    a = db.create_key(label="a", plan="pro", monthly_quota=1000, rate_per_min=1)
    b = db.create_key(label="b", plan="pro", monthly_quota=1000, rate_per_min=1)
    assert _render(client, {"Authorization": f"Bearer {a}"}).status_code == 200
    # b must be unaffected by a exhausting its allowance.
    assert _render(client, {"Authorization": f"Bearer {b}"}).status_code == 200
    assert _render(client, {"Authorization": f"Bearer {a}"}).status_code == 429


# --------------------------------------------------------------------------
# Client IP resolution — security critical
# --------------------------------------------------------------------------

def test_client_ip_uses_last_forwarded_entry():
    """Caddy appends the peer it saw; earlier entries are caller-controlled.

    Trusting the *first* entry — the common mistake — lets a caller forge a
    different IP per request and walk straight through the demo's per-IP cap.
    """
    class Req:
        headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8, 203.0.113.9"}
        client = None

    assert main_module.client_ip(Req()) == "203.0.113.9"


def test_client_ip_falls_back_to_peer_when_no_header():
    class Peer:
        host = "198.51.100.7"

    class Req:
        headers = {}
        client = Peer()

    assert main_module.client_ip(Req()) == "198.51.100.7"


# --------------------------------------------------------------------------
# Anonymous demo
# --------------------------------------------------------------------------

def _demo(client, ip="203.0.113.5", name="a.docx", body=b"data"):
    return client.post(
        "/try/convert",
        files={"file": (name, io.BytesIO(body), "application/octet-stream")},
        headers={"x-forwarded-for": ip},
    )


def test_demo_needs_no_api_key(client):
    assert _demo(client).status_code == 200


def test_demo_is_capped_per_ip(client):
    ip = "203.0.113.11"
    for _ in range(main_module.DEMO_PER_DAY):
        assert _demo(client, ip=ip).status_code == 200
    r = _demo(client, ip=ip)
    assert r.status_code == 429
    assert "demo" in r.json()["detail"].lower()


def test_demo_cap_is_per_ip_not_global(client):
    for _ in range(main_module.DEMO_PER_DAY):
        _demo(client, ip="203.0.113.20")
    assert _demo(client, ip="203.0.113.21").status_code == 200


def test_demo_cannot_be_bypassed_by_spoofing_forwarded_for(client):
    """Prepending a fake IP must not reset the cap."""
    real = "203.0.113.30"
    for _ in range(main_module.DEMO_PER_DAY):
        _demo(client, ip=real)
    r = client.post(
        "/try/convert",
        files={"file": ("a.docx", io.BytesIO(b"x"), "application/octet-stream")},
        headers={"x-forwarded-for": f"9.9.9.9, {real}"},
    )
    assert r.status_code == 429


def test_demo_rejects_unsupported_types(client):
    assert _demo(client, name="virus.exe").status_code == 415


def test_demo_counts_before_rendering(client):
    """Otherwise a caller spends our CPU for free by sending files that fail."""
    ip = "203.0.113.40"
    _demo(client, ip=ip)
    assert db.demo_count_today(ip) == 1


def test_demo_never_touches_customer_usage(client):
    _demo(client, ip="203.0.113.50")
    with db.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM usage").fetchone()["c"] == 0


# --------------------------------------------------------------------------
# Marketplace proxy secret
# --------------------------------------------------------------------------

@pytest.fixture
def marketplace(monkeypatch, key):
    """Configure the marketplace path: proxy secret plus the key it bills to."""
    monkeypatch.setattr(main_module, "RAPIDAPI_PROXY_SECRET", "s3cret")
    monkeypatch.setattr(main_module, "RAPIDAPI_KEY", key)
    return {"X-RapidAPI-Proxy-Secret": "s3cret"}


def test_marketplace_call_needs_no_docforge_key(client, marketplace):
    """RapidAPI strips its own key and never forwards a DocForge one.

    Regression: authenticate() demanded Authorization regardless, so every
    marketplace request 401'd. Caught only by calling the live proxy.
    """
    assert _render(client, marketplace).status_code == 200


def test_wrong_proxy_secret_refused(client, monkeypatch, key):
    monkeypatch.setattr(main_module, "RAPIDAPI_PROXY_SECRET", "s3cret")
    monkeypatch.setattr(main_module, "RAPIDAPI_KEY", key)
    assert _render(client, {"X-RapidAPI-Proxy-Secret": "wrong"}).status_code == 403


def test_marketplace_unconfigured_is_503_not_a_silent_401(client, monkeypatch):
    """A missing marketplace key is our misconfiguration, not the caller's."""
    monkeypatch.setattr(main_module, "RAPIDAPI_PROXY_SECRET", "s3cret")
    monkeypatch.setattr(main_module, "RAPIDAPI_KEY", "")
    assert _render(client, {"X-RapidAPI-Proxy-Secret": "s3cret"}).status_code == 503


def test_direct_key_still_works_alongside_marketplace(client, auth, marketplace):
    """Direct customers must not be locked out once the marketplace is live."""
    assert _render(client, auth).status_code == 200


def test_marketplace_user_recorded_as_subject(client, marketplace):
    """Needed to tell one marketplace end-user from another under one key."""
    h = dict(marketplace, **{"X-RapidAPI-User": "alice"})
    assert _render(client, h).status_code == 200
    with db.cursor() as conn:
        row = conn.execute("SELECT subject FROM usage ORDER BY id DESC LIMIT 1").fetchone()
    assert row["subject"] == "alice"


def test_marketplace_users_are_rate_limited_separately(client, monkeypatch, marketplace):
    """One busy subscriber must not throttle every other subscriber."""
    monkeypatch.setattr(main_module, "DEFAULT_RATE_PER_MIN", 1)
    with db.cursor(write=True) as conn:
        conn.execute("UPDATE api_keys SET rate_per_min = 1")
    a = dict(marketplace, **{"X-RapidAPI-User": "alice"})
    b = dict(marketplace, **{"X-RapidAPI-User": "bob"})
    assert _render(client, a).status_code == 200
    assert _render(client, b).status_code == 200      # bob unaffected by alice
    assert _render(client, a).status_code == 429


# --------------------------------------------------------------------------
# Renderer failures are reported honestly
# --------------------------------------------------------------------------

def test_renderer_rejection_is_4xx_not_502(client, auth, monkeypatch):
    """A malformed upload is the caller's problem; 502 says ours is broken."""
    import httpx

    async def boom(path, files, data=None):
        raise main_module.HTTPException(422, "bad input")

    monkeypatch.setattr(main_module, "_gotenberg", boom)
    r = client.post(
        "/v1/convert/office",
        files={"file": ("x.docx", io.BytesIO(b"junk"), "application/octet-stream")},
        headers=auth,
    )
    assert r.status_code == 422
    assert httpx  # imported for parity with the async client under test


# --------------------------------------------------------------------------
# Abuse signals
# --------------------------------------------------------------------------

def test_abuse_report_flags_free_keys_sharing_an_ip():
    a = db.create_key(label="a", plan="free", monthly_quota=50)
    b = db.create_key(label="b", plan="free", monthly_quota=50)
    for raw in (a, b):
        db.record(db.hash_key(raw), "x", 1, 200, 5, ip="198.51.100.1")
    ips = [r["ip"] for r in db.abuse_report()["shared_ip"]]
    assert "198.51.100.1" in ips


def test_abuse_report_ignores_single_key_per_ip():
    raw = db.create_key(label="solo", plan="free", monthly_quota=50)
    db.record(db.hash_key(raw), "x", 1, 200, 5, ip="198.51.100.99")
    ips = [r["ip"] for r in db.abuse_report()["shared_ip"]]
    assert "198.51.100.99" not in ips


def test_abuse_report_flags_key_used_from_many_ips():
    raw = db.create_key(label="shared", plan="pro", monthly_quota=1000)
    for i in range(8):
        db.record(db.hash_key(raw), "x", 1, 200, 5, ip=f"198.51.100.{i}")
    flagged = [r["key_hash"] for r in db.abuse_report()["many_ips"]]
    assert db.hash_key(raw) in flagged


@pytest.mark.parametrize("plan,quota", [("free", 50), ("starter", 5000), ("pro", 50000)])
def test_published_plan_quotas(plan, quota):
    """Pricing on the landing page must match what keys actually get."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ctl", __import__("pathlib").Path(__file__).parent.parent / "gateway" / "docforgectl.py"
    )
    assert spec and spec.loader
    # Import without executing main(); the module only defines PLANS at import.
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules.setdefault("ctl", mod)
    spec.loader.exec_module(mod)
    assert mod.PLANS[plan] == quota
