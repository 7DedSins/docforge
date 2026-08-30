"""DocForge API gateway.

Two capabilities behind one process:

  Documents  /v1/convert/*, /v1/pdf/*   — office + HTML to PDF, merging
  Images     /v1/image/*                — HTML templates to PNG/JPEG/WebP

Both delegate the actual rendering to Gotenberg (Apache-2.0), which wraps
LibreOffice and Chromium. What lives here is the part Gotenberg deliberately
does not do: authentication, quota, metering, rate limiting, concurrency
fairness, and a template layer.

There is also a no-signup browser demo at `/`, capped per IP. It exists because
an API-only product can only be bought by developers; a page a human can use
widens the audience and gives search engines something to index.

Nothing here fetches third-party data on its own behalf. Callers send their own
files and their own templates.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from .limits import ConcurrencyGuard, RateLimiter
from .ui import LANDING_PAGE

GOTENBERG = os.environ.get("DOCFORGE_GOTENBERG_URL", "http://gotenberg:3000")
PUBLIC_HOST = os.environ.get("DOCFORGE_DOMAIN", "your-host")
FREE_TIER = int(os.environ.get("DOCFORGE_FREE_TIER_MONTHLY", "50"))
MAX_DOCS = int(os.environ.get("DOCFORGE_MAX_CONCURRENT_DOCS", "6"))
MAX_IMAGES = int(os.environ.get("DOCFORGE_MAX_CONCURRENT_IMAGES", "4"))

# Defaults applied when a key does not set its own. Generous enough that a real
# integration never notices, tight enough that one caller cannot monopolise the
# renderer.
DEFAULT_RATE_PER_MIN = int(os.environ.get("DOCFORGE_RATE_PER_MIN", "60"))
DEFAULT_MAX_CONCURRENT = int(os.environ.get("DOCFORGE_KEY_MAX_CONCURRENT", "4"))

# Anonymous browser demo, per IP per day. Small on purpose: it is a taste of the
# product, not a way to use it for free forever.
DEMO_PER_DAY = int(os.environ.get("DOCFORGE_DEMO_PER_DAY", "3"))
DEMO_MAX_BYTES = 5 * 1024 * 1024

# When set, requests must carry this secret in X-RapidAPI-Proxy-Secret. RapidAPI
# adds it to every request it forwards. Without the check, anyone who reads the
# base URL off the marketplace listing can call the API directly and never pay.
RAPIDAPI_PROXY_SECRET = os.environ.get("DOCFORGE_RAPIDAPI_PROXY_SECRET", "").strip()

# The DocForge key marketplace traffic is metered against. Marketplace consumers
# never hold a DocForge key — they authenticate to the marketplace, which proves
# itself to us with the proxy secret — so their calls still need an identity on
# this side for usage to attach to.
RAPIDAPI_KEY = os.environ.get("DOCFORGE_RAPIDAPI_KEY", "").strip()

# Global backstop: protects the host regardless of who is calling.
_doc_sem = asyncio.Semaphore(MAX_DOCS)
_img_sem = asyncio.Semaphore(MAX_IMAGES)

# Per-caller fairness: protects callers from each other.
_rate = RateLimiter(window_seconds=60)
_conc = ConcurrencyGuard()

# SANDBOXED, not a plain Environment. Callers supply the template *source*, not
# just the data, so a plain Environment is a server-side template injection
# hole: `{{ "".__class__.__mro__[1].__subclasses__() }}` walks from a string
# literal to every loaded class — the standard path from "render a banner" to
# remote code execution. Autoescaping is the separate, lesser concern.
_jinja = SandboxedEnvironment(autoescape=select_autoescape(["html", "xml"]))

# A template is a design, not a document.
MAX_TEMPLATE_BYTES = 256 * 1024

app = FastAPI(
    title="DocForge API",
    version="1.1.0",
    description=(
        "Convert office documents and HTML to PDF, merge PDFs, and render "
        "HTML templates to images. One unit per call regardless of file size; "
        "failed calls are never billed."
    ),
    docs_url="/docs",
    # Without an explicit server the importer has no base URL to call, and
    # every generated snippet points at a relative path.
    servers=[{"url": f"https://{PUBLIC_HOST}", "description": "Production"}],
)


def _downgrade_to_30(node: Any) -> Any:
    """Rewrite JSON-Schema 2020-12 nullability into OpenAPI 3.0 form, in place.

    FastAPI renders `Optional[str]` as `anyOf: [{type: string}, {type: "null"}]`.
    That is valid 3.1 and invalid 3.0 — `"null"` is not among 3.0's permitted
    types — so a 3.0 validator rejects the document with one error per optional
    parameter. 3.0 spells the same thing as `{type: string, nullable: true}`.

    Also handles the list form, `type: ["string", "null"]`.

    Declaring `openapi: 3.0.3` without this is dishonest: the header claims a
    version the body does not satisfy, which is exactly how the first attempt
    failed validation on upload.
    """
    if isinstance(node, list):
        return [_downgrade_to_30(v) for v in node]
    if not isinstance(node, dict):
        return node

    for combinator in ("anyOf", "oneOf"):
        options = node.get(combinator)
        if not isinstance(options, list):
            continue
        non_null = [o for o in options
                    if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(non_null) == len(options):
            continue  # no null member; leave the combinator alone
        node.pop(combinator)
        node["nullable"] = True
        if len(non_null) == 1 and isinstance(non_null[0], dict):
            # Single remaining option collapses into the parent, which is the
            # readable 3.0 shape and what generators expect.
            for k, v in non_null[0].items():
                node.setdefault(k, v)
        elif non_null:
            node[combinator] = non_null

    # `type: ["string", "null"]` — the other 2020-12 nullability spelling.
    if isinstance(node.get("type"), list):
        kinds = [t for t in node["type"] if t != "null"]
        if len(kinds) < len(node["type"]):
            node["nullable"] = True
        node["type"] = kinds[0] if len(kinds) == 1 else kinds

    for key, value in list(node.items()):
        node[key] = _downgrade_to_30(value)
    return node


def _openapi_30() -> dict:
    """Emit the schema as OpenAPI 3.0.3 rather than FastAPI's default 3.1.0.

    Marketplaces and code generators — RapidAPI among them — still parse 3.0,
    and given a 3.1 document they either reject it outright or import it with
    operations silently missing.

    This has to override `app.openapi` rather than pass `openapi_version=` to
    `FastAPI(...)`: that is not a constructor parameter (checked, 0.115.6), so
    it lands in `**extra` and is ignored without any error. The document still
    said 3.1.0 and nothing indicated why.

    Only the declared version changes. This API uses no 3.1-only JSON Schema
    constructs, so the body is already valid 3.0 — there is a test asserting
    that stays true.
    """
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version="3.0.3",
        description=app.description,
        routes=app.routes,
        servers=app.servers,
    )
    # get_openapi honours the version string but still emits a 2020-12 body.
    app.openapi_schema = _downgrade_to_30(schema)
    return app.openapi_schema


app.openapi = _openapi_30

_client: httpx.AsyncClient | None = None
_sweeper: asyncio.Task | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _client, _sweeper
    db.init()
    # Long timeout because a cold LibreOffice start is genuinely ~3s and a big
    # DOCX is slower still. Keepalives keep Gotenberg warm.
    _client = httpx.AsyncClient(
        base_url=GOTENBERG,
        timeout=httpx.Timeout(180.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
    )

    async def sweep_loop() -> None:
        while True:
            await asyncio.sleep(300)
            await _rate.sweep()

    _sweeper = asyncio.create_task(sweep_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _sweeper:
        _sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _sweeper
    if _client:
        await _client.aclose()


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def client_ip(request: Request) -> str:
    """The address Caddy actually saw.

    Caddy *appends* the observed peer to any X-Forwarded-For the client sent, so
    the last entry is the only one we can trust — the earlier ones are
    attacker-controlled. Taking the first entry (the usual mistake) would let a
    caller forge a different IP per request and walk straight through the demo's
    per-IP cap.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else ""


class Caller:
    def __init__(self, row, used: int, ip: str, subject: str,
                 via_marketplace: bool = False):
        # How the caller authenticated, not merely whether an upstream user id
        # happened to arrive. A marketplace request with no X-RapidAPI-User must
        # still be treated as marketplace — otherwise it falls through to the
        # direct branch and is told the shared key's plan.
        self.via_marketplace = via_marketplace
        self.key_hash = row["key_hash"]
        self.plan = row["plan"]
        self.quota = int(row["monthly_quota"])
        self.used = used
        self.ip = ip
        # Upstream identity when the request arrives via a marketplace. One
        # marketplace key may front many end users, so rate limits key off this
        # when present — otherwise one busy RapidAPI user throttles all of them.
        self.subject = subject
        self.rate = int(row["rate_per_min"]) or DEFAULT_RATE_PER_MIN
        self.max_concurrent = int(row["max_concurrent"]) or DEFAULT_MAX_CONCURRENT

    @property
    def bucket(self) -> str:
        return f"{self.key_hash}:{self.subject}" if self.subject else self.key_hash


async def authenticate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_rapidapi_proxy_secret: str | None = Header(default=None),
    x_rapidapi_user: str | None = Header(default=None),
) -> Caller:
    """Resolve the caller, or reject.

    Two ways in, deliberately.

    **Marketplace.** The consumer authenticates to RapidAPI. RapidAPI strips its
    own key and forwards `X-RapidAPI-Proxy-Secret` plus `X-RapidAPI-User`. There
    is no DocForge key in that request and there never will be — demanding one
    401s every marketplace call, which is exactly how this was broken.

    **Direct.** The customer sends a DocForge key we issued, as
    `Authorization: Bearer <key>` or `X-API-Key: <key>`. Supporting both removes
    an integration step that would otherwise cost signups.

    Leaving the direct path open alongside the marketplace one does not create a
    billing hole: bypassing the marketplace still requires a key we issued.
    """
    supplied_secret = (x_rapidapi_proxy_secret or "").strip()
    subject = ""
    via_marketplace = False

    if RAPIDAPI_PROXY_SECRET and supplied_secret:
        # Constant-time compare: a plain `!=` leaks the secret one byte at a
        # time to anyone willing to measure response latency.
        if not secrets_equal(supplied_secret, RAPIDAPI_PROXY_SECRET):
            raise HTTPException(403, "Invalid marketplace proxy secret.")
        if not RAPIDAPI_KEY:
            raise HTTPException(
                503, "Marketplace access is not configured on this deployment."
            )
        raw = RAPIDAPI_KEY
        via_marketplace = True
        # One marketplace key fronts every consumer, so rate limiting and
        # concurrency bucket on this instead — otherwise one busy subscriber
        # throttles all of them.
        subject = (x_rapidapi_user or "").strip()
    else:
        raw = None
        if authorization and authorization.lower().startswith("bearer "):
            raw = authorization[7:].strip()
        elif x_api_key:
            raw = x_api_key.strip()
        if not raw:
            raise HTTPException(
                401, "Missing API key. Send 'Authorization: Bearer <key>'."
            )

    row = db.lookup_key(raw)
    if row is None:
        raise HTTPException(401, "Invalid or revoked API key.")

    caller = Caller(row, db.month_usage(row["key_hash"]), client_ip(request),
                    subject, via_marketplace)

    if caller.quota > 0 and caller.used >= caller.quota:
        raise HTTPException(
            429,
            f"Monthly quota of {caller.quota} exhausted ({caller.used} used). "
            "Quota resets on the 1st (UTC).",
            headers={"X-Quota-Limit": str(caller.quota),
                     "X-Quota-Used": str(caller.used)},
        )

    allowed, retry = await _rate.check(caller.bucket, caller.rate)
    if not allowed:
        raise HTTPException(
            429,
            f"Rate limit of {caller.rate} requests/minute exceeded.",
            headers={"Retry-After": str(retry)},
        )

    return caller


def secrets_equal(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


def _meter(caller: Caller, endpoint: str, units: int, status: int, started: float) -> None:
    db.record(caller.key_hash, endpoint, units, status,
              int((time.time() - started) * 1000), caller.ip, caller.subject)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

async def _gotenberg(path: str, files: list, data: dict | None = None) -> bytes:
    # `data` must be a Mapping. httpx treats a non-Mapping `data=` as raw
    # request content and wraps it in a *sync* byte stream, which an
    # AsyncClient then refuses to send.
    assert _client is not None
    try:
        resp = await _client.post(path, files=files, data=data or {})
    # Infrastructure failures keep their cause chained, so the traceback in our
    # logs still says which call actually broke. These are the only genuine 502s.
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Rendering timed out. Try a smaller document.") from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            502, f"Rendering backend unavailable: {exc.__class__.__name__}"
        ) from exc

    if resp.status_code >= 400:
        # We construct these requests ourselves, so if Gotenberg rejects one the
        # only variable is the caller's file. Reporting that as 502 tells the
        # customer our service is broken when they sent a malformed document —
        # it generates support tickets and looks like an outage. 422 is the
        # honest answer, whatever status the renderer chose internally.
        detail = (resp.text or "").strip()[:300]
        raise HTTPException(
            422,
            "The renderer could not process this input. It is most likely "
            "corrupt, password-protected, or not the format its extension "
            f"claims. Renderer said: {detail}" if detail else
            "The renderer could not process this input.",
        )
    return resp.content


@contextlib.asynccontextmanager
async def _slot(caller: Caller, sem: asyncio.Semaphore):
    """Claim a per-caller slot, then a global one, for the duration of a render."""
    if not await _conc.acquire(caller.bucket, caller.max_concurrent):
        raise HTTPException(
            429,
            f"Too many concurrent requests (limit {caller.max_concurrent}). "
            "Wait for one to finish before starting another.",
            headers={"Retry-After": "2"},
        )
    try:
        async with sem:
            yield
    finally:
        await _conc.release(caller.bucket, caller.max_concurrent)


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

OFFICE_EXT = {
    ".doc", ".docx", ".odt", ".rtf", ".txt",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
}


@app.post("/v1/convert/office", tags=["Documents"], operation_id="convertOffice",
          summary="Convert an office document to PDF",
          response_class=Response,
          responses={200: {"content": {"application/pdf": {}},
                           "description": "The converted PDF"}})
async def convert_office(
    file: UploadFile = File(..., description="DOCX, XLSX, PPTX, ODT, RTF, CSV…"),
    landscape: bool = Form(False),
    caller: Caller = Depends(authenticate),
):
    """Convert an office document to PDF. One unit per call."""
    started = time.time()
    name = (file.filename or "document").lower()
    if not any(name.endswith(e) for e in OFFICE_EXT):
        raise HTTPException(415, f"Unsupported file type. Supported: {sorted(OFFICE_EXT)}")

    payload = await file.read()
    if not payload:
        raise HTTPException(400, "Empty file.")

    async with _slot(caller, _doc_sem):
        pdf = await _gotenberg(
            "/forms/libreoffice/convert",
            files=[("files", (file.filename, payload))],
            data={"landscape": str(landscape).lower()},
        )

    _meter(caller, "convert/office", 1, 200, started)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="converted.pdf"'})


@app.post("/v1/convert/html", tags=["Documents"], operation_id="convertHtml",
          summary="Convert HTML to PDF",
          response_class=Response,
          responses={200: {"content": {"application/pdf": {}},
                           "description": "The rendered PDF"}})
async def convert_html(
    html: str = Form(..., description="Full HTML document"),
    landscape: bool = Form(False),
    margin: float = Form(0.39, description="Margin in inches"),
    caller: Caller = Depends(authenticate),
):
    """Render an HTML string to PDF. One unit per call."""
    started = time.time()
    async with _slot(caller, _doc_sem):
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


@app.post("/v1/pdf/merge", tags=["Documents"], operation_id="mergePdfs",
          summary="Merge several PDFs into one",
          response_class=Response,
          responses={200: {"content": {"application/pdf": {}},
                           "description": "The merged PDF"}})
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

    async with _slot(caller, _doc_sem):
        pdf = await _gotenberg("/forms/pdfengines/merge", files=parts)

    _meter(caller, "pdf/merge", 1, 200, started)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="merged.pdf"'})


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

# The body is declared through openapi_extra rather than a Pydantic parameter.
# A declared model would make FastAPI validate and reject before the handler
# runs, replacing specific messages ("width and height must be between 16 and
# 4000") with a generic 422 array. But with no declaration at all, the OpenAPI
# document carries no requestBody — so a marketplace renders the endpoint as
# taking no input and nobody can work out how to call it. This documents the
# body and leaves the runtime contract untouched.
RENDER_IMAGE_BODY = {
    "required": True,
    "content": {"application/json": {"schema": {
        "type": "object",
        "required": ["template"],
        "properties": {
            "template": {
                "type": "string",
                "description": "HTML with Jinja2 placeholders, e.g. <h1>{{ title }}</h1>",
            },
            "data": {
                "type": "object",
                "description": "Values substituted into the template",
            },
            "width": {"type": "integer", "default": 1200, "minimum": 16, "maximum": 4000},
            "height": {"type": "integer", "default": 630, "minimum": 16, "maximum": 4000},
            "format": {"type": "string", "enum": ["png", "jpeg", "webp"], "default": "png"},
        },
        "example": {
            "template": "<div style='width:1200px;height:630px;display:flex;"
                        "align-items:center;justify-content:center;background:#0f172a;"
                        "font-family:sans-serif'><h1 style='color:#fff;font-size:72px'>"
                        "{{ title }}</h1></div>",
            "data": {"title": "Hello world"},
            "width": 1200, "height": 630, "format": "png",
        },
    }}},
}


@app.post("/v1/image/render", tags=["Images"], operation_id="renderImage",
          summary="Render an HTML template to an image",
          response_class=Response,
          openapi_extra={"requestBody": RENDER_IMAGE_BODY},
          responses={200: {"content": {"image/png": {}, "image/jpeg": {},
                                       "image/webp": {}},
                           "description": "The rendered image"}})
async def render_image(request: Request, caller: Caller = Depends(authenticate)):
    """Render an HTML template plus JSON data to an image.

    Body: `template` (HTML with Jinja2 placeholders), `data` (object),
    `width`, `height`, `format` (png|jpeg|webp). Default 1200x630 is the
    standard Open Graph card. One unit per call.
    """
    started = time.time()
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON.") from None

    template = body.get("template")
    if not isinstance(template, str) or not template.strip():
        raise HTTPException(400, "'template' (HTML string) is required.")
    if len(template.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise HTTPException(413, f"Template exceeds {MAX_TEMPLATE_BYTES // 1024} KB.")

    data = body.get("data") or {}
    if not isinstance(data, dict):
        raise HTTPException(400, "'data' must be an object.")

    width, height = int(body.get("width", 1200)), int(body.get("height", 630))
    fmt = str(body.get("format", "png")).lower()
    if fmt not in {"png", "jpeg", "webp"}:
        raise HTTPException(400, "'format' must be png, jpeg or webp.")
    # Bound the viewport. A 20000x20000 render occupies Chromium for minutes.
    if not (16 <= width <= 4000) or not (16 <= height <= 4000):
        raise HTTPException(400, "width and height must be between 16 and 4000.")

    try:
        html = _jinja.from_string(template).render(**data)
    except SecurityError:
        # Terse and unchained. Naming the blocked internal — in the response or
        # the logs — helps someone map the sandbox boundary.
        raise HTTPException(422, "Template uses operations that are not permitted.") from None
    except Exception as exc:
        raise HTTPException(422, f"Template error: {exc}") from None

    async with _slot(caller, _img_sem):
        img = await _gotenberg(
            "/forms/chromium/screenshot/html",
            files=[("files", ("index.html", html.encode("utf-8"), "text/html"))],
            data={"format": fmt, "width": str(width), "height": str(height),
                  "clip": "true", "optimizeForSpeed": "true"},
        )

    _meter(caller, "image/render", 1, 200, started)
    return Response(img, media_type=f"image/{fmt}")


# --------------------------------------------------------------------------
# Anonymous demo — no key, capped per IP
# --------------------------------------------------------------------------

@app.post("/try/convert", include_in_schema=False)
async def demo_convert(request: Request, file: UploadFile = File(...)):
    """Convert one document without an API key.

    Rate limited by IP and deliberately small. This exists so a human can see
    the product work before deciding to sign up — an API-only product can only
    be evaluated by someone willing to write a curl command first.
    """
    ip = client_ip(request)
    if not ip:
        raise HTTPException(400, "Could not determine client address.")

    used = db.demo_count_today(ip)
    if used >= DEMO_PER_DAY:
        raise HTTPException(
            429,
            f"The free demo allows {DEMO_PER_DAY} conversions per day. "
            "Grab an API key for more — the free tier is larger.",
        )

    name = (file.filename or "document").lower()
    if not any(name.endswith(e) for e in OFFICE_EXT):
        raise HTTPException(415, "Upload a DOCX, XLSX, PPTX, ODT, RTF or CSV file.")

    payload = await file.read()
    if not payload:
        raise HTTPException(400, "Empty file.")
    if len(payload) > DEMO_MAX_BYTES:
        raise HTTPException(413, f"Demo files are limited to {DEMO_MAX_BYTES // 1024 // 1024} MB.")

    # Counted before rendering, not after. Counting on success lets a caller
    # spend our CPU for free by sending files they know will fail.
    db.demo_record(ip)

    async with _doc_sem:
        pdf = await _gotenberg(
            "/forms/libreoffice/convert",
            files=[("files", (file.filename, payload))],
        )

    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="converted.pdf"',
        "X-Demo-Remaining": str(max(0, DEMO_PER_DAY - used - 1)),
    })


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------

@app.get("/v1/usage", tags=["Account"], operation_id="getUsage",
         summary="Your usage and limits")
async def usage(caller: Caller = Depends(authenticate)):
    """Usage for the caller.

    Marketplace callers get a different answer to direct ones, deliberately.
    Every marketplace subscriber arrives under a single shared key, so the
    key's plan and running total describe the marketplace as a whole, not the
    person asking. Returning those would tell a subscriber on a 50-call plan
    that they have unlimited quota, and would disclose the combined call volume
    of every other subscriber.
    """
    if caller.via_marketplace:
        return {
            "billing": "marketplace",
            # The marketplace owns the quota and reports it in its own
            # X-RateLimit-* response headers; we cannot see the caller's plan.
            "monthly_quota": None,
            "used_this_month": (
                db.month_usage_by_subject(caller.key_hash, caller.subject)
                if caller.subject else None
            ),
            "remaining": None,
            "note": "Quota is set by your marketplace subscription. See the "
                    "X-RateLimit-Requests-Remaining response header.",
            "rate_limit_per_minute": caller.rate,
            "max_concurrent": caller.max_concurrent,
        }

    return {
        "billing": "direct",
        "plan": caller.plan,
        "monthly_quota": caller.quota,
        "used_this_month": caller.used,
        "remaining": max(0, caller.quota - caller.used) if caller.quota > 0 else None,
        "rate_limit_per_minute": caller.rate,
        "max_concurrent": caller.max_concurrent,
    }


@app.get("/health", include_in_schema=False)
async def health():
    assert _client is not None
    try:
        r = await _client.get("/health", timeout=5.0)
        backend = "up" if r.status_code == 200 else "degraded"
    except Exception:
        backend = "down"
    # Never 503 here. This endpoint is what an uptime monitor watches; a failing
    # renderer should show as degraded, not take the whole service down.
    return JSONResponse({"status": "ok", "renderer": backend})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    return LANDING_PAGE.replace("{{HOST}}", PUBLIC_HOST) \
                       .replace("{{FREE_TIER}}", str(FREE_TIER)) \
                       .replace("{{DEMO_PER_DAY}}", str(DEMO_PER_DAY))
