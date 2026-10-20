"""
Merge the crawler's new service tags into the dashboard CSV.

Run this before starting the dashboard — app.py reads the merged file.

Usage:
    python merge_services.py
    -> writes GWIorgs_v6.csv
"""

import pandas as pd

ORGS_PATH = "data/GWIorgs_v5.csv"
SERVICES_PATH = "qa/org_services_65_final.csv"
OUTPUT_PATH = "data/GWIorgs_v6.csv"

# Rows the manual QA found to be showing the WRONG organization's services.
# Blanked rather than displayed — a resident acting on these would be
# misdirected to a different organization.
#   Northeast Justice Center  -> CSV URL points at masslegalhelp.org
#   Hands to Help             -> CSV URL points at a Merrimack College page
#   LPTE                      -> shares a URL with Lawrence Partnership
#   Si se puede!              -> correct site, but ~half the tags unsupported
KNOWN_BAD = [
    "Northeast Justice Center",
    "Hands to Help",
    "Lawrence Partnership for Transition to Employment (LPTE)",
    "Si se puede!",
]


def main():
    orgs = pd.read_csv(ORGS_PATH, dtype=str).fillna("")
    new = pd.read_csv(SERVICES_PATH, dtype=str).fillna("")

    orgs.columns = orgs.columns.str.strip()
    new.columns = new.columns.str.strip()

    merged = orgs.drop(columns=["Services"], errors="ignore").merge(
        new[["Name", "Services"]], on="Name", how="left"
    ).fillna("")

    unmatched = merged[merged["Services"].str.strip() == ""]["Name"].tolist()

    blanked = merged["Name"].isin(KNOWN_BAD) & (merged["Services"].str.strip() != "")
    merged.loc[merged["Name"].isin(KNOWN_BAD), "Services"] = ""

    merged.to_csv(OUTPUT_PATH, index=False)

    have = (merged["Services"].str.strip() != "").sum()
    print(f"Wrote {OUTPUT_PATH}")
    print(f"  {have}/{len(merged)} orgs have service tags")
    print(f"  {blanked.sum()} blanked as known-bad (QA)")
    if unmatched:
        print(f"\n  {len(unmatched)} orgs with no services — check name mismatches:")
        for n in unmatched[:15]:
            print(f"    {n}")


if __name__ == "__main__":
    main()