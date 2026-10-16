"""
Step A1: find Lawrence nonprofits that aren't in the directory yet.

Reads the IRS list of Massachusetts tax-exempt organizations and writes a
review sheet of candidates: Lawrence ZIP codes, service-type categories, not
private foundations, not already in the directory.

Before running: download the Massachusetts CSV from the IRS "Exempt
Organizations Business Master File Extract (EO BMF)" page and save it as
data/irs/eo_ma.csv.

Run from the repo root:
    python scripts/find_missing_orgs.py
Then open review/missing_org_candidates.csv and fill in, for each row:
    decision      keep / skip   (leave blank = not decided yet)
    display_name  the name residents know it by (prefilled with a guess)
    website       the org's website, if you keep it
"""

import difflib
import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import LAWRENCE_ZIPS, REVIEW_DIR, ROOT, fetch_all, supabase  # noqa: E402

IRS_CSV = os.path.join(ROOT, "data", "irs", "eo_ma.csv")
OUT = os.path.join(REVIEW_DIR, "missing_org_candidates.csv")

# NTEE major groups (first letter of NTEE_CD). Kept: the kinds of help the
# dashboard lists. Dropped: churches, sports leagues, foundations, etc.
NTEE_KEEP = {
    "B": "Education", "E": "Health care", "F": "Mental health & crisis",
    "I": "Crime & legal", "J": "Employment", "K": "Food & nutrition",
    "L": "Housing & shelter", "M": "Public safety & disaster relief",
    "O": "Youth development", "P": "Human services", "R": "Civil rights & advocacy",
    "S": "Community improvement",
}
NTEE_DROP = {
    "A": "Arts & culture", "C": "Environment", "D": "Animals", "G": "Disease research",
    "H": "Medical research", "N": "Recreation & sports", "Q": "International",
    "T": "Philanthropy & grantmaking", "U": "Science", "V": "Social science",
    "W": "Public policy", "X": "Religion", "Y": "Mutual benefit",
}
PRIVATE_FOUNDATION = {"02", "03", "04"}  # IRS FOUNDATION codes for private foundations

STOPWORDS = {"inc", "incorporated", "corp", "corporation", "co", "llc", "ltd",
             "the", "of", "and", "a", "an", "trust", "assn", "association"}


def norm_name(name: str) -> str:
    name = re.sub(r"\(.*?\)", " ", str(name).lower())   # drop "(GLCAC)"
    name = name.replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", name)
    return " ".join(w for w in words if w not in STOPWORDS)


def best_match(name: str, existing: list[tuple[str, str]]) -> tuple[str, float]:
    """(closest existing org name, similarity 0-1)."""
    n = norm_name(name)
    best, score = "", 0.0
    for orig, e in existing:
        s = difflib.SequenceMatcher(None, n, e).ratio()
        if n and e and (n in e or e in n):
            s = max(s, 0.9)
        if s > score:
            best, score = orig, s
    return best, score


def guess_display_name(irs_name: str) -> str:
    """'LAWRENCE COMMUNITY WORKS INC' -> 'Lawrence Community Works'.
    A starting point for the reviewer, not the final name."""
    name = re.sub(r"\b(INC|INCORPORATED|CORP|CORPORATION|LLC|LTD)\b\.?", "", irs_name)
    small = {"of", "and", "the", "for", "in", "on", "at", "to", "a", "de", "la", "del", "y"}
    acronyms = {"YMCA", "YWCA", "USA", "MA", "ESL", "GED", "PTO", "PTA", "VFW", "NAACP"}
    out = []
    for i, w in enumerate(name.split()):
        lw = w.lower()
        if w.strip(",.") in acronyms or not re.search(r"[aeiouy]", lw):
            out.append(w)                       # YMCA, LGBTQ, "MVRCS"
        elif i and lw in small:
            out.append(lw)
        else:
            out.append(lw.capitalize())
    return " ".join(out).strip(" ,")


def main():
    if not os.path.exists(IRS_CSV):
        sys.exit(f"Missing {os.path.relpath(IRS_CSV, ROOT)} — download the Massachusetts file "
                 "from the IRS EO BMF page first.")

    irs = pd.read_csv(IRS_CSV, dtype=str).fillna("")
    total = len(irs)
    irs["ZIP5"] = irs["ZIP"].str[:5]
    irs = irs[irs["ZIP5"].isin(LAWRENCE_ZIPS)]
    in_lawrence = len(irs)

    irs = irs[~irs["FOUNDATION"].str.zfill(2).isin(PRIVATE_FOUNDATION)]
    irs["NTEE_GROUP"] = irs["NTEE_CD"].str[:1].str.upper()
    irs = irs[~irs["NTEE_GROUP"].isin(NTEE_DROP)]

    client = supabase()
    orgs = fetch_all(client, "organizations")
    existing = [(o["name"], norm_name(o["name"])) for o in orgs]
    known_eins = {str(o.get("ein") or "").replace("-", "") for o in orgs} - {""}

    rows = []
    for _, r in irs.iterrows():
        ein = r["EIN"].zfill(9)
        if ein in known_eins:
            continue
        match, score = best_match(r["NAME"], existing)
        rows.append({
            "decision": "",
            "display_name": guess_display_name(r["NAME"]),
            "website": "",
            "irs_name": r["NAME"],
            "ein": f"{ein[:2]}-{ein[2:]}",
            "street": r["STREET"].title(),
            "city": r["CITY"].title(),
            "zip": r["ZIP5"],
            "category": NTEE_KEEP.get(r["NTEE_GROUP"], "Unknown (no code)"),
            "ntee_code": r["NTEE_CD"],
            "revenue": int(float(r["REVENUE_AMT"])) if r["REVENUE_AMT"].strip() else "",
            "maybe_same_as": match if score >= 0.75 else "",
            "propublica": f"https://projects.propublica.org/nonprofits/organizations/{ein}",
            "search": "https://www.google.com/search?q="
                      + re.sub(r"\s+", "+", guess_display_name(r["NAME"])) + "+Lawrence+MA",
            "notes": "",
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        # Likely duplicates last; otherwise by category, biggest first.
        out["_dup"] = out["maybe_same_as"] != ""
        out["_rev"] = pd.to_numeric(out["revenue"], errors="coerce").fillna(-1)
        out = out.sort_values(["_dup", "category", "_rev"], ascending=[True, True, False])
        out = out.drop(columns=["_dup", "_rev"])

    os.makedirs(REVIEW_DIR, exist_ok=True)
    if os.path.exists(OUT):
        # Rerun (e.g. with a newer IRS file): keep every existing row and its
        # decision, append only orgs that aren't in the sheet yet.
        old = pd.read_csv(OUT, dtype=str).fillna("")
        out = out[~out["ein"].isin(old["ein"])]
        print(f"Existing sheet kept; {len(out)} new candidates appended.")
        out = pd.concat([old, out], ignore_index=True)
    out.to_csv(OUT, index=False)

    dup = int((out["maybe_same_as"] != "").sum()) if not out.empty else 0
    print(f"IRS Massachusetts list:         {total:,} organizations")
    print(f"  in Lawrence ZIP codes:        {in_lawrence:,}")
    print(f"  service types, no foundations: {len(irs):,}")
    print(f"  not already in the directory: {len(out):,}  ({dup} look like an org you already have)")
    print(f"\nWrote {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
