"""
Where the dashboard's data comes from: Supabase, or the old CSVs.

Both paths return the same three things, so app.py doesn't care which ran:

  df         one row per organization, the same columns load_data() built
  locations  {org name: [site dict, ...]} for orgs with more than one site,
             same keys as org_locations.csv
  tax        the taxonomy: category order, Spanish labels, specific services,
             quick filters, search synonyms

Switch with DATA_SOURCE in .streamlit/secrets.toml ("supabase" or "csv").
The CSV path stays only as a fallback during the switch-over; once Supabase is
trusted, it can go.
"""

import re
from dataclasses import dataclass, field

import pandas as pd
import streamlit as st

import search_utils
import service_taxonomy as taxo

# Captured at import, before app.py swaps in the database's synonyms, so the
# CSV path always has the original list.
_CODE_ALIAS_GROUPS = [set(g) for g in search_utils.ALIAS_GROUPS]


@dataclass
class Taxonomy:
    category_order: list[str]
    category_es: dict[str, str]
    specific_order: dict[str, list[str]]
    specific_es: dict[tuple[str, str], str]
    quick_filters: list[tuple[str, list[tuple[str, str]]]]
    alias_groups: list[set[str]]
    # key -> (label_en, label_es, description_en, description_es).
    # Empty on the CSV path, where app.py's T dict supplies the text.
    quick_labels: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)


def _smart_split(s: str) -> list[str]:
    """Same as app.py's _smart_split."""
    if not s:
        return []
    parts = [p.strip() for p in re.split(r",(?![^(]*\))", s) if p.strip()]
    parts = [re.sub(r"^and\s+", "", p, flags=re.IGNORECASE) for p in parts]
    return [p for p in parts if p]


def _s(v) -> str:
    """NULL -> "", everything else -> str, matching the CSVs read with fillna("")."""
    return "" if v is None else str(v)


def _zip(v) -> str:
    z = _s(v).strip()
    return z.zfill(5) if z.isdigit() and len(z) < 5 else z


# ── CSV path (the old way) ────────────────────────────────────────────────────
def taxonomy_from_code() -> Taxonomy:
    return Taxonomy(
        category_order=list(taxo.CATEGORY_ORDER),
        category_es=dict(taxo.CATEGORY_ES),
        specific_order={c: list(v) for c, v in taxo.SPECIFIC_ORDER.items()},
        specific_es=dict(taxo.SPECIFIC_ES),
        quick_filters=list(taxo.QUICK_FILTERS),
        alias_groups=[set(g) for g in _CODE_ALIAS_GROUPS],
    )


# ── Supabase path ─────────────────────────────────────────────────────────────
@st.cache_resource
def _client():
    from supabase import create_client

    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


def _fetch(client, table: str, order: str, page: int = 1000) -> list[dict]:
    """Every row of a table. The API caps one response at 1000 rows, so page
    through with a stable order; small today, but services will pass 1000."""
    rows, start = [], 0
    while True:
        batch = (
            client.table(table).select("*").order(order)
            .range(start, start + page - 1).execute().data
        )
        rows.extend(batch)
        if len(batch) < page:
            return rows
        start += page


@st.cache_data(ttl=600, show_spinner="Loading organizations…")
def load_from_supabase() -> tuple[pd.DataFrame, dict[str, list[dict]], Taxonomy]:
    """Ten-minute cache: edits made in Supabase show up within ten minutes."""
    c = _client()

    cats = _fetch(c, "categories", "sort_order")
    specs = _fetch(c, "specific_services", "sort_order")
    orgs = _fetch(c, "organizations", "id")
    locs = _fetch(c, "locations", "id")
    svcs = _fetch(c, "services", "id")
    svc_specs = _fetch(c, "service_specifics", "service_id")
    svc_cats = _fetch(c, "service_categories", "service_id")
    syns = _fetch(c, "synonyms", "id")
    quicks = _fetch(c, "quick_filters", "sort_order")
    quick_specs = _fetch(c, "quick_filter_specifics", "quick_filter_key")

    # ── taxonomy ──
    cat_name = {r["id"]: r["name_en"] for r in cats}
    cat_rank = {r["name_en"]: r["sort_order"] for r in cats}
    spec_pair = {r["id"]: (cat_name[r["category_id"]], r["name_en"]) for r in specs}

    specific_order: dict[str, list[str]] = {}
    for r in specs:  # already in sort_order
        specific_order.setdefault(cat_name[r["category_id"]], []).append(r["name_en"])

    groups: dict[str, set[str]] = {}
    for r in syns:
        groups.setdefault(r["group_name"], set()).add(r["term"].strip().lower())

    spec_rank = {r["id"]: (cat_rank[cat_name[r["category_id"]]], r["sort_order"]) for r in specs}
    pairs_by_key: dict[str, list[tuple[str, str]]] = {}
    for r in sorted(quick_specs, key=lambda r: spec_rank[r["specific_service_id"]]):
        pairs_by_key.setdefault(r["quick_filter_key"], []).append(
            spec_pair[r["specific_service_id"]]
        )

    tax = Taxonomy(
        category_order=[r["name_en"] for r in cats],
        category_es={r["name_en"]: r["name_es"] for r in cats},
        specific_order=specific_order,
        specific_es={spec_pair[r["id"]]: r["name_es"] for r in specs},
        quick_filters=[(q["key"], pairs_by_key.get(q["key"], [])) for q in quicks],
        alias_groups=list(groups.values()),
        quick_labels={
            q["key"]: (q["label_en"], q["label_es"],
                       _s(q["description_en"]), _s(q["description_es"]))
            for q in quicks
        },
    )

    # ── per-organization lookups ──
    org_name = {o["id"]: o["name"] for o in orgs}  # inactive orgs are already hidden

    svc_org = {s["id"]: s["organization_id"] for s in svcs}
    raw_by_org: dict[int, list[str]] = {}
    for s in svcs:  # id order = the order the CSV listed them
        raw_by_org.setdefault(s["organization_id"], []).append(s["name"])

    specifics_by_org: dict[int, set[tuple[str, str]]] = {}
    for r in svc_specs:
        oid = svc_org.get(r["service_id"])
        if oid is not None:
            specifics_by_org.setdefault(oid, set()).add(spec_pair[r["specific_service_id"]])

    cats_by_org: dict[int, set[str]] = {}
    for r in svc_cats:
        oid = svc_org.get(r["service_id"])
        if oid is not None:
            cats_by_org.setdefault(oid, set()).add(cat_name[r["category_id"]])

    locs_by_org: dict[int, list[dict]] = {}
    for l in locs:
        if l["organization_id"] in org_name:
            locs_by_org.setdefault(l["organization_id"], []).append(l)

    # ── the per-org table ──
    rows = []
    for o in orgs:
        site_list = locs_by_org.get(o["id"], [])
        main = next((l for l in site_list if l["is_main"]), site_list[0] if site_list else {})
        raw = raw_by_org.get(o["id"], [])
        svc_area = _smart_split(_s(o["service_area"]))
        rows.append({
            "Name": o["name"],
            "Address": _s(main.get("address")),
            "City": _s(main.get("city")),
            "State": _s(main.get("state")),
            "Zip": _zip(main.get("zip")),
            "URL": _s(o["url"]),
            "OrgType": _s(o["org_type"]),
            "ServiceArea": _s(o["service_area"]),
            "Latitude": main.get("latitude"),
            "Longitude": main.get("longitude"),
            "StrategicPlan": _s(o["strategic_plan_url"]),
            "ImpactReport": _s(o["impact_report_url"]),
            "Services": ", ".join(raw),
            "Phone": _s(o["phone"]),
            # Plain-language summary (scripts/draft_descriptions.py drafts it,
            # a person approves it). Searched and shown on the org's card.
            "Description": _s(o.get("description")),
            "DescriptionEs": _s(o.get("description_es")),
            # Resident-facing details (db/005), drafted from the org's website
            # by scripts/extract_details.py. Empty until approved.
            "WhoItsFor": _s(o.get("who_its_for")),
            "Cost": _s(o.get("cost")),
            "Languages": _s(o.get("languages")),
            "HowToGetHelp": _s(o.get("how_to_get_help")),
            # The org's own hours summary first; a site's hours only if it has none.
            "Hours": _s(o.get("hours_text") or main.get("hours_text")),
            "HoursSourceURL": _s(o.get("hours_source_url") or main.get("source_url")),
            "HoursVerifiedOn": _s(o.get("hours_verified_on") or main.get("verified_on")),
            "SvcList": svc_area,
            "SvcTagList": raw,
            # Canonical tags are still computed in code; they only feed search.
            "SvcCanonical": taxo.canonical_tags(raw or svc_area),
            "Categories": sorted(cats_by_org.get(o["id"], set()), key=cat_rank.get),
            "Specifics": specifics_by_org.get(o["id"], set()),
        })
    df = pd.DataFrame(rows)
    df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")

    # ── extra sites, shaped like org_locations.csv ──
    # Only orgs with more than one site, same as when the CSV was the source.
    locations: dict[str, list[dict]] = {}
    for oid, site_list in locs_by_org.items():
        if len(site_list) < 2:
            continue
        name = org_name[oid]
        locations[name] = [
            {
                "OrgName": name,
                "LocationName": _s(l["name"]),
                "Address": _s(l["address"]),
                "City": _s(l["city"]),
                "State": _s(l["state"]),
                "Zip": _zip(l["zip"]),
                "Phone": _s(l["phone"]),
                "Hours": _s(l["hours_text"]),
                "ServicesHere": _s(l["services_here"]),
                "Kind": _s(l["kind"]),
                "SourceURL": _s(l["source_url"]),
                "VerifiedOn": _s(l["verified_on"]),
                "Latitude": _s(l["latitude"]),
                "Longitude": _s(l["longitude"]),
                "GeoSource": _s(l["geo_source"]),
            }
            for l in site_list
        ]

    return df, locations, tax
