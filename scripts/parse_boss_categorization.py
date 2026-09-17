"""
Turn the boss's categorization markdown into machine-readable assignments.

    .venv/bin/python scripts/parse_boss_categorization.py            # report
    .venv/bin/python scripts/parse_boss_categorization.py --write    # emit CSV

The draft at qa/boss_categorization_2026-09-08.md is the authority on which
category each raw service tag belongs to. This parses it into
data/service_categories.csv so the app can follow it directly rather than
re-deriving categories from regex.

Because the draft lists each multi-category tag under every category it belongs
to, the same tag appears several times; those are merged into one row with a
list of categories. It was generated against an older snapshot, so the report
also shows which of the tags now in GWIorgs_v6.csv the draft does not cover.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pandas as pd

from audit_service_tags import smart_split

ROOT = os.path.dirname(_HERE)
DOC = os.path.join(ROOT, "qa", "boss_categorization_2026-09-08.md")
ORGS = os.path.join(ROOT, "data", "GWIorgs_v6.csv")
OUT = os.path.join(ROOT, "data", "service_categories.csv")
CORRECTIONS = os.path.join(ROOT, "qa", "categorization_corrections.csv")

NEEDS_REVIEW = "Needs Review"

# "- Tag *(also: Cat A, Cat B)*" — the also-list is comma separated, but
# "Health Care (Clinical)" and "Sports, Recreation & Fitness" both contain
# punctuation that naive splitting breaks, so categories are matched by name
# against the set of headings rather than split on commas.
_ITEM = re.compile(r"^- (.+?)(?:\s*\*\(also:\s*(.+?)\)\*)?\s*$")
_HEADING = re.compile(r"^## (.+?)(?:\s*\(\d+\))?\s*$")


def norm(tag: str) -> str:
    """Normalise a tag for comparison.

    The CSV carries typographic apostrophes ("Kid's Net") while the draft was
    retyped with straight ones. Without folding them, one tag reads as both a
    coverage gap and a stale entry at the same time.
    """
    return (
        str(tag)
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .strip()
    )


def parse() -> tuple[dict[str, list[str]], list[str], list[str]]:
    """-> ({raw tag: [categories]}, category order, needs-review tags)"""
    categories: list[str] = []
    assignments: defaultdict[str, list[str]] = defaultdict(list)
    review: list[str] = []
    current = None

    for line in open(DOC, encoding="utf-8"):
        line = line.rstrip("\n")
        h = _HEADING.match(line)
        if h:
            current = h.group(1).strip()
            if current.startswith(NEEDS_REVIEW):
                current = NEEDS_REVIEW
            elif current not in categories:
                categories.append(current)
            continue

        m = _ITEM.match(line)
        if not m or current is None:
            continue

        tag = norm(m.group(1))
        if current == NEEDS_REVIEW:
            review.append(tag)
            continue

        if current not in assignments[tag]:
            assignments[tag].append(current)

        # Cross-references are redundant with the tag's own listing under the
        # other category, but parse them anyway: if the draft ever names a
        # category in an (also:) that it forgot to list the tag under, this is
        # what catches it.
        if m.group(2):
            for cat in categories_in(m.group(2), categories):
                if cat not in assignments[tag]:
                    assignments[tag].append(cat)

    return dict(assignments), categories, review


def categories_in(text: str, known: list[str]) -> list[str]:
    """Pull category names out of an (also: …) clause by longest-name match."""
    found = []
    for cat in sorted(known, key=len, reverse=True):
        if cat in text:
            found.append(cat)
            text = text.replace(cat, "")
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write data/service_categories.csv")
    args = ap.parse_args()

    # Parsed twice: an (also:) clause can name a category whose own heading
    # has not been seen yet on the first pass, so the second run resolves
    # against the complete heading list.
    parse()
    assignments, categories, review = parse()

    print(f"parsed {len(assignments)} tags into {len(categories)} categories")
    print(f"{len(review)} tags in Needs Review")
    multi = {t: c for t, c in assignments.items() if len(c) > 1}
    print(f"{len(multi)} tags carry more than one category\n")

    df = pd.read_csv(ORGS, dtype=str).fillna("")
    live = {norm(t) for s in df["Services"] for t in smart_split(s)}

    doc_tags = set(assignments) | set(review)
    missing = sorted(live - doc_tags)
    stale = sorted(doc_tags - live)

    print(f"live CSV has {len(live)} raw tags")
    print(f"  covered by the draft : {len(live & doc_tags)}")
    print(f"  NOT in the draft     : {len(missing)}")
    print(f"  in draft, not in CSV : {len(stale)}\n")

    if missing:
        print("TAGS THE DRAFT DOES NOT COVER (need a category assigned):")
        for t in missing:
            print(f"      {t}")
        print()
    if stale:
        print("IN THE DRAFT BUT NO LONGER IN THE DATA:")
        for t in stale:
            print(f"      {t}")

    # Corrections are an overlay, not an edit to the draft. The boss's document
    # stays exactly as received; every deviation is one reviewable row that can
    # be deleted to restore the original assignment.
    corrections: dict[str, tuple[list[str], str]] = {}
    if os.path.exists(CORRECTIONS):
        with open(CORRECTIONS, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                cats = [c.strip() for c in row["Categories"].split(";") if c.strip()]
                corrections[norm(row["RawService"])] = (cats, row.get("Reason", ""))

    unknown = {
        c
        for cats, _r in corrections.values()
        for c in cats
        if c not in categories
    }
    if unknown:
        print(f"\nERROR: corrections name categories not in the draft: {sorted(unknown)}")
        sys.exit(1)

    unused = [t for t in corrections if t not in assignments]
    if unused:
        print(f"\nWARNING: corrections for tags not in the draft: {unused}")

    if corrections:
        print(f"\n{len(corrections)} correction(s) applied from {os.path.basename(CORRECTIONS)}:")
        for tag, (cats, _r) in corrections.items():
            was = assignments.get(tag, [])
            print(f"      {tag}")
            print(f"        draft : {'; '.join(was) or '(none)'}")
            print(f"        now   : {'; '.join(cats)}")

    if args.write:
        with open(OUT, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["RawService", "Categories", "Source"])
            for tag in sorted(assignments):
                if tag in corrections:
                    w.writerow([tag, "; ".join(corrections[tag][0]), "corrected"])
                else:
                    w.writerow([tag, "; ".join(assignments[tag]), "boss-draft-2026-09-08"])
            for tag in sorted(norm(t) for t in review):
                w.writerow([tag, "", "boss-draft-2026-09-08:needs-review"])
        print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
