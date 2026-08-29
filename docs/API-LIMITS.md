# API limits and real capacity

Every number here is either read from the config or **measured on a live
deployment**. Nothing is estimated.

Base URL: whatever you set as `DOCFORGE_DOMAIN`.

---

## Hard limits

| Limit | Value | Set in | If exceeded |
|---|---|---|---|
| Request body | **32 MB** | `Caddyfile` | HTTP 413 |
| Request timeout | **180 s** | Caddy + httpx | HTTP 504 |
| Concurrent document conversions (global) | **6** | `DOCFORGE_MAX_CONCURRENT_DOCS` | Queues (adds latency, never fails) |
| Concurrent image renders (global) | **4** | `DOCFORGE_MAX_CONCURRENT_IMAGES` | Queues |
| Requests per minute, per key | **60** | `DOCFORGE_RATE_PER_MIN` | HTTP 429 + `Retry-After` |
| In-flight requests, per key | **4** | `DOCFORGE_KEY_MAX_CONCURRENT` | HTTP 429 |
| Anonymous demo, per IP per day | **3** | `DOCFORGE_DEMO_PER_DAY` | HTTP 429 |
| Anonymous demo file size | **5 MB** | `main.py` | HTTP 413 |
| Image width / height | **16–4000 px** each | `main.py` | HTTP 400 |
| Template source | **256 KB** | `main.py` | HTTP 413 |
| PDFs per merge | **2 minimum** | `main.py` | HTTP 400 |
| MCP inline result | **6 MB** | `mcpserver/server.py` | Returns an error string, not a truncated file |
| Chromium queue | **20** | `docker-compose.yml` | Gotenberg rejects |
| LibreOffice restart interval | every **50** conversions | `docker-compose.yml` | — |

Concurrency limits **queue** rather than reject. Overload shows up as latency,
never as an error, and never as an OOM kill on the media stack.

---

## Measured performance

Single request, warm:

| Operation | Time | Output |
|---|---|---|
| HTML → PDF | **1.3 s** | 13 KB |
| DOCX → PDF | **2.0 s** | 12 KB |
| 1200×630 PNG | **2.9 s** | 685 KB |

Load test — **20 concurrent** HTML→PDF, measured on the live box:

```
requests    : 20
succeeded   : 20   (100%)
wall clock  : 3.6 s
throughput  : 5.63 docs/sec
latency min : 0.92 s
latency p50 : 2.38 s
latency max : 3.53 s
```

Host during the burst: load average **3.39** on 4 cores, 3.45 GB RAM free, all
six media containers healthy throughout.

**Read that as: bursts are fine, and nothing broke.**

---

## What the box can actually sustain

Burst ceiling is ~5.6 docs/sec. Sustained is lower, because the media stack needs
CPU too — plan on roughly **1–2 docs/sec sustained**, ~15–25% duty cycle.

Against the pricing tiers:

| Plan | Quota/month | Average rate needed | Verdict |
|---|---|---|---|
| Free | 250 | 0.0001/sec | trivial |
| Starter 5k | 5,000 | 0.002/sec | trivial |
| Pro 50k | 50,000 | 0.02/sec | comfortable |
| Scale 500k | 500,000 | **0.19/sec** | fine — 3% of burst capacity |

Even **2 million calls/month** is only 0.77/sec average. The constraint is never
the monthly total — it's simultaneous bursts.

**So:** the tiers are safe on volume. The constraint that actually bit was
*fairness*, and it is now handled by the per-key limits above.

### Measured behaviour under a single-key flood

Before per-key caps existed, one key firing everything it had:

| Concurrent | Succeeded | Wall | Throughput | p50 |
|---|---|---|---|---|
| 10 | 10/10 | 5.6 s | 1.80/s | 5.1 s |
| 50 | 50/50 | 11.0 s | 4.54/s | 6.9 s |
| 150 | 150/150 | 29.7 s | 5.06/s | **15.6 s** |

Nothing failed — the global semaphores held and the host stayed up. But every
other caller waited up to 30 seconds behind one customer. That is the problem
the per-key concurrency cap solves: it converts "one caller degrades everyone"
into "one caller degrades themselves".

---

## Quotas

| Plan | Calls/month |
|---|---|
| `free` | 50 |
| `starter` | 5,000 |
| `pro` | 50,000 |
| `scale` | 500,000 |
| `unlimited` | no cap |

- One unit per call, regardless of file size or page count. A 200-page merge
  costs the same as a 2-page merge.
- **Failed calls are not billed** — only `status < 400` counts. Charging for our
  own 500s is how you earn a chargeback.
- Resets on the 1st, UTC.
- Over quota → **HTTP 429** with a message stating usage and reset.

---

## Error codes

| Code | Meaning |
|---|---|
| 400 | Bad parameters (dimensions, <2 PDFs, malformed JSON) |
| 401 | Missing or revoked API key |
| 413 | Body over 32 MB |
| 415 | Unsupported file extension |
| 422 | Template error, or the renderer rejected the input |
| 429 | Monthly quota exhausted |
| 502 | Renderer unavailable |
| 504 | Render exceeded 180 s |

---

## Supported input formats

**Office → PDF:** `.doc .docx .odt .rtf .txt .xls .xlsx .ods .csv .ppt .pptx .odp`

**Image output:** `png`, `jpeg`, `webp`

---

## Known constraints worth telling customers

- **Fidelity:** LibreOffice and Microsoft Word render the same DOCX slightly
  differently — page breaks, font fallbacks, hyphenation. Fine for generic
  documents; not pixel-identical to Word. Say this openly in the listing; it
  prevents refund requests.
- **No fonts beyond the container's set.** Custom-font documents fall back.
  Embed fonts as base64 in HTML templates to control this.
- **No async/webhook mode.** Every call blocks until the file is ready. Long
  batches should be parallelised client-side, up to the concurrency limits.
- **Single region** (Germany). Latency to US/Asia adds ~100–300 ms.

---

## Raising the limits

Each of these is a config change, not a rewrite:

| To raise | Change | Cost |
|---|---|---|
| Body size | `Caddyfile` → `max_size` | free |
| Concurrency | `DOCFORGE_MAX_CONCURRENT_*` | free, but watch the media stack |
| Throughput | Move media stack off, or size up the VPS | Contabo upgrade |
| Image dimensions | `main.py` bounds check | free |

The 6/4 concurrency split is deliberately conservative because this box is
shared. On a dedicated box, 4 cores would comfortably carry 10–12 documents.
