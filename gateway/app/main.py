"""DocForge API gateway.

Two capabilities behind one process:

  Documents  /v1/convert/*, /v1/pdf/*   — office + HTML to PDF, merging
  Images     /v1/image/*                — HTML templates to PNG/JPEG/WebP

Both delegate the actual rendering to Gotenberg (Apache-2.0), which wraps
LibreOffice and Chromium. What lives here is the part Gotenberg deliberately
does not do: authentication, quota, metering, concurrency control, and a
template layer.

Nothing here scrapes or fetches third-party data on its own behalf. Customers
send us their own files and their own templates. That keeps the service clear
of platform terms-of-service questions entirely.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import select_autoescape
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

from . import db

GOTENBERG = os.environ.get("DOCFORGE_GOTENBERG_URL", "http://gotenberg:3000")
PUBLIC_HOST = os.environ.get("DOCFORGE_DOMAIN", "your-host")
FREE_TIER = int(os.environ.get("DOCFORGE_FREE_TIER_MONTHLY", "250"))
MAX_DOCS = int(os.environ.get("DOCFORGE_MAX_CONCURRENT_DOCS", "6"))
MAX_IMAGES = int(os.environ.get("DOCFORGE_MAX_CONCURRENT_IMAGES", "4"))

# These semaphores are the safety valve for the whole box. Requests beyond the
# limit wait rather than spawning more LibreOffice/Chromium processes, so a
# traffic spike shows up as latency instead of an OOM kill on the media stack.
_doc_sem = asyncio.Semaphore(MAX_DOCS)
_img_sem = asyncio.Semaphore(MAX_IMAGES)

# SANDBOXED, not a plain Environment. Callers supply the template *source*, not
# just the data, so a plain Environment is a server-side template injection hole:
# `{{ "".__class__.__mro__[1].__subclasses__() }}` walks from a string literal to
# every loaded class, and `{{ cycler.__init__.__globals__ }}` reaches module
# globals — the standard path from "renders a banner" to remote code execution.
# SandboxedEnvironment blocks attribute access to internals and unsafe calls.
#
# Autoescaping is the separate, lesser concern: an apostrophe in a customer's
# name shouldn't break the layout or inject markup.
_jinja = SandboxedEnvironment(autoescape=select_autoescape(["html", "xml"]))

# A template is a design, not a document. Anything larger is either a mistake or
# an attempt to make the parser do expensive work.
MAX_TEMPLATE_BYTES = 256 * 1024

app = FastAPI(
    title="DocForge API",
    version="1.0.0",
    description="Document conversion and template image rendering.",
    docs_url="/docs",
)

_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _client
    db.init()
    # Long timeout because a cold LibreOffice start is genuinely ~3s and an
    # 80-page DOCX is slower still. Keepalives keep Gotenberg warm.
    _client = httpx.AsyncClient(
        base_url=GOTENBERG,
        timeout=httpx.Timeout(180.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client:
        await _client.aclose()


# --------------------------------------------------------------------------
# Auth + metering
# --------------------------------------------------------------------------

class Caller:
    def __init__(self, key_hash: str, plan: str, quota: int, used: int):
        self.key_hash = key_hash
        self.plan = plan
        self.quota = quota
        self.used = used


async def authenticate(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> Caller:
    """Accept either `Authorization: Bearer <key>` or `X-API-Key: <key>`.

    RapidAPI and most agent frameworks send one or the other; supporting both
    removes an integration step that would otherwise cost us signups.
    """
    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    elif x_api_key:
        raw = x_api_key.strip()

    if not raw:
        raise HTTPException(401, "Missing API key. Send 'Authorization: Bearer <key>'.")

    row = db.lookup_key(raw)
    if row is None:
        raise HTTPException(401, "Invalid or revoked API key.")

    used = db.month_usage(row["key_hash"])
    quota = int(row["monthly_quota"])
    if quota > 0 and used >= quota:
        raise HTTPException(
            429,
            f"Monthly quota of {quota} exhausted ({used} used). "
            "Quota resets on the 1st (UTC).",
        )
    return Caller(row["key_hash"], row["plan"], quota, used)


def _meter(caller: Caller, endpoint: str, units: int, status: int, started: float) -> None:
    db.record(caller.key_hash, endpoint, units, status, int((time.time() - started) * 1000))


async def _gotenberg(path: str, files: list, data: dict | None = None) -> bytes:
    # `data` must be a Mapping. httpx treats a non-Mapping `data=` as raw request
    # content and wraps it in a *sync* byte stream, which an AsyncClient then
    # refuses to send ("Attempted to send an sync request with an AsyncClient").
    assert _client is not None
    try:
        resp = await _client.post(path, files=files, data=data or {})
    # Infrastructure failures keep their cause chained, so the traceback in our
    # logs still says which call actually broke.
    except httpx.TimeoutException as exc:
        raise HTTPException(
            504, "Rendering timed out. Try a smaller document."
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            502, f"Rendering backend unavailable: {exc.__class__.__name__}"
        ) from exc

    if resp.status_code >= 400:
        # Gotenberg's own message is genuinely useful (unsupported format, bad
        # HTML, and so on), so pass it through rather than swallowing it.
        detail = resp.text[:400] or "Rendering failed."
        raise HTTPException(422 if resp.status_code == 400 else 502, detail)
    return resp.content


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

OFFICE_EXT = {
    ".doc", ".docx", ".odt", ".rtf", ".txt",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
}


@app.post("/v1/convert/office", tags=["Documents"])
async def convert_office(
    file: UploadFile = File(..., description="DOCX, XLSX, PPTX, ODT, RTF, CSV…"),
    landscape: bool = Form(False),
    caller: Caller = Depends(authenticate),
):
    """Convert an office document to PDF. One unit per call."""
    started = time.time()
    name = (file.filename or "document").lower()
    if not any(name.endswith(e) for e in OFFICE_EXT):
        _meter(caller, "convert/office", 0, 415, started)
        raise HTTPException(415, f"Unsupported file type. Supported: {sorted(OFFICE_EXT)}")

    payload = await file.read()
    if not payload:
        raise HTTPException(400, "Empty file.")

    async with _doc_sem:
        pdf = await _gotenberg(
            "/forms/libreoffice/convert",
            files=[("files", (file.filename, payload))],
            data={"landscape": str(landscape).lower()},
        )

    _meter(caller, "convert/office", 1, 200, started)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="converted.pdf"'},
    )


@app.post("/v1/convert/html", tags=["Documents"])
async def convert_html(
    html: str = Form(..., description="Full HTML document"),
    landscape: bool = Form(False),
    margin: float = Form(0.39, description="Margin in inches"),
    caller: Caller = Depends(authenticate),
):
    """Render an HTML string to PDF. One unit per call."""
    started = time.time()
    async with _doc_sem:
        pdf = await _gotenberg(
            "/forms/chromium/convert/html",
            files=[("files", ("index.html", html.encode("utf-8"), "text/html"))],
            data={
                "landscape": str(landscape).lower(),
                "marginTop": str(margin), "marginBottom": str(margin),
                "marginLeft": str(margin), "marginRight": str(margin),
            },
        )
    _meter(caller, "convert/html", 1, 200, started)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="document.pdf"'})


@app.post("/v1/pdf/merge", tags=["Documents"])
async def merge_pdfs(
    files: list[UploadFile] = File(..., description="Two or more PDFs, in order"),
    caller: Caller = Depends(authenticate),
):
    """Merge PDFs into one. One unit per call regardless of file count."""
    started = time.time()
    if len(files) < 2:
        raise HTTPException(400, "Provide at least two PDFs to merge.")

    parts = []
    for i, f in enumerate(files):
        body = await f.read()
        if not body:
            raise HTTPException(400, f"File {i + 1} ({f.filename}) is empty.")
        # Gotenberg merges in filename order, so number them to preserve the
        # order the caller uploaded rather than whatever they happened to name.
        parts.append(("files", (f"{i:04d}.pdf", body, "application/pdf")))

    async with _doc_sem:
        pdf = await _gotenberg("/forms/pdfengines/merge", files=parts)

    _meter(caller, "pdf/merge", 1, 200, started)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="merged.pdf"'})


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

@app.post("/v1/image/render", tags=["Images"])
async def render_image(
    request: Request,
    caller: Caller = Depends(authenticate),
):
    """Render an HTML template plus JSON data to an image.

    Body (JSON):
        template : HTML with Jinja2 placeholders, e.g. "<h1>{{ title }}</h1>"
        data     : object substituted into the template
        width    : viewport width  (default 1200 — standard OG image)
        height   : viewport height (default 630)
        format   : png | jpeg | webp

    The competitors that charge $49/1000 for this are doing exactly this and
    calling it a platform. One unit per call.
    """
    started = time.time()
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        # Caller-input errors are suppressed rather than chained — the cause is
        # their payload, and the parser's traceback is noise in our logs.
        raise HTTPException(400, "Body must be JSON.") from None

    template = body.get("template")
    if not isinstance(template, str) or not template.strip():
        raise HTTPException(400, "'template' (HTML string) is required.")
    if len(template.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise HTTPException(
            413, f"Template exceeds {MAX_TEMPLATE_BYTES // 1024} KB."
        )

    data = body.get("data") or {}
    if not isinstance(data, dict):
        raise HTTPException(400, "'data' must be an object.")

    width = int(body.get("width", 1200))
    height = int(body.get("height", 630))
    fmt = str(body.get("format", "png")).lower()
    if fmt not in {"png", "jpeg", "webp"}:
        raise HTTPException(400, "'format' must be png, jpeg or webp.")
    # Bound the viewport. A 20000x20000 render is a cheap way for one caller to
    # occupy Chromium for minutes.
    if not (16 <= width <= 4000) or not (16 <= height <= 4000):
        raise HTTPException(400, "width and height must be between 16 and 4000.")

    try:
        html = _jinja.from_string(template).render(**data)
    except SecurityError:
        # Deliberately terse, and deliberately unchained. Echoing which internal
        # the sandbox blocked — in the response or the logs — just helps someone
        # map the sandbox boundary.
        raise HTTPException(
            422, "Template uses operations that are not permitted."
        ) from None
    except Exception as exc:
        raise HTTPException(422, f"Template error: {exc}") from None

    async with _img_sem:
        img = await _gotenberg(
            "/forms/chromium/screenshot/html",
            files=[("files", ("index.html", html.encode("utf-8"), "text/html"))],
            data={
                "format": fmt,
                "width": str(width),
                "height": str(height),
                # Capture exactly the viewport, so callers get the dimensions
                # they asked for rather than however tall the content grew.
                "clip": "true",
                "optimizeForSpeed": "true",
            },
        )

    _meter(caller, "image/render", 1, 200, started)
    return Response(img, media_type=f"image/{fmt}")


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------

@app.get("/v1/usage", tags=["Account"])
async def usage(caller: Caller = Depends(authenticate)):
    return {
        "plan": caller.plan,
        "monthly_quota": caller.quota,
        "used_this_month": caller.used,
        "remaining": max(0, caller.quota - caller.used) if caller.quota > 0 else None,
    }


@app.get("/health", include_in_schema=False)
async def health():
    assert _client is not None
    try:
        r = await _client.get("/health", timeout=5.0)
        backend = "up" if r.status_code == 200 else "degraded"
    except Exception:
        backend = "down"
    return JSONResponse({"status": "ok", "renderer": backend})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>DocForge API</title>
<style>
 body{{font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      max-width:44rem;margin:4rem auto;padding:0 1.5rem;color:#1a1a1a}}
 code,pre{{background:#f4f4f5;border-radius:4px}}
 code{{padding:.15rem .35rem;font-size:.9em}}
 pre{{padding:1rem;overflow-x:auto}}
 h1{{margin-bottom:.25rem}} .sub{{color:#666;margin-top:0}}
 table{{border-collapse:collapse;width:100%}}
 td,th{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #eee}}
</style></head><body>
<h1>DocForge API</h1>
<p class="sub">Document conversion and template image rendering.</p>

<h2>Endpoints</h2>
<table>
<tr><th>Method</th><th>Path</th><th>Does</th></tr>
<tr><td>POST</td><td><code>/v1/convert/office</code></td><td>DOCX/XLSX/PPTX → PDF</td></tr>
<tr><td>POST</td><td><code>/v1/convert/html</code></td><td>HTML → PDF</td></tr>
<tr><td>POST</td><td><code>/v1/pdf/merge</code></td><td>Merge PDFs</td></tr>
<tr><td>POST</td><td><code>/v1/image/render</code></td><td>Template → PNG/JPEG/WebP</td></tr>
<tr><td>GET</td><td><code>/v1/usage</code></td><td>Your usage this month</td></tr>
</table>

<h2>Quick start</h2>
<pre>curl -X POST https://{PUBLIC_HOST}/v1/convert/office \\
  -H "Authorization: Bearer YOUR_KEY" \\
  -F "file=@report.docx" -o report.pdf</pre>

<p>Free tier: <strong>{FREE_TIER} calls/month</strong>, no card required.
Interactive reference at <a href="/docs">/docs</a>.</p>
</body></html>"""
