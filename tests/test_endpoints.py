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
