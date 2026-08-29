"""Shared fixtures.

Tests run against a temporary SQLite file and a stubbed renderer. Gotenberg is
deliberately not exercised here — converting a DOCX is Gotenberg's job and it
has its own test suite. What needs covering is everything *we* wrote around it:
authentication, quota accounting, input validation, and the template sandbox.

That keeps CI hermetic and fast (no containers, no network), which is what makes
it useful as a pre-merge gate.
"""

from __future__ import annotations

import os
import tempfile

import pytest

# db.py resolves DB_PATH at import time, so this must be set before the app is
# imported anywhere in the test session.
_tmp = tempfile.mkdtemp(prefix="docforge-tests-")
os.environ["DOCFORGE_DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ.setdefault("DOCFORGE_DOMAIN", "test.local")

from app import db  # noqa: E402
from app import main as main_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# A recognisable stand-in for rendered output. Starts with the PDF magic bytes
# so a test can assert the body was passed through untouched.
FAKE_RENDER = b"%PDF-1.7\nstub render payload\n%%EOF"


@pytest.fixture(autouse=True)
def clean_db():
    """Fresh schema per test, so quota counts never leak between them."""
    db.init()
    with db.cursor(write=True) as conn:
        conn.execute("DELETE FROM usage")
        conn.execute("DELETE FROM api_keys")
    yield


@pytest.fixture
def stub_renderer(monkeypatch):
    """Replace the Gotenberg call with a stub that records what it was sent."""
    calls = []

    async def _fake(path, files, data=None):
        calls.append({"path": path, "files": files, "data": data or {}})
        return FAKE_RENDER

    monkeypatch.setattr(main_module, "_gotenberg", _fake)
    return calls


@pytest.fixture
def client(stub_renderer):
    # TestClient triggers startup, which opens the real httpx client to
    # Gotenberg. Nothing dials out because _gotenberg is stubbed.
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture
def key():
    """A key with plenty of quota."""
    return db.create_key(label="test", plan="pro", monthly_quota=1000)


@pytest.fixture
def auth(key):
    return {"Authorization": f"Bearer {key}"}
