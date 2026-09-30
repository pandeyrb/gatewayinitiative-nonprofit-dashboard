"""
Check data/specific_services.csv against the data and the categorization draft.

    .venv/bin/python scripts/check_specific_services.py

specific_services.csv groups every raw service tag in GWIorgs_v6.csv under a
plain-language specific service, inside each of its categories. It is meant to
be edited by hand, so this checks the edits. Exits non-zero on any of:
  * a raw service in the CSV that no organization lists (a typo, or stale)
  * a service the draft categorized that lands in a category the draft did
    not give it, or is missing from one it did (the draft is the authority on
    categories; data/service_categories.csv is its parsed form)
  * a service from the draft's Needs Review list given a category, other than
    the Sarah's Place fragments reunited on purpose (Source=reunited-fragment)
  * a raw service in the data that is neither in the CSV nor in Needs Review,
    so it would silently have no category (add it, Source=added-YYYY-MM-DD)
Then prints how many specific services each category offers.
"""

import csv
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pandas as pd

from audit_service_tags import smart_split
from service_taxonomy import CATEGORY_ORDER, _normalize

ROOT = os.path.dirname(_HERE)
ORGS = os.path.join(ROOT, "data", "GWIorgs_v6.csv")
DRAFT = os.path.join(ROOT, "data", "service_categories.csv")
SPECIFIC = os.path.join(ROOT, "data", "specific_services.csv")


def key(tag: str) -> str:
    return _normalize(tag).lower()


def main() -> int:
    orgs = pd.read_csv(ORGS, dtype=str).fillna("")
    in_data = {key(t): t for s in orgs["Services"] for t in smart_split(s)}

    draft: dict[str, set[str]] = {}
    needs_review: set[str] = set()
    with open(DRAFT, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cats = {c.strip() for c in row["Categories"].split(";") if c.strip()}
            if cats:
                draft[key(row["RawService"])] = cats
            else:
                needs_review.add(key(row["RawService"]))

    placed: dict[str, set[str]] = defaultdict(set)
    sources: dict[str, set[str]] = defaultdict(set)
    per_cat: dict[str, set[str]] = defaultdict(set)
    problems: list[str] = []
    with open(SPECIFIC, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            k, cat = key(row["RawService"]), row["Category"].strip()
            if cat not in CATEGORY_ORDER:
                problems.append(f"unknown category {cat!r} for {row['RawService']!r}")
            if k not in in_data:
                problems.append(f"not in the data: {row['RawService']!r} ({cat})")
            if row["SpecificService"].strip() and not row["SpecificService_es"].strip():
                problems.append(f"no Spanish name: {row['SpecificService']!r} ({cat})")
            placed[k].add(cat)
            sources[k].add(row["Source"].strip())
            if row["SpecificService"].strip():
                per_cat[cat].add(row["SpecificService"].strip())

    for k, cats in placed.items():
        if k in draft:
            extra, missing = cats - draft[k], draft[k] - cats
            if extra:
                problems.append(f"outside the draft: {in_data.get(k, k)!r} in {sorted(extra)}")
            if missing:
                problems.append(f"missing a draft category: {in_data.get(k, k)!r} not in {sorted(missing)}")
        elif k in needs_review and "reunited-fragment" not in sources[k]:
            problems.append(f"Needs Review tag given a category: {in_data.get(k, k)!r}")

    for k, t in in_data.items():
        if k in draft and k not in placed:
            problems.append(f"draft service not grouped: {t!r} (draft: {sorted(draft[k])})")
        elif k not in draft and k not in needs_review and k not in placed:
            problems.append(f"new service with no category: {t!r}")

    print("specific services per category:")
    for c in CATEGORY_ORDER:
        print(f"  {len(per_cat[c]):3d}  {c}")
    print(f"  {sum(len(v) for v in per_cat.values()):3d}  total\n")
    left_out = sorted(t for k, t in in_data.items() if k not in placed)
    print(f"{len(left_out)} services left uncategorized (the draft's Needs Review):")
    print("  " + ", ".join(left_out) + "\n")

    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print("  " + p)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
