"""
Step B4: write the approved rows of review/details_review.csv to Supabase.

Run from the repo root:
    python scripts/upload_details.py --dry-run
    python scripts/upload_details.py

Only rows with "yes" in the approve column are written, with whatever edits
you made in the sheet. "not stated" is stored as empty. new_services are only
added for orgs that have no services yet, so existing tags are never changed.

It updates rows in place; nothing is deleted. An org hidden while waiting for
review (is_active false) is switched back on when its row is approved. The site picks up the changes
within 10 minutes (its cache), or immediately after a restart.
"""

import argparse
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import REVIEW_DIR, fetch_all, is_yes, supabase  # noqa: E402

REVIEW = os.path.join(REVIEW_DIR, "details_review.csv")
FIELDS = ["description", "who_its_for", "cost", "languages", "how_to_get_help"]


def clean(v):
    v = str(v or "").strip()
    return None if not v or v.lower() == "not stated" else v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(REVIEW):
        sys.exit("Run scripts/extract_details.py first.")
    sheet = pd.read_csv(REVIEW, dtype=str).fillna("")
    approved = sheet[sheet["approve"].map(lambda v: is_yes(v) or str(v).strip().lower() == "auto")]
    if approved.empty:
        sys.exit('No rows with "yes" in the approve column yet.')

    sb = supabase()
    org_id = {o["name"]: o["id"] for o in fetch_all(sb, "organizations")}
    with_services = {s["organization_id"] for s in fetch_all(sb, "services")}
    cats = {c["name_en"]: c["id"] for c in fetch_all(sb, "categories", "sort_order")}
    cat_name = {v: k for k, v in cats.items()}
    spec_id = {(cat_name[s["category_id"]], s["name_en"]): s["id"]
               for s in fetch_all(sb, "specific_services", "sort_order")}

    updated, svc_added, problems = 0, 0, []
    for _, r in approved.iterrows():
        oid = org_id.get(r["org"])
        if oid is None:
            problems.append(f"{r['org']}: not in Supabase (renamed?)")
            continue

        values = {f: clean(r[f]) for f in FIELDS}
        values["details_sources"] = clean(r.get("pages_read"))
        # Only a person's "yes" counts as reviewed; "auto" leaves it empty, so
        # unreviewed details are easy to find later in Supabase.
        human = is_yes(r["approve"])
        values["details_reviewed_on"] = date.today().isoformat() if human else None
        # New orgs stay hidden (is_active false) until their details are
        # approved; approving them is what puts them on the dashboard.
        values["is_active"] = True

        pairs = []
        for item in str(r.get("new_services", "")).split(";"):
            if ">" in item:
                c, s = (x.strip() for x in item.split(">", 1))
                if (c, s) in spec_id:
                    pairs.append((c, s))
                else:
                    problems.append(f"{r['org']}: unknown service {item.strip()!r}")
        add_services = pairs and oid not in with_services

        if args.dry_run:
            filled = [f for f in FIELDS if values[f]]
            print(f"{r['org']}: would set {', '.join(filled) or 'nothing'}"
                  + (f"; add {len(pairs)} services" if add_services else ""))
            continue

        sb.table("organizations").update(values).eq("id", oid).execute()
        updated += 1

        if add_services:
            for c, s in pairs:
                svc = sb.table("services").insert({
                    "organization_id": oid, "name": s, "source": "website-extract",
                }).execute().data[0]
                sb.table("service_specifics").insert({
                    "service_id": svc["id"], "specific_service_id": spec_id[(c, s)],
                    "source": "website-extract",
                }).execute()
                sb.table("service_categories").insert({
                    "service_id": svc["id"], "category_id": cats[c],
                }).execute()
                svc_added += 1
            with_services.add(oid)

    if not args.dry_run:
        print(f"Updated {updated} organizations; added {svc_added} services.")
    for p in problems:
        print(f"  ! {p}")


if __name__ == "__main__":
    main()
