#!/usr/bin/env python3
"""Key management for DocForge. Run inside the gateway container.

    docker exec -it docforge-gateway python /app/docforgectl.py issue --label acme --plan starter
    docker exec -it docforge-gateway python /app/docforgectl.py list
    docker exec -it docforge-gateway python /app/docforgectl.py stats
    docker exec -it docforge-gateway python /app/docforgectl.py abuse
    docker exec -it docforge-gateway python /app/docforgectl.py revoke <key-hash-prefix>

The raw key is printed exactly once at issue time. It is stored only as a
SHA-256 hash, so there is deliberately no way to recover it later — reissue
instead.
"""

import argparse
import sys

sys.path.insert(0, "/app")

from app import db  # noqa: E402

# Quota per plan, matching the published pricing. Free is deliberately small:
# large enough to build an integration against, too small to run a production
# workload on forever. A free tier that quietly serves real traffic is not
# marketing, it is an unpaid customer.
PLANS = {
    "free":      250,
    "starter":   5_000,
    "pro":       50_000,
    "scale":     500_000,
    "unlimited": 0,  # 0 means no cap
}


def main() -> int:
    p = argparse.ArgumentParser(description="DocForge key management")
    sub = p.add_subparsers(dest="cmd", required=True)

    issue = sub.add_parser("issue", help="mint a new API key")
    issue.add_argument("--label", default="", help="who this key is for")
    issue.add_argument("--plan", default="free", choices=sorted(PLANS))
    issue.add_argument("--rate-per-min", type=int, default=0,
                       help="0 uses the server default")
    issue.add_argument("--max-concurrent", type=int, default=0,
                       help="0 uses the server default")

    sub.add_parser("list", help="list keys")
    sub.add_parser("stats", help="usage summary for the current month")

    ab = sub.add_parser("abuse", help="signals that free keys are being farmed")
    ab.add_argument("--days", type=int, default=30)

    rev = sub.add_parser("revoke", help="deactivate a key")
    rev.add_argument("prefix", help="first characters of the key hash")

    args = p.parse_args()
    db.init()

    if args.cmd == "issue":
        raw = db.create_key(args.label, args.plan, PLANS[args.plan],
                            args.rate_per_min, args.max_concurrent)
        print(f"\n  plan   {args.plan} ({PLANS[args.plan] or 'unlimited'} calls/month)")
        print(f"  label  {args.label or '(none)'}")
        print(f"\n  KEY    {raw}\n")
        print("  Shown once. Not recoverable — store it now.\n")

    elif args.cmd == "list":
        with db.cursor() as conn:
            rows = conn.execute(
                "SELECT key_hash, label, plan, monthly_quota, rate_per_min, "
                "max_concurrent, active FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
        if not rows:
            print("No keys yet.")
            return 0
        print(f"{'HASH':12} {'PLAN':10} {'QUOTA':>8} {'RATE':>5} {'CONC':>5} {'ON':>3}  LABEL")
        for r in rows:
            print(f"{r['key_hash'][:12]} {r['plan']:10} {r['monthly_quota']:>8} "
                  f"{r['rate_per_min'] or '-':>5} {r['max_concurrent'] or '-':>5} "
                  f"{'y' if r['active'] else 'n':>3}  {r['label']}")

    elif args.cmd == "revoke":
        with db.cursor(write=True) as conn:
            n = conn.execute(
                "UPDATE api_keys SET active = 0 WHERE key_hash LIKE ?",
                (args.prefix + "%",),
            ).rowcount
        print(f"Revoked {n} key(s).")

    elif args.cmd == "stats":
        from datetime import UTC, datetime
        month = datetime.now(UTC).strftime("%Y-%m")
        with db.cursor() as conn:
            rows = conn.execute(
                "SELECT k.label, k.plan, COUNT(*) AS calls, "
                "       SUM(CASE WHEN u.status >= 400 THEN 1 ELSE 0 END) AS errors, "
                "       COUNT(DISTINCT u.ip) AS ips, "
                "       CAST(AVG(u.ms) AS INTEGER) AS avg_ms "
                "FROM usage u JOIN api_keys k ON k.key_hash = u.key_hash "
                "WHERE u.ts LIKE ? GROUP BY u.key_hash ORDER BY calls DESC",
                (f"{month}%",),
            ).fetchall()
            demo = conn.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT ip) AS ips FROM demo_usage "
                "WHERE ts LIKE ?", (f"{month}%",),
            ).fetchone()
        print(f"\nUsage for {month}\n")
        if rows:
            print(f"  {'LABEL':20} {'PLAN':10} {'CALLS':>7} {'ERR':>5} {'IPs':>4} {'AVG ms':>7}")
            for r in rows:
                print(f"  {(r['label'] or '-')[:20]:20} {r['plan']:10} "
                      f"{r['calls']:>7} {r['errors']:>5} {r['ips']:>4} {r['avg_ms']:>7}")
        else:
            print("  No API calls yet.")
        print(f"\n  anonymous demo: {demo['n']} conversions from {demo['ips']} IPs\n")

    elif args.cmd == "abuse":
        rep = db.abuse_report(args.days)
        print(f"\nAbuse signals, last {args.days} days")
        print("These are a shortlist to eyeball, not proof. A shared office NAT")
        print("looks identical to one person holding many free keys.\n")

        print("  Free keys sharing an IP")
        if rep["shared_ip"]:
            for r in rep["shared_ip"]:
                print(f"    {r['ip']:>16}  {r['keys']} keys, {r['calls']} calls")
        else:
            print("    none")

        print("\n  One key used from many IPs (shared or resold key)")
        if rep["many_ips"]:
            for r in rep["many_ips"]:
                print(f"    {r['key_hash'][:12]}  {r['ips']} IPs, {r['calls']} calls")
        else:
            print("    none")

        print("\n  Free keys burned in a burst (script, not evaluation)")
        if rep["burst"]:
            for r in rep["burst"]:
                print(f"    {r['key_hash'][:12]}  {(r['label'] or '-')[:18]:18} "
                      f"{r['calls']} calls in <10 min")
        else:
            print("    none")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
