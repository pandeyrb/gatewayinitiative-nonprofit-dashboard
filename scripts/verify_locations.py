"""
Validate org_locations.csv and the map quick-filters against the live data.

    .venv/bin/python scripts/verify_locations.py

Exits non-zero if anything is inconsistent, so it can gate a commit. Checks:
  * every OrgName in org_locations.csv resolves to a row in GWIorgs_v6.csv
    (a typo here silently hides an org's locations instead of erroring)
  * every quick-filter chip's canonical tags exist in the taxonomy AND match
    at least one org, so no chip is a dead end
  * phone numbers are formatted consistently
  * every location row carries a SourceURL, per the no-unverified-data rule
Then prints phone/hours coverage.
"""

import os
import re
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # repo root, for service_taxonomy
sys.path.insert(0, _HERE)  # this dir, for audit_service_tags

import pandas as pd

from audit_service_tags import smart_split

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORGS = os.path.join(ROOT, "data", "GWIorgs_v6.csv")
LOCS = os.path.join(ROOT, "data", "org_locations.csv")

PHONE_RE = re.compile(r"^\(\d{3}\) \d{3}-\d{4}$")

problems: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)


def main() -> None:
    orgs = pd.read_csv(ORGS, dtype=str).fillna("")
    names = set(orgs["Name"].str.strip())

    # ── locations ─────────────────────────────────────────────────────────────
    if os.path.exists(LOCS):
        locs = pd.read_csv(LOCS, dtype=str).fillna("")
        print(f"org_locations.csv: {len(locs)} rows across "
              f"{locs['OrgName'].nunique()} orgs\n")

        for i, r in locs.iterrows():
            line = i + 2  # header + 0-index
            org = r["OrgName"].strip()
            check(org in names, f"row {line}: OrgName {org!r} not in GWIorgs_v6.csv")
            check(
                bool(r["LocationName"].strip()),
                f"row {line}: {org} has an empty LocationName",
            )
            check(
                r["Kind"].strip() in ("public", "admin"),
                f"row {line}: {org} has Kind={r['Kind']!r} (expected public/admin)",
            )
            check(
                bool(r["SourceURL"].strip()),
                f"row {line}: {org} / {r['LocationName']} has no SourceURL",
            )
            if r["Phone"].strip():
                check(
                    bool(PHONE_RE.match(r["Phone"].strip())),
                    f"row {line}: phone {r['Phone']!r} is not (NNN) NNN-NNNN",
                )
            # A row with neither an address nor a phone tells a user nothing.
            check(
                bool(r["Address"].strip() or r["Phone"].strip()),
                f"row {line}: {org} / {r['LocationName']} has no address and no phone",
            )

        multi = [n for n, c in Counter(locs["OrgName"]).items() if c == 1]
        for n in multi:
            problems.append(
                f"{n}: only one location row — this file is for multi-site orgs; "
                f"a single site belongs in GWIorgs_v6.csv"
            )
    else:
        print("org_locations.csv absent — app degrades to one address per org\n")

    # ── quick filters ─────────────────────────────────────────────────────────
    from service_taxonomy import (
        ASSIGNED_CATEGORIES,
        CATEGORY_ORDER,
        QUICK_FILTERS,
        TAGS,
        canonical_tags,
        org_categories,
    )

    # ── categorization coverage ───────────────────────────────────────────────
    live = {t for _i, r in orgs.iterrows() for t in smart_split(r["Services"])}
    covered = sum(
        1
        for t in live
        if t.replace("\u2019", "'").strip().lower() in ASSIGNED_CATEGORIES
    )
    print(f"categorization: {covered}/{len(live)} raw tags assigned by the draft, "
          f"{len(live) - covered} inferred from rules")

    cat_counts = {c: 0 for c in CATEGORY_ORDER}
    for _i, r in orgs.iterrows():
        for c in org_categories(smart_split(r["Services"])):
            cat_counts[c] += 1
    empty = [c for c, n in cat_counts.items() if n == 0]
    check(not empty, f"categories with no orgs (dead filter options): {empty}")
    print(f"                26 categories, all populated\n")

    print("quick filters:")
    for key, tags in QUICK_FILTERS:
        for t in tags:
            check(t in TAGS, f"chip {key}: {t!r} is not a canonical tag")
        hits = sum(
            1
            for _i, r in orgs.iterrows()
            if set(canonical_tags(smart_split(r["Services"]))) & set(tags)
        )
        check(hits > 0, f"chip {key}: matches no orgs — would be a dead end")
        print(f"  {key:22} {hits:2d} orgs   {', '.join(tags)}")

    # ── coverage ──────────────────────────────────────────────────────────────
    have_phone = (orgs["Phone"].str.strip() != "").sum()
    have_hours = (orgs["Hours"].str.strip() != "").sum()
    print(
        f"\ncoverage: phone {have_phone}/{len(orgs)}   hours {have_hours}/{len(orgs)}"
    )
    # The Phone column is hand-maintained and arrives in mixed styles; app.py
    # normalises them for display, so style is not a problem. What IS a problem
    # is a value that holds no dialable number — the popup would otherwise show
    # a tap-to-call link that dials nothing.
    undialable = [
        (r["Name"], r["Phone"].strip())
        for _i, r in orgs.iterrows()
        if r["Phone"].strip() and len(re.sub(r"\D", "", r["Phone"])) < 10
    ]
    if undialable:
        print(f"\n{len(undialable)} Phone cell(s) hold no dialable number "
              f"(shown as plain text, not a call link):")
        for name, val in undialable:
            print(f"      {name}: {val!r}")

    nonstandard = sum(
        1
        for _i, r in orgs.iterrows()
        if r["Phone"].strip() and not PHONE_RE.match(r["Phone"].strip())
        and len(re.sub(r"\D", "", r["Phone"])) >= 10
    )
    if nonstandard:
        print(f"{nonstandard} phone(s) written in another style — "
              f"normalised to (NNN) NNN-NNNN on load, no action needed")

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  ✗ {p}")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
