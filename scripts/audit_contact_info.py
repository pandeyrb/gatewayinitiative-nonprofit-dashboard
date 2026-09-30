"""
Check every organization's and site's address, ZIP, map pin, phone and hours
against outside sources, and write a review sheet of anything that disagrees.

    .venv/bin/python scripts/audit_contact_info.py

Three checks, each against a source we don't control:
  * Address / ZIP / pin: the US Census geocoder. Flags a ZIP that differs from
    the Census match, and a pin more than 250 m from where the Census puts the
    street address. Floor/suite qualifiers are stripped first; the Census
    matches street ranges, not units.
  * Phone: the org's own website (and the location's SourceURL). Flags a phone
    number whose digits appear nowhere on those pages. Sites that block
    scripted requests are reported as "unreachable", not as wrong.
  * Hours: the HoursSourceURL page. Flags hours whose clock times mostly do
    not appear on the cited page.

Nothing here edits the data. Output: qa/contact_audit.csv, one row per issue.
A flag means "a person should look", not "this is wrong": a phone missing
from a homepage is often just on the contact page.
"""

import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORGS = os.path.join(ROOT, "data", "GWIorgs_v6.csv")
LOCS = os.path.join(ROOT, "data", "org_locations.csv")
OUT = os.path.join(ROOT, "qa", "contact_audit.csv")

UA = {"User-Agent": "Mozilla/5.0 (GWI resource finder data audit)"}
CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

_QUAL = re.compile(
    r",?\s*(?:\([^)]*\)|#\S+|\b(?:suite|ste\.?|unit|apt\.?|rm\.?|room|bldg\.?|"
    r"building|entrance|floor|\d+(?:st|nd|rd|th)\s+floor)\b[^,]*)",
    re.I,
)


def street(addr: str) -> str:
    return re.sub(r"\s*,\s*$", "", _QUAL.sub("", str(addr))).strip(" ,")


def geocode(addr: str, city: str, state: str, zip_: str) -> dict | None:
    one = ", ".join(x for x in (street(addr), city, state, zip_) if x)
    for _ in range(3):
        try:
            r = requests.get(
                CENSUS,
                params={"address": one, "benchmark": "Public_AR_Current", "format": "json"},
                timeout=30,
            )
            m = r.json()["result"]["addressMatches"]
            if not m:
                return None
            c = m[0]
            return {
                "lat": c["coordinates"]["y"],
                "lon": c["coordinates"]["x"],
                "zip": c["addressComponents"]["zip"],
                "matched": c["matchedAddress"],
            }
        except Exception:
            time.sleep(2)
    return None


def meters(a, b) -> float:
    dy = (a[0] - b[0]) * 111_000
    dx = (a[1] - b[1]) * 111_000 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


_page_cache: dict[str, str | None] = {}


def page_text(url: str) -> str | None:
    url = str(url or "").strip()
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    if url in _page_cache:
        return _page_cache[url]
    try:
        r = requests.get(url, headers=UA, timeout=20)
        text = r.text if r.ok else None
    except Exception:
        text = None
    _page_cache[url] = text
    return text


def contact_pages(url: str) -> list[str]:
    """The homepage plus the usual contact/about paths."""
    url = str(url or "").strip().rstrip("/")
    if not url:
        return []
    if not url.startswith("http"):
        url = "https://" + url
    base = re.match(r"https?://[^/]+", url).group(0)
    return [url, base, base + "/contact", base + "/contact-us", base + "/about"]


def digits(s: str) -> str:
    d = re.sub(r"\D", "", str(s))
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def phone_on(pages: list[str], phone: str) -> str:
    want = digits(phone)
    if len(want) != 10:
        return "not a number"
    reached = False
    for u in pages:
        t = page_text(u)
        if t is None:
            continue
        reached = True
        if want in re.sub(r"\D", "", t):
            return "found"
    return "missing" if reached else "unreachable"


_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.?|p\.m\.?)", re.I)


def times(s: str) -> set[str]:
    out = set()
    for h, m, ap in _TIME.findall(str(s)):
        out.add(f"{int(h)}:{m or '00'}{ap[0].lower()}")
    return out


def hours_match(hours: str, url: str) -> tuple[str, str]:
    want = times(hours)
    if not want:
        return "no times", ""
    t = page_text(url)
    if t is None:
        return "unreachable", ""
    have = times(re.sub(r"<[^>]+>", " ", t))
    missing = sorted(want - have)
    if not missing:
        return "found", ""
    return ("partial" if len(missing) < len(want) else "missing"), ", ".join(missing)


def audit_row(kind, name, site, r, website, source) -> list[dict]:
    issues = []

    def add(field, stored, found, note):
        issues.append(
            {"Kind": kind, "Org": name, "Site": site, "Field": field,
             "Stored": stored, "Found": found, "Note": note}
        )

    addr = str(r.get("Address", "")).strip()
    if addr:
        g = geocode(addr, r.get("City", ""), r.get("State", "MA"), r.get("Zip", ""))
        if g is None:
            add("address", addr, "", "Census geocoder found no match for this street address")
        else:
            if str(r.get("Zip", "")).strip() and g["zip"] != str(r["Zip"]).strip():
                add("zip", r["Zip"], g["zip"], f"Census match: {g['matched']}")
            try:
                pin = (float(r["Latitude"]), float(r["Longitude"]))
                d = meters(pin, (g["lat"], g["lon"]))
                if d > 250:
                    add("pin", f"{pin[0]:.5f}, {pin[1]:.5f}",
                        f"{g['lat']:.5f}, {g['lon']:.5f}",
                        f"pin is {d:.0f} m from the Census point for {g['matched']}")
            except (KeyError, TypeError, ValueError):
                pass

    phone = str(r.get("Phone", "")).strip()
    if phone:
        pages = ([source] if source else []) + contact_pages(website)
        res = phone_on(pages, phone)
        if res != "found":
            add("phone", phone, res, "phone digits not seen on the org's site"
                if res == "missing" else "site could not be fetched")

    hours = str(r.get("Hours", "")).strip()
    hsrc = str(r.get("HoursSourceURL", "") or source or "").strip()
    if hours and hsrc:
        res, miss = hours_match(hours, hsrc)
        if res in ("missing", "partial", "unreachable"):
            add("hours", hours, res, f"times not on source page: {miss}" if miss else
                "source page could not be fetched")
    return issues


def main() -> int:
    orgs = pd.read_csv(ORGS, dtype=str).fillna("")
    locs = pd.read_csv(LOCS, dtype=str).fillna("")
    site_of = dict(zip(orgs["Name"], orgs["URL"]))

    jobs = [("org", r["Name"], "", r, r["URL"], "") for _i, r in orgs.iterrows()]
    jobs += [
        ("location", r["OrgName"], r["LocationName"], r,
         site_of.get(r["OrgName"], ""), r.get("SourceURL", ""))
        for _i, r in locs.iterrows()
    ]

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda j: audit_row(*j), jobs))

    rows = [i for lst in results for i in lst]
    pd.DataFrame(rows, columns=["Kind", "Org", "Site", "Field", "Stored", "Found", "Note"]).to_csv(
        OUT, index=False
    )
    print(f"checked {len(jobs)} rows, {len(rows)} flags -> {os.path.relpath(OUT, ROOT)}")
    for f, n in pd.Series([r["Field"] for r in rows]).value_counts().items():
        print(f"  {f}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
