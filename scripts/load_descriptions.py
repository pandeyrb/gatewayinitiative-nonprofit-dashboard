"""
Load approved summaries from qa/descriptions_review.csv.

Run from the repo root:
    python scripts/load_descriptions.py --dry-run   # show what would change, touch nothing
    python scripts/load_descriptions.py             # save and update Supabase

Only rows with Approved = yes (or y / x / true) are loaded, with whatever edits
the reviewer made to Summary_EN and Summary_ES. Two things happen:
  1. data/org_descriptions.csv is updated. It's the copy in the repo, and
     archive/scripts/load_supabase.py reads it, so a full reload would keep them.
  2. organizations.description and description_es are updated in Supabase.
     The site shows them within ten minutes.

Run db/004_org_descriptions.sql in Supabase once first (adds description_es).
Needs SUPABASE_URL and SUPABASE_SECRET_KEY in .env, like the other scripts that write to Supabase.
"""

import argparse
import csv
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW = os.path.join(ROOT, "qa", "descriptions_review.csv")
SAVED = os.path.join(ROOT, "data", "org_descriptions.csv")
FIELDS = ["Name", "Description", "DescriptionEs", "Source", "ApprovedOn"]
YES = {"yes", "y", "x", "true", "1"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    args = ap.parse_args()

    with open(REVIEW, newline="", encoding="utf-8") as fh:
        approved = [r for r in csv.DictReader(fh)
                    if r.get("Approved", "").strip().lower() in YES and r["Summary_EN"].strip()]
    if not approved:
        sys.exit("No approved rows yet. Mark Approved = yes in qa/descriptions_review.csv.")

    saved: dict[str, dict] = {}
    if os.path.exists(SAVED):
        with open(SAVED, newline="", encoding="utf-8") as fh:
            saved = {r["Name"]: r for r in csv.DictReader(fh)}

    changes = []
    for r in approved:
        new = {
            "Name": r["Name"].strip(),
            "Description": r["Summary_EN"].strip(),
            "DescriptionEs": r["Summary_ES"].strip(),
            "Source": ("service list" if "from_service_list" in r["Flag"]
                       else "web search" if "from_web_search" in r["Flag"] else "website"),
            "ApprovedOn": date.today().isoformat(),
        }
        old = saved.get(new["Name"], {})
        if (old.get("Description"), old.get("DescriptionEs")) != (new["Description"], new["DescriptionEs"]):
            changes.append(new)

    print(f"{len(approved)} approved, {len(changes)} new or changed:")
    for c in changes:
        print(f"  {c['Name']}: {c['Description'][:90]}")
    if not changes or args.dry_run:
        print("\nDry run: nothing was written." if args.dry_run else "\nNothing to do.")
        return

    from dotenv import load_dotenv
    from supabase import create_client

    load_dotenv(os.path.join(ROOT, ".env"))
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SECRET_KEY in .env first.")
    client = create_client(url, key)

    missing = []
    for c in changes:
        try:
            res = (client.table("organizations")
                   .update({"description": c["Description"], "description_es": c["DescriptionEs"] or None})
                   .eq("name", c["Name"]).execute())
        except Exception as exc:
            if "description_es" in str(exc):
                sys.exit("Supabase has no description_es column yet: run db/004_org_descriptions.sql first.")
            raise
        if not res.data:
            missing.append(c["Name"])
        saved[c["Name"]] = c

    with open(SAVED, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(saved.values(), key=lambda r: r["Name"].lower()))

    print(f"\nUpdated {len(changes) - len(missing)} organizations; saved {os.path.relpath(SAVED, ROOT)}.")
    if missing:
        print("No organization with this exact name in Supabase (saved to the CSV anyway): "
              + ", ".join(missing))


if __name__ == "__main__":
    main()
