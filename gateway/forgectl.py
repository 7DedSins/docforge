#!/usr/bin/env python3
"""Key management for Forge. Run on the VPS, inside the gateway container.

    docker exec -it forge-gateway python /app/forgectl.py issue --label "acme" --plan pro
    docker exec -it forge-gateway python /app/forgectl.py list
    docker exec -it forge-gateway python /app/forgectl.py revoke <key-hash-prefix>
    docker exec -it forge-gateway python /app/forgectl.py stats

The raw key is printed exactly once at issue time. It is stored only as a
SHA-256 hash, so there is deliberately no way to recover it later — reissue
instead.
"""

import argparse
import sys

sys.path.insert(0, "/app")

from datetime import UTC

from app import db  # noqa: E402

# Quota per plan. Deliberately generous at the free tier: the research is clear
# that crippled free tiers (200 files/month) are the loudest complaint about
# every incumbent in this category, and the free tier is the marketing.
PLANS = {
    "free":  250,
    "starter": 5_000,
    "pro":    50_000,
    "scale": 500_000,
    "unlimited": 0,  # 0 means no cap
}


def main() -> int:
    p = argparse.ArgumentParser(description="Forge key management")
    sub = p.add_subparsers(dest="cmd", required=True)

    issue = sub.add_parser("issue", help="mint a new API key")
    issue.add_argument("--label", default="", help="who this key is for")
    issue.add_argument("--plan", default="free", choices=sorted(PLANS))

    sub.add_parser("list", help="list keys")
    sub.add_parser("stats", help="usage summary for the current month")

    rev = sub.add_parser("revoke", help="deactivate a key")
    rev.add_argument("prefix", help="first characters of the key hash")

    args = p.parse_args()
    db.init()

    if args.cmd == "issue":
        raw = db.create_key(args.label, args.plan, PLANS[args.plan])
        quota = PLANS[args.plan]
        print(f"\n  plan   {args.plan} ({quota or 'unlimited'} calls/month)")
        print(f"  label  {args.label or '(none)'}")
        print(f"\n  KEY    {raw}\n")
        print("  Shown once. Not recoverable — store it now.\n")

    elif args.cmd == "list":
        with db.cursor() as conn:
            rows = conn.execute(
                "SELECT key_hash, label, plan, monthly_quota, active, created_at "
                "FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
        if not rows:
            print("No keys yet.")
            return 0
        print(f"{'HASH':12} {'PLAN':10} {'QUOTA':>8} {'ON':>3}  LABEL")
        for r in rows:
            print(f"{r['key_hash'][:12]} {r['plan']:10} {r['monthly_quota']:>8} "
                  f"{'y' if r['active'] else 'n':>3}  {r['label']}")

    elif args.cmd == "revoke":
        with db.cursor(write=True) as conn:
            n = conn.execute(
                "UPDATE api_keys SET active = 0 WHERE key_hash LIKE ?",
                (args.prefix + "%",),
            ).rowcount
        print(f"Revoked {n} key(s).")

    elif args.cmd == "stats":
        from datetime import datetime
        month = datetime.now(UTC).strftime("%Y-%m")
        with db.cursor() as conn:
            rows = conn.execute(
                "SELECT k.label, k.plan, COUNT(*) AS calls, "
                "       SUM(CASE WHEN u.status >= 400 THEN 1 ELSE 0 END) AS errors, "
                "       CAST(AVG(u.ms) AS INTEGER) AS avg_ms "
                "FROM usage u JOIN api_keys k ON k.key_hash = u.key_hash "
                "WHERE u.ts LIKE ? GROUP BY u.key_hash ORDER BY calls DESC",
                (f"{month}%",),
            ).fetchall()
        print(f"\nUsage for {month}\n")
        if not rows:
            print("  No calls yet.\n")
            return 0
        print(f"  {'LABEL':20} {'PLAN':10} {'CALLS':>7} {'ERR':>5} {'AVG ms':>7}")
        for r in rows:
            print(f"  {(r['label'] or '-')[:20]:20} {r['plan']:10} "
                  f"{r['calls']:>7} {r['errors']:>5} {r['avg_ms']:>7}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
