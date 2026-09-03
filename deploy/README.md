# Running DocForge on a NAS or home server

The compose file here is for **CasaOS, Umbrel, Unraid, Coolify, Portainer and
Dockge** — anywhere the platform already handles reverse proxying and TLS.

If you want DocForge on a public server with its own certificate, use the
compose file at the repo root instead: it includes Caddy and gets a Let's
Encrypt certificate automatically.

```bash
curl -O https://raw.githubusercontent.com/7DedSins/docforge/main/deploy/docker-compose.yml
docker compose up -d
```

Open `http://<your-server>:8080` and you get a working drag-and-drop converter.

## Issue yourself a key

The browser demo is capped per IP per day. For the API, mint a key:

```bash
docker exec docforge-gateway python /app/docforgectl.py issue --label me --plan unlimited
```

It is printed once and stored only as a SHA-256 hash, so it cannot be recovered
later — save it now, or issue another.

```bash
curl -X POST http://<your-server>:8080/v1/convert/office \
  -H "Authorization: Bearer $DOCFORGE_KEY" \
  -F "file=@report.docx" -o report.pdf
```

## Settings worth knowing

| Variable | Default | Why you'd change it |
|---|---|---|
| `DOCFORGE_PORT` | `8080` | Something else already has 8080 |
| `DOCFORGE_MCP_PORT` | `8100` | Same |
| `DOCFORGE_DOMAIN` | `localhost` | Address you actually reach it at; shown in the landing page's examples |
| `DOCFORGE_MCP_ALLOWED_HOSTS` | *(empty)* | **Required for MCP over LAN** — see below |
| `DOCFORGE_MAX_CONCURRENT_DOCS` | `6` | Raise on dedicated hardware, lower on a Pi |
| `DOCFORGE_FREE_TIER_MONTHLY` | `250` | Quota given to `free` plan keys |

## If MCP returns 400

The MCP SDK refuses `Host` headers it doesn't recognise — that is what stops a
malicious page in your browser from driving a service on your LAN. It cannot
guess that you reach the server at `192.168.1.20:8100`, so tell it:

```bash
DOCFORGE_MCP_ALLOWED_HOSTS=192.168.1.20:*,nas.local:*
```

`:*` wildcards the port, so remapping later doesn't break it. If you'd rather
not maintain the list on a network you trust:

```bash
DOCFORGE_MCP_DNS_REBINDING_PROTECTION=off
```

That accepts any `Host` header. Fine on a home LAN, wrong on anything exposed
to the internet.

## Hardware

Gotenberg carries LibreOffice and Chromium, so the image is large (~2.4 GB) and
wants around 2 GB of RAM under load. Measured on 4 vCPU / 8 GB: HTML→PDF 1.3 s,
DOCX→PDF 2.0 s, 20 concurrent conversions in 3.6 s.

It runs on a Raspberry Pi 4 or 5 — `arm64` images are published — but expect
conversions to take appreciably longer.

## Backups

One volume matters: `docforge_data`, which holds every API key and every usage
record. The containers are disposable; that isn't.
