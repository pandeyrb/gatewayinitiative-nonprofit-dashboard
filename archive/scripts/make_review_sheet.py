"""
Build a manual-review sheet from the crawler output.

Produces ONE ROW PER TAG so each tag can be judged individually, plus a
per-org row for services the crawler missed entirely.

Usage:
    python make_review_sheet.py
    -> writes qa_review_sheet.csv, open it in Excel or Google Sheets
"""

import re

import pandas as pd

INPUT_PATH = "org_services_65_final.csv"
OUTPUT_PATH = "qa_review_sheet.csv"
SAMPLE_SIZE = 15

# Orgs we already know are broken — include them so the sample contains
# real failures, not just the rows that happened to work.
MUST_INCLUDE = [
    "Northeast Justice Center",          # reads masslegalhelp.org
    "Hands to Help",                     # reads Merrimack College
    "Lawrence Partnership for Transition to Employment (LPTE)",  # shared URL
    "Bread and Roses Housing",           # redirects to CAF
    "St. Mary's Parish",                 # lost its food pantry
    "Greater Lawrence Family Health",    # lost dental + behavioral
]


def split_tags(s: str) -> list[str]:
    """Split on commas that are NOT inside parentheses."""
    if not isinstance(s, str) or not s.strip():
        return []
    return [p.strip() for p in re.split(r",(?![^(]*\))", s) if p.strip()]


def main():
    df = pd.read_csv(INPUT_PATH, dtype=str).fillna("")

    has_services = df[df["Services"].str.strip() != ""]

    picked = has_services[has_services["Name"].isin(MUST_INCLUDE)]
    remaining = has_services[~has_services["Name"].isin(MUST_INCLUDE)]
    n_more = max(0, SAMPLE_SIZE - len(picked))
    # seeded so re-running gives the same sample
    picked = pd.concat([picked, remaining.sample(n=min(n_more, len(remaining)), random_state=42)])

    rows = []
    for _, org in picked.iterrows():
        new_tags = split_tags(org["Services"])
        old_tags = split_tags(org["Old_Services"])

        for tag in new_tags:
            rows.append({
                "Org": org["Name"],
                "Tag": tag,
                "Verdict": "",          # accurate / wrong / vague
                "Notes": "",
                "Landed_URL": org["Landed_URL"],
                "Flag": org["Service_Method"],
            })

        # one row per old tag so you can mark whether the crawler confirmed it
        for tag in old_tags:
            rows.append({
                "Org": org["Name"],
                "Tag": f"[OLD] {tag}",
                "Verdict": "",          # confirmed / crawler-missed / org-stopped
                "Notes": "",
                "Landed_URL": org["Landed_URL"],
                "Flag": org["Service_Method"],
            })

        # blank row to write in anything the site offers that neither list has
        rows.append({
            "Org": org["Name"],
            "Tag": "[MISSING — write in anything the site offers that's not listed]",
            "Verdict": "",
            "Notes": "",
            "Landed_URL": org["Landed_URL"],
            "Flag": org["Service_Method"],
        })

    out = pd.DataFrame(rows)
    out.to_csv(OUTPUT_PATH, index=False)

    print(f"Wrote {OUTPUT_PATH}")
    print(f"  {picked['Name'].nunique()} orgs, {len(out)} rows to review")
    print()
    print("Verdict values to use:")
    print("  new tags: accurate | wrong | vague")
    print("  [OLD] tags: confirmed | crawler-missed | org-stopped")


if __name__ == "__main__":
    main()
