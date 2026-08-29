# Security

## Reporting

Open a [security advisory](https://github.com/7DedSins/forge/security/advisories/new)
rather than a public issue.

## Threat model

Forge accepts **untrusted input from authenticated callers**: documents to
convert, HTML to render, and Jinja2 template *source* for image rendering. A
valid API key is not a trust boundary — free-tier keys are handed out on signup,
so every control below has to hold against someone who has one.

## Controls

### Template sandboxing

`/v1/image/render` accepts template source, not just data. That is a
server-side template injection surface: with a plain Jinja2 `Environment`,
`{{ "".__class__.__mro__[1].__subclasses__() }}` reaches every loaded class and
`{{ cycler.__init__.__globals__ }}` reaches module globals — a direct path from
"render a banner" to code execution.

Templates render in a `SandboxedEnvironment`, which blocks attribute access to
internals and unsafe calls. Blocked templates return `422` with a deliberately
vague message; naming the blocked attribute would help map the sandbox edge.

Autoescaping is on, so caller data cannot inject markup into output.

Template source is capped at **256 KB**.

### SSRF containment

Both rendering engines fetch what a caller's content references — an
`<iframe src>` in HTML, a linked image in a DOCX. Unrestricted, that makes the
service a probe for anything reachable from the host.

Gotenberg deny-lists block, for **both** Chromium and LibreOffice:

- `file://` outside the working directory (Gotenberg's own default, preserved)
- loopback — `localhost`, `127.0.0.0/8`, `0.0.0.0`, `[::1]`
- RFC1918 — `10/8`, `172.16/12`, `192.168/16`
- link-local and cloud metadata — `169.254.0.0/16`, which covers `169.254.169.254`
- `100.64.0.0/10` — the CGNAT range Tailscale uses
- internal Compose service names

Outbound requests to the public internet are still allowed, since legitimate
templates reference external images and fonts.

### Credentials

API keys are stored as SHA-256 hashes. The raw value is returned once at issue
time and never persisted, so a leaked database file yields no usable
credentials. Keys are matched by hash; revocation is a flag, not a delete, so
usage history survives.

### Availability

- Request bodies capped at 32 MB, render timeout 180 s.
- Image dimensions bounded to 16–4000 px, so one caller can't occupy Chromium
  for minutes with a 20000×20000 render.
- Concurrency semaphores (6 documents, 4 images) mean excess load **queues**
  rather than spawning unbounded LibreOffice/Chromium processes.
- Every container has a hard memory limit.

### Known gaps

Stated plainly rather than discovered by a reader:

- **No per-key rate limit.** Quotas are monthly, not per-second. One key can
  consume all concurrency slots. Put a reverse-proxy rate limit in front before
  selling a high-volume tier.
- **No request-body scanning.** A malformed document that crashes LibreOffice
  produces a 502, not a compromise — but it is a denial-of-service avenue.
- **Sandbox escapes are possible in principle.** Jinja2's sandbox has had
  bypasses historically. Keep Jinja2 current; don't treat it as the only layer
  if you process genuinely hostile input at scale.
- **Single-tenant isolation only.** All callers share one Gotenberg instance.
  Fine for a shared service; not a substitute for per-tenant isolation if you
  need it.

## Deployment notes

- Never commit `.env`. Use `.env.example` as the template.
- Terminate TLS at Caddy; don't expose the gateway or Gotenberg directly.
- Back up the `gateway_data` volume — it holds every key and usage record.
