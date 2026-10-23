"""
What people search for on the dashboard, from the search_log table.

Run from the repo root:
    python scripts/search_report.py              # last 30 days
    python scripts/search_report.py --days 7
    python scripts/search_report.py --days 0     # everything

The app logs each search once per visit: its text, language and how many
organizations it found (app.py, _log_search). Searches that found nothing are
the most useful part: each one is a service, phrasing or organization the
directory is missing. Needs SUPABASE_URL and SUPABASE_SECRET_KEY in .env (the
public key can add to the log but not read it).
"""

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="how far back to look (0 = all)")
    ap.add_argument("--top", type=int, default=25, help="rows per list")
    args = ap.parse_args()

    from dotenv import load_dotenv
    from supabase import create_client

    load_dotenv(os.path.join(ROOT, ".env"))
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SECRET_KEY in .env first.")
    client = create_client(url, key)

    rows, start = [], 0
    while True:
        q = client.table("search_log").select("query,lang,result_count,created_at").order("id")
        if args.days:
            since = datetime.now(timezone.utc) - timedelta(days=args.days)
            q = q.gte("created_at", since.isoformat())
        batch = q.range(start, start + 999).execute().data
        rows += batch
        if len(batch) < 1000:
            break
        start += 1000

    span = f"the last {args.days} days" if args.days else "all time"
    if not rows:
        print(f"No searches logged in {span}.")
        return

    norm = lambda s: " ".join(s.lower().split())
    all_q = Counter(norm(r["query"]) for r in rows)
    zero = Counter(norm(r["query"]) for r in rows if (r["result_count"] or 0) == 0)
    langs = Counter(r["lang"] or "?" for r in rows)

    print(f"{len(rows)} searches in {span} "
          f"({', '.join(f'{n} {l}' for l, n in langs.most_common())}); "
          f"{sum(zero.values())} found nothing.\n")
    print(f"Searches that found NOTHING (most common first):")
    for q, n in zero.most_common(args.top):
        print(f"  {n:4}  {q}")
    print(f"\nMost common searches:")
    for q, n in all_q.most_common(args.top):
        print(f"  {n:4}  {q}")


if __name__ == "__main__":
    main()
