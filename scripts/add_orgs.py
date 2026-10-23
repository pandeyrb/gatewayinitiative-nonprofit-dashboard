"""
Step A3: add the candidates you marked "keep" to Supabase.

Run from the repo root, after filling in review/missing_org_candidates.csv:
    python scripts/add_orgs.py --dry-run    # show what would be added
    python scripts/add_orgs.py              # add them

For each "keep" row it adds the organization (name, website, EIN) and its
address as the main location, with map coordinates from the US Census
geocoder (free, no key). Rows already in the directory, by EIN or name, are
skipped, so running it twice is safe.

New orgs have no services yet, so they won't show under any category until
you run scripts/extract_details.py and approve its suggestions.

Duplicate guard: a "keep" is NOT added, and is listed under "needs a person"
(with a note in the sheet's notes column), when
  - the sheet flags it as maybe the same as an existing org (maybe_same_as),
  - it is a known rename of an org already listed (KNOWN_RENAMES),
  - its name is close to an existing org's, ignoring word order, accents,
    "Inc." and acronyms in brackets, or
  - it duplicates another "keep" in the same batch (same EIN or name).
Clear the note and fill in website/display_name if it really is new.
"""

import argparse
import difflib
import os
import re
import sys
import unicodedata

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import REVIEW_DIR, ROOT, fetch_all, supabase  # noqa: E402

CANDIDATES = os.path.join(REVIEW_DIR, "missing_org_candidates.csv")
CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
DUP_NOTE = "POSSIBLE DUPLICATE"

# Same organization, different name: (pattern on the candidate's name, which
# existing org(s) it is). Matched against the folded, lowercased name.
KNOWN_RENAMES = [
    (r"northeast legal aid", "Northeast Justice Center"),
    (r"bread (and|&) roses", "Bread and Roses Kitchen / Bread and Roses Housing"),
    (r"family services of (the )?merrimack valley|\bfsmv\b", "Family Services of Merrimack Valley/LMCC"),
    (r"lawrence family development", "Lawrence Prospera"),
    # Lawrence General Hospital's parent took this name; whole name only, so a
    # separate service like "Merrimack Health Community Medical Associates" isn't caught.
    (r"^merrimack health(,? inc\.?)?$", "Lawrence General Hospital"),
]
STOPWORDS = {"inc", "incorporated", "corp", "corporation", "co", "llc", "ltd", "the",
             "of", "and", "a", "an", "assn", "association", "ma", "massachusetts"}


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text))
                   if not unicodedata.combining(c))


def norm(name: str) -> str:
    """'Sueños Basketball, Inc. (SB)' -> 'basketball suenos' style key parts."""
    name = re.sub(r"\(.*?\)", " ", fold(name).lower()).replace("&", " and ")
    name = re.sub(r"['’]s\b", "", name)                 # "Patrick's" -> "Patrick"
    name = re.sub(r"\bst\b\.?", "saint", name)          # "St." -> "Saint"
    return " ".join(w for w in re.findall(r"[a-z0-9]+", name) if w not in STOPWORDS)


def similarity(a: str, b: str) -> float:
    """Name similarity 0-1 that ignores word order ('Boys and Girls Club of
    Lawrence' vs 'Lawrence Boys and Girls Club')."""
    if not a or not b:
        return 0.0
    straight = difflib.SequenceMatcher(None, a, b).ratio()
    sorted_ = difflib.SequenceMatcher(None, " ".join(sorted(a.split())),
                                      " ".join(sorted(b.split()))).ratio()
    contained = 0.9 if (len(a) >= 8 and a in b) or (len(b) >= 8 and b in a) else 0.0
    return max(straight, sorted_, contained)


def duplicate_of(row, existing: list[tuple[str, str]]) -> str:
    """Why this candidate looks like an org already in the directory, or ""."""
    if row.get("maybe_same_as", "").strip():
        return f"maybe the same as {row['maybe_same_as'].strip()}"
    names = [n for n in (row.get("display_name", ""), row.get("irs_name", "")) if n.strip()]
    for pattern, target in KNOWN_RENAMES:
        if any(re.search(pattern, fold(n).lower()) for n in names):
            return f"known rename of {target}"
    best, score = "", 0.0
    for n in names:
        for orig, e in existing:
            s = similarity(norm(n), e)
            if s > score:
                best, score = orig, s
    return f"name close to {best}" if score >= 0.8 else ""


def geocode(street: str, city: str, zip5: str):
    """(lat, lon) from the Census geocoder, or (None, None)."""
    try:
        r = requests.get(CENSUS, timeout=20, params={
            "address": f"{street}, {city}, MA {zip5}",
            "benchmark": "Public_AR_Current", "format": "json",
        })
        matches = r.json()["result"]["addressMatches"]
        if matches:
            c = matches[0]["coordinates"]
            return c["y"], c["x"]
    except Exception:
        pass
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(CANDIDATES):
        sys.exit("Run scripts/find_missing_orgs.py first.")
    c = pd.read_csv(CANDIDATES, dtype=str).fillna("")
    keep = c[c["decision"].str.strip().str.lower() == "keep"]
    if keep.empty:
        sys.exit('No rows marked "keep" in the decision column yet.')

    client = supabase()
    orgs = fetch_all(client, "organizations")
    have_names = {o["name"].strip().lower() for o in orgs}
    have_eins = {str(o.get("ein") or "") for o in orgs} - {""}
    region = (client.table("regions").select("id").eq("name", "Lawrence").execute().data or [{}])[0].get("id")

    existing = [(o["name"], norm(o["name"])) for o in orgs]

    added, skipped, no_coords, held = [], [], [], []
    batch_eins, batch_names = set(), {}
    for i, r in keep.iterrows():
        name = r["display_name"].strip() or r["irs_name"].strip()
        if r["ein"] in have_eins or name.lower() in have_names:
            skipped.append(name)
            continue
        why = duplicate_of(r, existing)
        key = norm(name)
        if not why and (r["ein"] in batch_eins or key in batch_names):
            why = f"same as another keep in this batch ({batch_names.get(key, r['ein'])})"
        if why:
            held.append(f"{name}: {why}")
            note = f"{DUP_NOTE}: {why}"
            if not c.at[i, "notes"].startswith(DUP_NOTE):
                c.at[i, "notes"] = (note + (" | " + c.at[i, "notes"] if c.at[i, "notes"] else ""))
            continue
        batch_eins.add(r["ein"])
        batch_names[key] = name
        if args.dry_run:
            added.append(name)
            continue

        lat, lon = geocode(r["street"], r["city"], r["zip"])
        if lat is None:
            no_coords.append(name)
        org = client.table("organizations").insert({
            "name": name,
            "url": r["website"].strip() or None,
            "ein": r["ein"],
            "org_type": "Non-profit organization",
        }).execute().data[0]
        if region:
            client.table("organization_regions").insert(
                {"organization_id": org["id"], "region_id": region}).execute()
        client.table("locations").insert({
            "organization_id": org["id"], "region_id": region,
            "is_main": True, "kind": "public",
            "address": r["street"].strip() or None, "city": r["city"].strip() or None,
            "state": "MA", "zip": r["zip"].strip() or None,
            "latitude": lat, "longitude": lon, "geo_source": "census" if lat else None,
        }).execute()
        have_names.add(name.lower())
        have_eins.add(r["ein"])
        added.append(name)

    # The notes are review data, not Supabase: saved on dry runs too, so the
    # sheet shows what was held back and why.
    c.to_csv(CANDIDATES, index=False)

    verb = "Would add" if args.dry_run else "Added"
    print(f"{verb} {len(added)}: " + (", ".join(added) or "none"))
    if held:
        print(f"\nNeeds a person, {len(held)} possible duplicates (not added):")
        for h in held:
            print(f"  - {h}")
    if skipped:
        print(f"Skipped {len(skipped)} already in the directory: " + ", ".join(skipped))
    if no_coords:
        print(f"No map location found for {len(no_coords)} (fix lat/long in Supabase): "
              + ", ".join(no_coords))
    if added and not args.dry_run:
        print("\nNext: python scripts/extract_details.py --only-new")


if __name__ == "__main__":
    main()
