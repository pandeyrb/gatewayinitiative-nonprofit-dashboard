# ── replace MISSION_PAGE_KEYWORDS and find_mission_pages with this ──────────

# Ordered by specificity: a page called /our-mission/ should win a slot over a
# generic /about page. The first run wasted its 4-page budget on things like
# /about-us/administration/superintendent because plain "about" matched first.
import time

import pandas as pd

from unified_crawler_starter import (
    find_service_pages,
    get_all_links,
    get_gemini_client,
    get_page_text,
)

CSV_PATH = "../GWIorgs_v5.csv"
OUTPUT_PATH = "org_missions.csv"

SAMPLE = [
    "Bread and Roses Kitchen",
    "Lazarus House Ministries",
    "Greater Lawrence Family Health",
    "Si se puede!",
    "Merrimack Valley Dream Center",
    "Community Giving Tree",
    "Suenos Basketball",
    "Merrimack River Watershed Council (MRVC)",
    "Greater Lawrence Technical School",
    "St. Mary's Parish",
    "International Institute of Greater Lawrence",
    "Merrimack Valley Food Bank",
]

MISSION_PAGE_KEYWORDS = [
    # tier 1 — explicitly about mission
    ["mission", "our-mission", "our mission", "vision", "who-we-are", "who we are"],
    # tier 2 — general about pages
    ["about-us", "about us", "our-story", "our story", "about"],
]

# Sub-pages that match "about" but never contain a mission statement.
MISSION_EXCLUDE = [
    "administration",
    "superintendent",
    "principal",
    "staff",
    "board",
    "leadership",
    "team",
    "funders",
    "sponsor",
    "partners",
    "equity-statement",
    "financials",
    "history",
    "contact",
    "careers",
    "employment",
]


def find_mission_pages(all_links, limit: int = 4) -> list[str]:
    """
    Locate About/Mission pages, preferring explicitly mission-named pages.

    Two-tier so specific pages fill the limited slots first — otherwise a site
    with a deep /about/ nav tree crowds out the one page we actually want.
    """
    matches = []
    seen = set()

    for tier in MISSION_PAGE_KEYWORDS:
        for link in all_links:
            url_lower = link["url"].lower()
            if any(bad in url_lower for bad in MISSION_EXCLUDE):
                continue
            haystack = url_lower + " " + link["label"].lower()
            if any(kw in haystack for kw in tier) and link["url"] not in seen:
                seen.add(link["url"])
                matches.append(link["url"])
        if len(matches) >= limit:
            break

    return matches[:limit]


def ask_gemini_for_mission(org_name: str, page_text: str) -> str:
    """
    Verbatim extraction, not summarization. If the model is allowed to
    paraphrase, a real mission and an invented one look identical — which is
    the failure mode the services QA found on Si se puede!.
    """
    if not page_text.strip():
        return ""

    client = get_gemini_client()
    prompt = f"""Below is text from {org_name}'s website (About / Mission pages).

Find this organization's mission statement.

Reply with the mission statement EXACTLY as written on the site — do not
summarize, rephrase, or improve it.

If the site does not state a mission, reply with exactly: NONE FOUND

TEXT:
{page_text[:6000]}"""

    response = client.models.generate_content(
        model="gemini-2.5-flash", contents=prompt
    )
    return response.text.strip()


def main():
    df = pd.read_csv(CSV_PATH, dtype=str).fillna("")
    df = df[df["Name"].isin(SAMPLE)].reset_index(drop=True)

    missing = set(SAMPLE) - set(df["Name"])
    if missing:
        print("WARNING — these names didn't match the CSV exactly:")
        for m in missing:
            print(f"   {m}")
        print()

    rows = []
    for i, row in df.iterrows():
        name, url = row["Name"], row["URL"]
        print(f"[{i + 1}/{len(df)}] {name} ...", end=" ", flush=True)

        all_links, landed = get_all_links(url)
        pages = find_mission_pages(all_links)
        if not pages:
            pages = find_service_pages(all_links) or [url]

        text = " ".join(get_page_text(p) for p in pages)

        if len(text.strip()) < 500:
            mission, method = "", "too_little_text"
        else:
            try:
                mission = ask_gemini_for_mission(name, text)
                if mission.upper().startswith("NONE FOUND"):
                    mission, method = "", "none_found"
                else:
                    method = "found"
            except Exception as e:
                print(f"Gemini failed: {e}")
                mission, method = "", "gemini_error"

        print(f"({method})")
        rows.append({
            "Name": name,
            "Mission": mission,
            "Mission_Method": method,
            "Mission_Length": len(mission),
            "Source_URLs": " | ".join(pages),
            "Old_ServiceArea": row.get("ServiceArea", ""),
        })
        pd.DataFrame(rows).to_csv(OUTPUT_PATH, index=False)
        time.sleep(2)

    out = pd.DataFrame(rows)
    found = (out["Mission_Method"] == "found").sum()
    print(f"\nMissions found: {found}/{len(out)}")
    print(out["Mission_Method"].value_counts().to_string())
    print(f"\nWrote {OUTPUT_PATH}")
    print("\nNEXT: read the missions before writing any categorization code.")
    print("Ask of each one: could you assign a category from this alone?")


if __name__ == "__main__":
    main()