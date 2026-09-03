"""DocForge exposed as an MCP server.

Why this exists as a third product rather than a feature: the buyer is
different. DocForge and ImageForge sell to a developer who writes an HTTP
client. This sells to an AI agent that needs to *produce a file* — turn a
draft into a PDF, generate a share image — and to the person paying for that
agent. MCP marketplaces list it and handle billing at an 80% revenue share,
which is a distribution channel that costs nothing to enter.

Transport is streamable HTTP so the server can be hosted and listed publicly,
rather than stdio which only works for a local subprocess.

Agents are untrusted callers by design. Every tool here either accepts content
the caller supplies or fetches nothing at all — there is no tool that lets a
caller make us retrieve an arbitrary URL, because that would turn this service
into an open proxy the moment it is listed.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from contextvars import ContextVar

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

GATEWAY = os.environ.get("DOCFORGE_GATEWAY_URL", "http://gateway:8000")
# The key the MCP server presents to the gateway. Marketplace-billed calls all
# arrive under this identity, so its usage row is what we reconcile against the
# marketplace payout.
#
# It must be a key the gateway currently recognises. Keys are minted with a
# `df_` prefix; a `forge_` value here is a leftover from before the rename and
# every tool call will fail with 401 while discovery keeps answering 200.
API_KEY = os.environ.get("DOCFORGE_API_KEY", "")
PUBLIC_HOST = os.environ.get("DOCFORGE_DOMAIN", "localhost")

# MCP marketplaces resell this server: they take the payment, then forward the
# call with a shared secret proving it came through them. Without the check,
# anyone holding the URL invokes the tools directly and the marketplace never
# bills — and listing review tests for exactly this.
#
# Empty disables enforcement, which is the right state when self-hosting.
PROXY_SECRET = os.environ.get("DOCFORGE_MCP_PROXY_SECRET", "").strip()

# Each marketplace names its header differently. Matching against a list means
# adding a second marketplace is a config change rather than a code change.
#
# Read as "empty or unset means the default". Compose expands an unset variable
# to an empty string rather than omitting it, so a plain os.environ.get default
# would be silently replaced by "" — leaving no headers to match and rejecting
# every marketplace call.
_DEFAULT_SECRET_HEADERS = "x-agenticmarket-secret,x-mcpize-secret,x-mcp-proxy-secret"
SECRET_HEADERS = tuple(
    h.strip().lower()
    for h in (
        os.environ.get("DOCFORGE_MCP_SECRET_HEADERS", "").strip()
        or _DEFAULT_SECRET_HEADERS
    ).split(",")
    if h.strip()
)

# Only billable work is gated. Discovery stays open deliberately: marketplace
# review reads tool descriptions from `tools/list` and their uptime probes call
# `initialize`, and neither carries the secret. Gating those fails review just
# as surely as gating nothing.
GATED_METHODS = frozenset({"tools/call"})

# Headers a direct caller may present their own DocForge key in.
CALLER_KEY_HEADERS = ("authorization", "x-api-key")

# The key to present to the gateway for the request in flight.
#
# Without this every MCP call billed to one shared identity, so a public
# listing would meter thousands of strangers against a single unlimited key —
# no quotas, no per-user usage, nothing to charge anyone for. A caller who
# brings their own key is metered as themselves, exactly like the REST API.
#
# Set by the middleware before the app is awaited, so it propagates into the
# tool coroutine and into any task the transport spawns from there.
_request_key: ContextVar[str] = ContextVar("_request_key", default="")


def _auth_header() -> dict[str, str]:
    """Auth for the gateway call: the caller's key, else the server's own."""
    return {"Authorization": f"Bearer {_request_key.get() or API_KEY}"}


def _caller_key(headers: dict[str, str]) -> str:
    """Pull a DocForge key out of the request, if the caller supplied one."""
    raw = ""
    for name in CALLER_KEY_HEADERS:
        value = headers.get(name, "").strip()
        if value:
            raw = value
            break
    # `Authorization: Bearer df_...` and a bare `X-API-Key: df_...` both arrive
    # here; normalise to the token itself.
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw

mcp = MCPServer(
    "docforge",
    title="DocForge — documents and images",
    instructions=(
        "Convert documents to PDF and render HTML templates to images. "
        "Use html_to_pdf for reports and invoices, render_image for social "
        "cards and banners, convert_office_document for DOCX/XLSX/PPTX."
    ),
)

# No default Authorization header: auth is decided per request by _auth_header,
# because which key to bill depends on how this caller authenticated.
_client = httpx.AsyncClient(
    base_url=GATEWAY,
    timeout=httpx.Timeout(180.0, connect=10.0),
)

# Agents routinely hand back multi-megabyte base64 blobs and blow their own
# context window. Anything larger than this is refused with a clear message
# rather than silently truncated.
MAX_RETURN_BYTES = 6 * 1024 * 1024


def _encode(payload: bytes, mime: str) -> str:
    if len(payload) > MAX_RETURN_BYTES:
        return (
            f"ERROR: result is {len(payload) // 1024} KB, over the "
            f"{MAX_RETURN_BYTES // 1024} KB limit for inline return. "
            "Use the HTTP API directly for large documents."
        )
    b64 = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{b64}"


@mcp.tool()
async def html_to_pdf(html: str, landscape: bool = False) -> str:
    """Convert an HTML document to PDF.

    Use this when the user wants a report, invoice, or formatted document as a
    PDF file. Supply a complete HTML document including any inline CSS.

    Returns the PDF as a base64 data URI.
    """
    resp = await _client.post(
        "/v1/convert/html",
        data={"html": html, "landscape": str(landscape).lower()},
        headers=_auth_header(),
    )
    if resp.status_code != 200:
        return f"ERROR {resp.status_code}: {resp.text[:300]}"
    return _encode(resp.content, "application/pdf")


@mcp.tool()
async def render_image(
    template: str,
    data: dict | None = None,
    width: int = 1200,
    height: int = 630,
    image_format: str = "png",
) -> str:
    """Render an HTML template to an image.

    Use this for social share images (Open Graph cards), banners, certificates,
    or any picture built from text and layout. The template is HTML with
    Jinja2 placeholders such as {{ title }}; `data` supplies those values.

    Default size 1200x630 is the standard Open Graph card.

    Returns the image as a base64 data URI.
    """
    resp = await _client.post(
        "/v1/image/render",
        json={
            "template": template,
            "data": data or {},
            "width": width,
            "height": height,
            "format": image_format,
        },
        headers=_auth_header(),
    )
    if resp.status_code != 200:
        return f"ERROR {resp.status_code}: {resp.text[:300]}"
    return _encode(resp.content, f"image/{image_format}")


@mcp.tool()
async def convert_office_document(
    filename: str, content_base64: str, landscape: bool = False
) -> str:
    """Convert an office document to PDF.

    Accepts DOCX, XLSX, PPTX, ODT, RTF, CSV and similar formats. `filename`
    must carry the real extension — it is how the converter picks a filter.
    `content_base64` is the raw file, base64 encoded.

    Returns the PDF as a base64 data URI.
    """
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except Exception:
        return "ERROR: content_base64 is not valid base64."
    if not raw:
        return "ERROR: decoded file is empty."

    resp = await _client.post(
        "/v1/convert/office",
        files={"file": (filename, raw)},
        data={"landscape": str(landscape).lower()},
        headers=_auth_header(),
    )
    if resp.status_code != 200:
        return f"ERROR {resp.status_code}: {resp.text[:300]}"
    return _encode(resp.content, "application/pdf")


@mcp.tool()
async def merge_pdfs(files_base64: list[str]) -> str:
    """Merge two or more PDFs into a single document, in the order given.

    Each entry in `files_base64` is one base64-encoded PDF.

    Returns the merged PDF as a base64 data URI.
    """
    if len(files_base64) < 2:
        return "ERROR: provide at least two PDFs."

    parts = []
    for i, b64 in enumerate(files_base64):
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            return f"ERROR: entry {i + 1} is not valid base64."
        if not raw:
            return f"ERROR: entry {i + 1} decoded to an empty file."
        parts.append(("files", (f"{i:04d}.pdf", raw, "application/pdf")))

    resp = await _client.post(
        "/v1/pdf/merge", files=parts, headers=_auth_header()
    )
    if resp.status_code != 200:
        return f"ERROR {resp.status_code}: {resp.text[:300]}"
    return _encode(resp.content, "application/pdf")


def _is_gated(body: bytes) -> bool:
    """True when the payload invokes a method that has to be paid for."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        # Malformed JSON is the SDK's to reject, with its own parse error.
        return False
    # A JSON-RPC batch arrives as a list; one gated member gates the request.
    messages = payload if isinstance(payload, list) else [payload]
    return any(
        isinstance(m, dict) and m.get("method") in GATED_METHODS for m in messages
    )


async def _forbidden(send, reason: str) -> None:
    body = json.dumps({"error": reason}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class CallerAuthMiddleware:
    """Decide who a tool call is billed to, and reject the ones that aren't.

    Three ways in, in precedence order:

    1. A marketplace forwarded it with a valid proxy secret — bill the server's
       own key, because the marketplace already took the money.
    2. The caller brought their own DocForge key — bill that key, so quotas and
       usage work exactly as they do on the REST API. This is what makes a
       public registry listing viable.
    3. Neither. Allowed only when no proxy secret is configured, i.e. while
       self-hosting, where the server's own key is the right answer.

    Written against the raw ASGI interface rather than Starlette's
    BaseHTTPMiddleware: that wrapper turns the streaming response the MCP
    transport depends on into a buffered one.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        supplied = next((headers[h] for h in SECRET_HEADERS if headers.get(h)), "")
        caller = _caller_key(headers)

        # compare_digest rather than ==, so response timing cannot be used to
        # recover the secret one byte at a time.
        via_marketplace = bool(
            PROXY_SECRET and supplied and secrets.compare_digest(supplied, PROXY_SECRET)
        )

        # Deciding this needs the method name, which is in the body, so buffer
        # the body and hand the app a receive() that replays what we consumed.
        chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)
        body = b"".join(chunks)

        if _is_gated(body):
            if supplied and not via_marketplace:
                # A wrong secret is refused even when other credentials are
                # present — same call the REST gateway makes.
                await _forbidden(send, "Invalid marketplace proxy secret.")
                return
            if not via_marketplace and not caller and PROXY_SECRET:
                await _forbidden(
                    send,
                    "Provide a DocForge API key in the Authorization header, "
                    "or call through a marketplace.",
                )
                return

        # Empty means "fall back to the server's own key" in _auth_header. A
        # marketplace call bills the server's key; a direct caller bills theirs.
        # An invalid key is the gateway's to reject, not ours to pre-judge.
        token = _request_key.set("" if via_marketplace else caller)

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # Once the body is replayed, defer to the real receive() and block
            # there. Returning http.disconnect here instead would tell the app
            # the client hung up, and it would tear down the streaming response
            # mid-flight — every SSE reply truncates.
            return await receive()

        try:
            await self.app(scope, replay, send)
        finally:
            _request_key.reset(token)


def _transport_security() -> TransportSecuritySettings:
    """Host allow-list for the SDK's DNS-rebinding guard.

    The guard rejects any Host header it doesn't recognise. Behind Caddy that
    header is the public name, so it must be listed or every request 400s.

    On a NAS or home server there is no public name — people reach the service
    at `192.168.1.20:8100`, which the default list would reject. Hence the extra
    hosts variable, and the escape hatch: both exist so a LAN install works
    without editing code, which is the difference between being installable from
    an app store and generating a stream of "it returns 400" reports.
    """
    if os.environ.get("DOCFORGE_MCP_DNS_REBINDING_PROTECTION", "").strip().lower() in (
        "off",
        "false",
        "0",
    ):
        # Correct only on a trusted network. Off means any Host header is
        # accepted, which is what makes DNS rebinding possible in the first place.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts = [PUBLIC_HOST, f"{PUBLIC_HOST}:*", "localhost:*", "127.0.0.1:*"]
    origins = [f"https://{PUBLIC_HOST}", f"http://{PUBLIC_HOST}:*"]

    # `:*` is a port wildcard the SDK understands, so "192.168.1.20:*" covers
    # whichever port the app store happened to map.
    for extra in os.environ.get("DOCFORGE_MCP_ALLOWED_HOSTS", "").split(","):
        extra = extra.strip()
        if extra:
            hosts.append(extra)
            origins.extend([f"http://{extra}", f"https://{extra}"])

    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


if __name__ == "__main__":
    app = mcp.streamable_http_app(
        transport_security=_transport_security(),
        # No session affinity to keep, which matters if this is ever run as
        # more than one replica behind the proxy.
        stateless_http=True,
        host="0.0.0.0",
    )
    uvicorn.run(
        CallerAuthMiddleware(app),
        host="0.0.0.0",
        port=8100,
        log_level="info",
    )
