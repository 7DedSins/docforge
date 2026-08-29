# Architecture

```
                    Internet
                       │
        ┌──────────────▼──────────────┐
        │  Caddy                      │  automatic TLS (Let's Encrypt)
        │  ports 80 / 443             │  hostname from $DOCFORGE_DOMAIN
        └───┬──────────────────┬──────┘
            │ /mcp*            │ /*
    ┌───────▼──────┐   ┌───────▼────────┐
    │  MCP server  │──▶│    gateway     │  keys, quotas, metering,
    │  streamable  │   │    FastAPI     │  concurrency semaphores
    └──────────────┘   └───────┬────────┘
                               │         ┌──────────────┐
                               ├────────▶│   SQLite     │  hashed keys + usage
                               │         └──────────────┘
                       ┌───────▼────────┐
                       │  Gotenberg 8   │  LibreOffice + Chromium
                       └────────────────┘
```

Four containers, one network, three volumes. Gotenberg is never exposed
directly — everything reaches it through the gateway, which is where
authentication and accounting live.

## Design decisions

**Gotenberg does the rendering; this project does the accounting.**
Gotenberg deliberately ships no auth, no quotas and no usage tracking — it
assumes a gateway in front. Building that gateway is the entire point here.

**One uvicorn worker, deliberately.** The concurrency limits are per-process
`asyncio.Semaphore` objects. Running multiple workers would multiply the
effective limit and defeat the protection they exist to provide. Scale by
raising the semaphore, not by adding workers.

**Concurrency limits queue rather than reject.** Requests past the *global*
limit wait for a slot instead of spawning another LibreOffice or Chromium
process. Overload therefore appears as latency, never as errors, and never as an
OOM kill.

**Two limiter layers, answering different questions.** The global semaphores ask
"can the host survive this?" The per-key rate limit and concurrency cap
(`limits.py`) ask "is this caller taking more than their share?" Measured
without the second layer: 150 concurrent requests from one key completed
successfully but pushed p50 latency to 15 s for every other caller. Safety was
never at risk; fairness was.

Per-caller state is in-memory and per-process on purpose — it protects *this*
box's capacity right now, and a restart already frees the capacity it guards.
Monthly quota, which must survive restarts, lives in SQLite instead.

**Rate limiting uses a sliding window, not a fixed one.** A fixed window lets a
caller send a full allowance at 59.9 s and another at 60.1 s — double the
intended burst in 200 ms, which is precisely what the limit exists to prevent.

**SQLite, not a database server.** One row per API call. On a small box, a file
you can copy is a better backup story than a second daemon. WAL mode lets quota
reads proceed while a usage row is written; a single lock serialises writers to
avoid `database is locked` under burst.

**Keys stored as SHA-256 hashes.** The raw key is returned once at issue time
and never persisted, so a leaked database file yields no working credentials.

**Failed calls are not metered.** Only `status < 400` counts against a quota.
Billing a customer for your own 500 is how you earn a chargeback.

**Jinja2 autoescaping is on.** Callers put their own data into these templates;
an apostrophe in a name should not be able to break the layout or inject markup.

**Memory limits on every container.** The stack is designed to be a good
neighbour on a shared host. Total ceiling is roughly 2.1 GB.

**No tool fetches arbitrary URLs.** Every endpoint and MCP tool operates on
content the caller supplies. A tool that fetched a caller-supplied URL would
turn the service into an open proxy the moment it was listed publicly.

## Binding, and why it's configurable

`DOCFORGE_BIND_IP` defaults to `0.0.0.0`, which is right for a normal server.

Pin it to one address when something else already holds `:80`/`:443` on another
interface. The concrete case: Tailscale Serve binds the tailnet address, and a
wildcard bind by Caddy collides with it — taking down whatever private services
were being served there. Binding Caddy to the public address only lets both
coexist on one host.

## Gotchas already solved

Recorded so they aren't rediscovered the hard way.

- **httpx 0.28** treats a non-Mapping `data=` as raw request content and wraps
  it in a *sync* byte stream, which `AsyncClient` then refuses to send
  (`Attempted to send an sync request with an AsyncClient instance`). Pass
  dictionaries, not lists of tuples.
- **MCP SDK 1.x → 2.x** renamed `FastMCP` to `MCPServer`, moved `host`/`port`
  into `run_streamable_http_async()`, and enforces Host/Origin allow-listing
  against DNS rebinding. The public hostname must appear in
  `TransportSecuritySettings.allowed_hosts` or every proxied request fails with
  a 400. (1.12 also crashes building schemas from union/generic annotations.)
- **Caddy does not reload on file change.** After editing the Caddyfile:
  `docker exec docforge-caddy caddy reload --config /etc/caddy/Caddyfile`.
- **sslip.io** provides working Let's Encrypt TLS with no domain purchase — it's
  on the Public Suffix List, so each subdomain gets its own rate limit.
- **Schema order is load-bearing: tables, then `ALTER TABLE`, then indexes.**
  Declaring an index on a column in the same script that declares the table
  works on a fresh database and fails on an existing one — `CREATE TABLE IF NOT
  EXISTS` is a no-op there, so a column added in a later release does not exist
  yet and the index dies with "no such column". That crashes startup on exactly
  the deployments with data worth keeping. `test_init_upgrades_a_pre_existing_database`
  pins this.
- **`X-Forwarded-For`: trust the last entry, not the first.** Caddy *appends*
  the peer it observed, so earlier entries are whatever the caller sent. Reading
  the first entry — the usual mistake — lets anyone forge a fresh IP per request
  and walk through every per-IP limit.

## Request lifecycle

1. Caddy terminates TLS, enforces the 32 MB body cap, routes `/mcp*` to the MCP
   server and everything else to the gateway.
2. The gateway resolves the API key, hashes it, looks it up, and sums the
   current month's usage.
3. Over quota → `429`. Otherwise acquire the relevant semaphore, waiting if all
   slots are busy.
4. Forward to Gotenberg as multipart; stream the result back.
5. Record one usage row with endpoint, units, status and duration.

Step 5 runs regardless of outcome, but only successful calls consume quota.
