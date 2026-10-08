"""
Load the GWI directory from the repo's CSVs into Supabase.

Run from the repo root:
    python scripts/load_supabase.py --dry-run    # build everything, print counts, touch nothing
    python scripts/load_supabase.py              # wipe the tables and load them fresh

It is a full reload, not an update: every run deletes the directory tables and
inserts them again from the CSVs. That is what you want while the CSVs are still
the source of truth. Once people start editing data in Supabase directly, stop
running this, or their edits will be overwritten.

search_log is never touched.

Needs SUPABASE_URL and SUPABASE_SECRET_KEY in .env. The secret key bypasses the
row-level security rules, so it stays on your machine — never in the app or git.
"""

import argparse
import os
import re
import sys

import pandas as pd

# service_taxonomy and search_utils live in the repo root, one level up.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from search_utils import ALIAS_GROUPS  # noqa: E402
from service_taxonomy import (  # noqa: E402
    CATEGORY_ES,
    CATEGORY_ORDER,
    QUICK_FILTERS,
    SPECIFIC_ES,
    SPECIFIC_ORDER,
    categories_for,
    specifics_for,
)

ORGS_CSV = os.path.join(ROOT, "data", "GWIorgs_v6.csv")
LOCATIONS_CSV = os.path.join(ROOT, "data", "org_locations.csv")
SPECIFIC_CSV = os.path.join(ROOT, "data", "specific_services.csv")

REGION = ("Lawrence", "MA")

# Button text, copied from the T dict in app.py (quick_* and need_* keys).
# From here on the database is where these get edited.
QUICK_FILTER_LABELS = {
    "quick_food": ("Food", "Comida",
                   "Pantries and free meals", "Despensas y comidas gratis"),
    "quick_housing": ("Housing & shelter", "Vivienda y refugio",
                      "Rent help and shelters", "Ayuda con alquiler y refugios"),
    "quick_health": ("Health care", "Atención médica",
                     "Clinics, hospitals and insurance", "Clínicas, hospitales y seguro médico"),
    "quick_immigration": ("Immigration help", "Ayuda de inmigración",
                          "Legal help and citizenship", "Ayuda legal y ciudadanía"),
    "quick_jobs": ("Jobs & training", "Empleo y capacitación",
                   "Job search and training", "Empleo y capacitación"),
}


# ── helpers ───────────────────────────────────────────────────────────────────
def smart_split(s: str) -> list[str]:
    """Same as app.py's _smart_split: commas outside parentheses, minus a
    leading Oxford-comma "and"."""
    if not s:
        return []
    parts = [p.strip() for p in re.split(r",(?![^(]*\))", s) if p.strip()]
    parts = [re.sub(r"^and\s+", "", p, flags=re.IGNORECASE) for p in parts]
    return [p for p in parts if p]


def clean(v):
    """'' and NaN -> None, so empty cells become NULL rather than ''."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    v = str(v).strip()
    return v or None


def pad_zip(z):
    z = clean(z)
    return z.zfill(5) if z and z.isdigit() and len(z) < 5 else z


def to_float(v):
    v = clean(v)
    try:
        return float(v) if v else None
    except ValueError:
        return None


def same_address(a, b) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


# ── build every row in memory first ───────────────────────────────────────────
def build():
    orgs = pd.read_csv(ORGS_CSV, dtype=str).fillna("")
    orgs = orgs[orgs["Name"].str.strip() != ""]
    locs = pd.read_csv(LOCATIONS_CSV, dtype=str).fillna("")

    unknown = set(locs["OrgName"].str.strip()) - set(orgs["Name"].str.strip())
    if unknown:
        sys.exit(f"org_locations.csv names orgs missing from GWIorgs_v6.csv: {sorted(unknown)}")

    data = {"notes": []}

    data["categories"] = [
        {"name_en": c, "name_es": CATEGORY_ES.get(c, c), "sort_order": i}
        for i, c in enumerate(CATEGORY_ORDER)
    ]

    data["specifics"] = [
        {"category": c, "name_en": s, "name_es": SPECIFIC_ES.get((c, s), s), "sort_order": i}
        for c in CATEGORY_ORDER
        for i, s in enumerate(SPECIFIC_ORDER.get(c, []))
    ]

    # Source column per (raw service, category, specific), for service_specifics.
    spec_csv = pd.read_csv(SPECIFIC_CSV, dtype=str).fillna("")
    source_of = {
        (r.RawService.strip().lower(), r.Category.strip(), r.SpecificService.strip()): r.Source
        for r in spec_csv.itertuples()
    }

    data["orgs"], data["services"], data["locations"] = [], [], []
    for _i, r in orgs.iterrows():
        name = r["Name"].strip()
        data["orgs"].append({
            "name": name,
            "url": clean(r["URL"]),
            "org_type": clean(r["OrgType"]),
            "service_area": clean(r["ServiceArea"]),
            "phone": clean(r["Phone"]),
            "strategic_plan_url": clean(r.get("Strategic Plan")),
            "impact_report_url": clean(r.get("Impact Report")),
        })

        # Same fallback as the app: no Services text -> use ServiceArea.
        raw_list = smart_split(r["Services"]) or smart_split(r["ServiceArea"])
        for raw in dict.fromkeys(raw_list):  # de-dupe, keep order
            specifics = sorted(specifics_for([raw]))
            data["services"].append({
                "org": name,
                "name": raw,
                "specifics": [(c, s, source_of.get((raw.lower(), c, s))) for c, s in specifics],
                "categories": categories_for(raw),
            })

        main = {
            "org": name,
            "is_main": True,
            "kind": "public",
            "name": None,
            "address": clean(r["Address"]),
            "city": clean(r["City"]),
            "state": clean(r["State"]) or "MA",
            "zip": pad_zip(r["Zip"]),
            "latitude": to_float(r["Latitude"]),
            "longitude": to_float(r["Longitude"]),
            "geo_source": None,
            "phone": clean(r["Phone"]),
            "hours_text": clean(r["Hours"]),
            "services_here": None,
            "source_url": clean(r["HoursSourceURL"]),
            "verified_on": clean(r["HoursVerifiedOn"]),
        }

        # Orgs with several sites: org_locations.csv is the detailed record.
        # The site at the org's main address becomes is_main, so the address
        # isn't stored twice.
        extra = locs[locs["OrgName"].str.strip() == name]
        if extra.empty:
            data["locations"].append(main)
            continue

        main_found = False
        for _j, l in extra.iterrows():
            is_main = not main_found and same_address(l["Address"], r["Address"])
            main_found = main_found or is_main
            data["locations"].append({
                "org": name,
                "is_main": is_main,
                "kind": clean(l["Kind"]) or "public",
                "name": clean(l["LocationName"]),
                "address": clean(l["Address"]),
                "city": clean(l["City"]),
                "state": clean(l["State"]) or "MA",
                "zip": pad_zip(l["Zip"]),
                "latitude": to_float(l["Latitude"]),
                "longitude": to_float(l["Longitude"]),
                "geo_source": clean(l["GeoSource"]),
                "phone": clean(l["Phone"]),
                "hours_text": clean(l["Hours"]) or (main["hours_text"] if is_main else None),
                "services_here": clean(l["ServicesHere"]),
                "source_url": clean(l["SourceURL"]),
                "verified_on": clean(l["VerifiedOn"]),
            })
        if not main_found:
            data["locations"].append(main)
            data["notes"].append(
                f"{name}: main address {r['Address']!r} isn't one of its listed sites; "
                "added it as the main location. Check whether it's public or admin-only."
            )

    data["synonyms"] = [
        {"group_name": sorted(group)[0], "term": term, "lang": "en"}
        for group in ALIAS_GROUPS
        for term in sorted(group)
    ]

    data["quick_filters"] = []
    for i, (key, pairs) in enumerate(QUICK_FILTERS):
        en, es, d_en, d_es = QUICK_FILTER_LABELS.get(key, (key, key, None, None))
        if key not in QUICK_FILTER_LABELS:
            data["notes"].append(f"{key}: no label in QUICK_FILTER_LABELS, used the key as its label")
        data["quick_filters"].append({
            "key": key, "label_en": en, "label_es": es,
            "description_en": d_en, "description_es": d_es,
            "sort_order": i, "pairs": pairs,
        })

    no_category = sorted({s["name"] for s in data["services"] if not s["categories"]})
    if no_category:
        data["notes"].append(
            f"{len(no_category)} raw services have no category (still searchable): "
            + ", ".join(no_category[:8]) + (" …" if len(no_category) > 8 else "")
        )
    return data


def summarize(data):
    n_links = sum(len(s["specifics"]) for s in data["services"])
    n_cats = sum(len(s["categories"]) for s in data["services"])
    print(f"  categories          {len(data['categories'])}")
    print(f"  specific services   {len(data['specifics'])}")
    print(f"  organizations       {len(data['orgs'])}")
    print(f"  locations           {len(data['locations'])}")
    print(f"  services (raw)      {len(data['services'])}")
    print(f"  service->specific   {n_links}")
    print(f"  service->category   {n_cats}")
    print(f"  synonyms            {len(data['synonyms'])}")
    print(f"  quick filters       {len(data['quick_filters'])}")
    for note in data["notes"]:
        print(f"  NOTE: {note}")


# ── write to Supabase ─────────────────────────────────────────────────────────
def insert(client, table, rows, chunk=500):
    out = []
    for i in range(0, len(rows), chunk):
        out.extend(client.table(table).insert(rows[i:i + chunk]).execute().data)
    return out


def wipe(client):
    # Children before parents. PostgREST refuses an unfiltered delete, so each
    # one carries a filter every row satisfies.
    for table, col, op, val in [
        ("quick_filter_specifics", "specific_service_id", "gte", 0),
        ("quick_filters", "key", "neq", ""),
        ("service_specifics", "service_id", "gte", 0),
        ("service_categories", "service_id", "gte", 0),
        ("services", "id", "gte", 0),
        ("specific_services", "id", "gte", 0),
        ("categories", "id", "gte", 0),
        ("synonyms", "id", "gte", 0),
        ("locations", "id", "gte", 0),
        ("organization_regions", "organization_id", "gte", 0),
        ("organizations", "id", "gte", 0),
        ("regions", "id", "gte", 0),
    ]:
        getattr(client.table(table).delete(), op)(col, val).execute()


def load(client, data):
    wipe(client)

    region = insert(client, "regions", [{"name": REGION[0], "state": REGION[1]}])[0]

    cats = insert(client, "categories", data["categories"])
    cat_id = {c["name_en"]: c["id"] for c in cats}

    specs = insert(client, "specific_services", [
        {k: v for k, v in s.items() if k != "category"} | {"category_id": cat_id[s["category"]]}
        for s in data["specifics"]
    ])
    # The insert result has no category name, so match up by position.
    spec_id = {
        (s["category"], s["name_en"]): row["id"]
        for s, row in zip(data["specifics"], specs)
    }

    orgs = insert(client, "organizations", data["orgs"])
    org_id = {o["name"]: o["id"] for o in orgs}

    insert(client, "organization_regions",
           [{"organization_id": i, "region_id": region["id"]} for i in org_id.values()])

    insert(client, "locations", [
        {k: v for k, v in l.items() if k != "org"}
        | {"organization_id": org_id[l["org"]], "region_id": region["id"]}
        for l in data["locations"]
    ])

    svcs = insert(client, "services", [
        {"organization_id": org_id[s["org"]], "name": s["name"]} for s in data["services"]
    ])
    links, cat_links = [], []
    for s, row in zip(data["services"], svcs):
        for c, sp, source in s["specifics"]:
            links.append({"service_id": row["id"],
                          "specific_service_id": spec_id[(c, sp)],
                          "source": source})
        for c in s["categories"]:
            cat_links.append({"service_id": row["id"], "category_id": cat_id[c]})
    insert(client, "service_specifics", links)
    insert(client, "service_categories", cat_links)

    insert(client, "synonyms", data["synonyms"])

    insert(client, "quick_filters",
           [{k: v for k, v in q.items() if k != "pairs"} for q in data["quick_filters"]])
    insert(client, "quick_filter_specifics", [
        {"quick_filter_key": q["key"], "specific_service_id": spec_id[p]}
        for q in data["quick_filters"] for p in q["pairs"]
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="build all rows and print counts without connecting")
    args = ap.parse_args()

    data = build()
    print("Rows to load:")
    summarize(data)
    if args.dry_run:
        print("\nDry run: nothing was written.")
        return

    from dotenv import load_dotenv
    from supabase import create_client

    load_dotenv(os.path.join(ROOT, ".env"))
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SECRET_KEY in .env first.")

    answer = input(f"\nThis DELETES and reloads the directory tables at {url}. Type 'yes': ")
    if answer.strip().lower() != "yes":
        sys.exit("Cancelled.")

    load(create_client(url, key), data)
    print("Done. Check the Table Editor in Supabase.")


if __name__ == "__main__":
    main()
