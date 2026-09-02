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

mcp = MCPServer(
    "docforge",
    title="DocForge — documents and images",
    instructions=(
        "Convert documents to PDF and render HTML templates to images. "
        "Use html_to_pdf for reports and invoices, render_image for social "
        "cards and banners, convert_office_document for DOCX/XLSX/PPTX."
    ),
)

_client = httpx.AsyncClient(
    base_url=GATEWAY,
    timeout=httpx.Timeout(180.0, connect=10.0),
    headers={"Authorization": f"Bearer {API_KEY}"},
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

    resp = await _client.post("/v1/pdf/merge", files=parts)
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


async def _forbidden(send) -> None:
    body = json.dumps(
        {"error": "Missing or invalid marketplace proxy secret."}
    ).encode()
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


class MarketplaceSecretMiddleware:
    """Reject billable tool calls that did not arrive through a marketplace.

    Written against the raw ASGI interface rather than Starlette's
    BaseHTTPMiddleware: that wrapper turns the streaming response the MCP
    transport depends on into a buffered one.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not PROXY_SECRET:
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        supplied = next((headers[h] for h in SECRET_HEADERS if headers.get(h)), "")

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

        # compare_digest rather than ==, so response timing cannot be used to
        # recover the secret one byte at a time.
        if _is_gated(body) and not secrets.compare_digest(supplied, PROXY_SECRET):
            await _forbidden(send)
            return

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

        await self.app(scope, replay, send)


if __name__ == "__main__":
    app = mcp.streamable_http_app(
        # The SDK rejects unknown Host/Origin headers to block DNS rebinding.
        # Behind Caddy the header is the public name, so it has to be allowed
        # explicitly or every request 400s.
        transport_security=TransportSecuritySettings(
            allowed_hosts=[PUBLIC_HOST, f"{PUBLIC_HOST}:443", "localhost:8100"],
            allowed_origins=[f"https://{PUBLIC_HOST}"],
        ),
        # No session affinity to keep, which matters if this is ever run as
        # more than one replica behind the proxy.
        stateless_http=True,
        host="0.0.0.0",
    )
    uvicorn.run(
        MarketplaceSecretMiddleware(app),
        host="0.0.0.0",
        port=8100,
        log_level="info",
    )
