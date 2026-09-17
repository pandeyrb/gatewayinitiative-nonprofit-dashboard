"""
Show how every raw service string in the CSV resolves to a canonical tag.

Run this after adding organizations or editing service_taxonomy.py:

    .venv/bin/python scripts/audit_service_tags.py            # summary
    .venv/bin/python scripts/audit_service_tags.py --unmatched  # only gaps
    .venv/bin/python scripts/audit_service_tags.py --csv out.csv

Anything listed under UNMATCHED is invisible to the sidebar filter — either
add a pattern for it in RULES, or add it to OVERRIDES as None if it isn't a
service (some raw strings are prose fragments, not services).
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from service_taxonomy import TAGS, canonicalize_all, sort_key

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "GWIorgs_v6.csv",
)


def smart_split(s: str) -> list[str]:
    """Mirror of app._smart_split — kept in sync by hand, it is four lines."""
    if not s:
        return []
    parts = [p.strip() for p in re.split(r",(?![^(]*\))", s) if p.strip()]
    parts = [re.sub(r"^and\s+", "", p, flags=re.IGNORECASE) for p in parts]
    return [p for p in parts if p]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unmatched", action="store_true", help="only show unmatched raw tags")
    ap.add_argument("--csv", metavar="PATH", help="write the full raw->canonical map to CSV")
    args = ap.parse_args()

    df = pd.read_csv(CSV_PATH, dtype=str).fillna("")

    raw_counts: Counter = Counter()
    for services in df["Services"]:
        raw_counts.update(smart_split(services))

    by_canonical: defaultdict[str, list[str]] = defaultdict(list)
    unmatched: list[str] = []
    for raw in raw_counts:
        tags = canonicalize_all(raw)
        for tag in tags:
            by_canonical[tag].append(raw)
        if not tags:
            unmatched.append(raw)

    org_counts: Counter = Counter()
    for services in df["Services"]:
        tags = {t for r in smart_split(services) for t in canonicalize_all(r)}
        org_counts.update(tags)

    if not args.unmatched:
        print(f"{len(raw_counts)} raw tags  ->  {len(by_canonical)} canonical tags in use")
        print(f"{len(TAGS)} canonical tags defined, {len(TAGS) - len(by_canonical)} unused\n")

        for tag in sorted(by_canonical, key=sort_key):
            print(f"{TAGS[tag][0]} · {tag}   [{org_counts[tag]} orgs]")
            for raw in sorted(by_canonical[tag]):
                print(f"      {raw}")
            print()

        unused = [t for t in TAGS if t not in by_canonical]
        if unused:
            print("CANONICAL TAGS WITH NO ORGS (will not appear in the dropdown):")
            for tag in sorted(unused, key=sort_key):
                print(f"      {TAGS[tag][0]} · {tag}")
            print()

    print(f"UNMATCHED ({len(unmatched)} raw tags, not filterable):")
    for raw in sorted(unmatched):
        print(f"      {raw}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["RawService", "Category", "CanonicalTag", "OrgCount"])
            for raw in sorted(raw_counts):
                tags = canonicalize_all(raw)
                if not tags:
                    w.writerow([raw, "", "", raw_counts[raw]])
                for tag in tags:
                    w.writerow([raw, TAGS[tag][0], tag, raw_counts[raw]])
        print(f"\nWrote {args.csv}")


if __name__ == "__main__":
    main()
