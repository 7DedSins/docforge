"""Endpoint contracts: validation, routing and response shape."""

from __future__ import annotations

import io

import pytest

from .conftest import FAKE_RENDER

# --------------------------------------------------------------------------
# Image rendering
# --------------------------------------------------------------------------

def test_render_returns_image_bytes(client, auth):
    r = client.post(
        "/v1/image/render",
        json={"template": "<p>hi</p>", "data": {}, "format": "png"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == FAKE_RENDER


@pytest.mark.parametrize("fmt", ["png", "jpeg", "webp"])
def test_supported_image_formats(client, auth, fmt):
    r = client.post(
        "/v1/image/render",
        json={"template": "<p>x</p>", "format": fmt},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == f"image/{fmt}"


def test_unsupported_format_rejected(client, auth):
    r = client.post(
        "/v1/image/render", json={"template": "<p>x</p>", "format": "gif"}, headers=auth
    )
    assert r.status_code == 400


@pytest.mark.parametrize(
    "w,h",
    [(0, 100), (100, 0), (5000, 100), (100, 5000), (-1, 100)],
    ids=["zero-w", "zero-h", "wide", "tall", "negative"],
)
def test_dimension_bounds_enforced(client, auth, w, h):
    """Unbounded dimensions let one caller occupy the renderer for minutes."""
    r = client.post(
        "/v1/image/render",
        json={"template": "<p>x</p>", "width": w, "height": h},
        headers=auth,
    )
    assert r.status_code == 400


def test_missing_template_rejected(client, auth):
    assert client.post("/v1/image/render", json={}, headers=auth).status_code == 400


def test_non_object_data_rejected(client, auth):
    r = client.post(
        "/v1/image/render",
        json={"template": "<p>x</p>", "data": ["not", "an", "object"]},
        headers=auth,
    )
    assert r.status_code == 400


def test_default_size_is_open_graph(client, auth, stub_renderer):
    client.post("/v1/image/render", json={"template": "<p>x</p>"}, headers=auth)
    sent = stub_renderer[0]["data"]
    assert sent["width"] == "1200"
    assert sent["height"] == "630"


# --------------------------------------------------------------------------
# HTML -> PDF
# --------------------------------------------------------------------------

def test_html_to_pdf(client, auth):
    r = client.post("/v1/convert/html", data={"html": "<h1>hi</h1>"}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == FAKE_RENDER


def test_html_to_pdf_honours_landscape(client, auth, stub_renderer):
    client.post(
        "/v1/convert/html", data={"html": "<h1>hi</h1>", "landscape": "true"},
        headers=auth,
    )
    assert stub_renderer[0]["data"]["landscape"] == "true"


# --------------------------------------------------------------------------
# Office -> PDF
# --------------------------------------------------------------------------

def test_office_conversion(client, auth):
    r = client.post(
        "/v1/convert/office",
        files={"file": ("report.docx", io.BytesIO(b"fake docx"), "application/octet-stream")},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"


def test_unsupported_extension_rejected(client, auth):
    r = client.post(
        "/v1/convert/office",
        files={"file": ("malware.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
        headers=auth,
    )
    assert r.status_code == 415


def test_empty_upload_rejected(client, auth):
    r = client.post(
        "/v1/convert/office",
        files={"file": ("empty.docx", io.BytesIO(b""), "application/octet-stream")},
        headers=auth,
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------
# PDF merge
# --------------------------------------------------------------------------

def test_merge_requires_two_files(client, auth):
    r = client.post(
        "/v1/pdf/merge",
        files=[("files", ("a.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf"))],
        headers=auth,
    )
    assert r.status_code == 400


def test_merge_preserves_upload_order(client, auth, stub_renderer):
    """Gotenberg merges in filename order, so uploads are renumbered."""
    client.post(
        "/v1/pdf/merge",
        files=[
            ("files", ("zebra.pdf", io.BytesIO(b"%PDF-1"), "application/pdf")),
            ("files", ("apple.pdf", io.BytesIO(b"%PDF-2"), "application/pdf")),
        ],
        headers=auth,
    )
    names = [f[1][0] for f in stub_renderer[0]["files"]]
    assert names == ["0000.pdf", "0001.pdf"]


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------

def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_landing_page_public(client):
    assert client.get("/").status_code == 200


def test_openapi_schema_generates(client):
    schema = client.get("/openapi.json").json()
    for path in ("/v1/convert/office", "/v1/convert/html",
                 "/v1/pdf/merge", "/v1/image/render"):
        assert path in schema["paths"], f"{path} missing from OpenAPI schema"


def test_openapi_is_30_for_marketplace_importers(client):
    """RapidAPI and most generators parse 3.0; a 3.1 document imports badly.

    FastAPI defaults to 3.1.0, and `FastAPI(openapi_version=...)` is not a real
    parameter — it lands in **extra and is silently ignored — so this is
    enforced by overriding app.openapi.
    """
    schema = client.get("/openapi.json").json()
    assert schema["openapi"].startswith("3.0")


def test_openapi_declares_a_server(client):
    """Without this, imported snippets point at a relative path."""
    schema = client.get("/openapi.json").json()
    assert schema["servers"]
    assert schema["servers"][0]["url"].startswith("http")


# The exact set OpenAPI 3.0 permits. "null" is absent — that is the whole point.
OPENAPI_30_TYPES = {"array", "boolean", "integer", "number", "object", "string"}


def _walk(node, path=""):
    """Yield (path, node) for every dict in the document."""
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def test_openapi_declares_no_type_outside_the_30_set(client):
    """RapidAPI rejected the first upload with 20 of exactly this error.

    FastAPI renders Optional[str] as anyOf:[{type:string},{type:"null"}], which
    is valid 3.1 and invalid 3.0 — one ENUM_VALUE_NOT_ALLOWED per optional
    parameter. Checking for `const`/`prefixItems`, as an earlier version of this
    test did, does not catch it.
    """
    schema = client.get("/openapi.json").json()
    offenders = [
        (path, node["type"])
        for path, node in _walk(schema)
        if isinstance(node.get("type"), str) and node["type"] not in OPENAPI_30_TYPES
    ]
    assert not offenders, f"types invalid in OpenAPI 3.0: {offenders}"


def test_openapi_has_no_list_valued_types(client):
    """`type: [...]` is 2020-12 only; 3.0 requires a single string."""
    schema = client.get("/openapi.json").json()
    offenders = [p for p, n in _walk(schema) if isinstance(n.get("type"), list)]
    assert not offenders, f"list-valued type at: {offenders}"


def test_optional_params_use_nullable_not_a_null_union(client):
    """The 3.0 spelling of optional, and proof the downgrade actually ran."""
    schema = client.get("/openapi.json").json()
    params = schema["paths"]["/v1/usage"]["get"]["parameters"]
    assert params, "expected the optional auth headers to be documented"
    for p in params:
        assert p["schema"].get("nullable") is True
        assert "anyOf" not in p["schema"]
