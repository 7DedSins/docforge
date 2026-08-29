# Security

## Reporting

Open a [security advisory](https://github.com/7DedSins/docforge/security/advisories/new)
rather than a public issue.

## Threat model

DocForge accepts **untrusted input from authenticated callers**: documents to
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

### Caller identity and abuse

Every usage row records the caller's IP and, for marketplace traffic, the
upstream user id. `docforgectl.py abuse` surfaces free keys sharing an IP, one
key used from many IPs, and free keys burned in a burst.

These are review prompts, not verdicts — a shared office NAT is
indistinguishable from one person holding many keys, so nothing is blocked
automatically.

**Free-tier farming cannot be fully prevented**, and any design claiming
otherwise is overselling. Someone with disposable email addresses will always
obtain more free calls than intended. The defence is economic: keep the free
tier small enough that farming costs more effort than it returns, and delegate
identity to a marketplace where one exists — RapidAPI has already verified the
user and holds their payment method.

### Marketplace enforcement

With `DOCFORGE_RAPIDAPI_PROXY_SECRET` set, every request must carry a matching
`X-RapidAPI-Proxy-Secret`. Without it, anyone who reads the base URL off a
marketplace listing calls the API directly and never pays. The comparison is
constant-time; a plain `!=` leaks the secret a byte at a time to anyone willing
to measure response latency.

### Client address resolution

Per-IP limits are only as trustworthy as the address they key on. Caddy
*appends* the peer it observed to `X-Forwarded-For`, so the **last** entry is
the only trustworthy one. Reading the first entry — the common implementation —
lets a caller forge a fresh IP per request and bypass every per-IP control.

### Availability

- Request bodies capped at 32 MB, render timeout 180 s.
- Image dimensions bounded to 16–4000 px, so one caller can't occupy Chromium
  for minutes with a 20000×20000 render.
- Concurrency semaphores (6 documents, 4 images) mean excess load **queues**
  rather than spawning unbounded LibreOffice/Chromium processes.
- Per-key sliding-window rate limit and in-flight cap, so one caller cannot
  occupy every slot. Measured before these existed: 150 concurrent requests
  from a single key raised p50 latency to 15 s for everyone else.
- The anonymous demo is capped per IP per day, counted **before** rendering —
  counting on success would let a caller burn CPU for free with files they know
  will fail.
- Every container has a hard memory limit.

### Known gaps

Stated plainly rather than discovered by a reader:

- **Limiter state is per-process and in-memory.** Correct for a single-worker
  deployment, which is what this ships as. Running multiple gateway replicas
  would multiply every per-key allowance — move the limiter to Redis first.
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
