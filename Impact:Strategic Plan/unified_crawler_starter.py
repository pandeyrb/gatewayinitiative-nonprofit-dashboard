"""
Services crawler starter
--------------------------------------------------------------------
For each org in GWIorgs_v5.csv, this script produces ONE row with:
    Services, plus a "Service_Method" field so we always know *how* the
    answer was found.

(Strategic Plan / Impact Report links are out of scope here — that's already
handled by the existing web crawler notebook + manual QA.)


    1. Keyword pass — locate the Services/Programs/Get-Help pages (free, deterministic).
    2. Gemini       — open-ended extraction of specific services from that page text.
    3. Human QA     — not code; that's the next meeting's job.

This file is a SKELETON. Sections marked "TODO(student)" are the parts you
should fill in / tune together.
"""

import concurrent.futures
import os
import time
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
}

CSV_PATH = "GWIorgs_v5.csv"  # run this script from inside "Impact:Strategic Plan/"
OUTPUT_PATH = "org_services.csv"

REQUEST_TIMEOUT = (
    10  # seconds per HTTP request — one slow site shouldn't stall everything
)
ORG_TIME_BUDGET = (
    90  # seconds max to spend on one org (crawling + Gemini) before moving on
)



# ── keyword list (step 1) ───────────────────────────────────────────────────
SERVICE_PAGE_KEYWORDS = [
    "service",
    "program",
    "what-we-do",
    "what we do",
    "get-help",
    "get help",
    "our-work",
    "our work",
    "impact-areas",
    "programs-services",
    "programs",
    "how-we-help",
    "how we help",
    "for-clients",
    "client-services",
    "support",
    "resources-for",
    "initiatives",
    "areas-of-focus",
]


# ── crawling helpers (reused from the existing report-finder notebook) ─────


def get_all_links(url: str) -> list[dict]:
    """Same-domain links + any PDF links found on `url`."""
    if not url.startswith("http"):
        return []
    try:
        page = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if page.status_code != 200:
            return []
        soup = BeautifulSoup(page.text, "html.parser")
        
    except Exception as e:
        print(f"    get_all_links failed on {url}: {type(e).__name__}: {e}")
        return []

    base_domain = urlparse(page.url).netloc
    
    links, seen = [], set()
    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        full_url = urljoin(page.url, href)
        label = a.get_text(" ", strip=True)
        same_site = urlparse(full_url).netloc == base_domain
        is_pdf = full_url.lower().endswith(".pdf")
        if (same_site or is_pdf) and full_url not in seen:
            seen.add(full_url)
            links.append({"url": full_url, "label": label})
    return links


def get_page_text(url: str) -> str:
    """Fetch a page and return its cleaned, lowercased visible text."""
    try:
        page = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if page.status_code != 200:
            return ""
        soup = BeautifulSoup(page.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.extract()
        return soup.get_text(" ")
    except Exception:
        return ""


# ── STEP 1: keyword-LOCATE relevant pages for services (not classify) ──────


def find_service_pages(all_links: list[dict], limit: int = 6) -> list[str]:
    """
    Find candidate Services/Programs/Get-Help pages by keyword, so we know
    WHERE to read for services — this replaces the old fixed 11-category
    scanner, which classified text instead of just finding the right page.
    """
    matches = []
    for link in all_links:
        haystack = (link["url"] + " " + link["label"]).lower()
        if any(kw in haystack for kw in SERVICE_PAGE_KEYWORDS):
            matches.append(link["url"])
    return matches[:limit]


# ── STEP 2: Gemini fallback (only reached when step 1 is inconclusive) ─────

_client = None


def get_gemini_client():
    """
    Lazily create the Gemini client using credentials from a .env file
    instead of a hardcoded personal path.

    TODO(student): create a `.env` file (same folder as this script, not
    committed to git) containing:
        GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/your-key.json
        GEMINI_PROJECT_ID=your-vertex-project-id
    """
    global _client
    if _client is not None:
        return _client
    load_dotenv()
    from google import genai

    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    project_id = os.getenv("GEMINI_PROJECT_ID")
    if not cred_path or not project_id:
        raise RuntimeError(
            "Missing GOOGLE_APPLICATION_CREDENTIALS or GEMINI_PROJECT_ID in .env — "
            "see the TODO above get_gemini_client()."
        )
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path
    _client = genai.Client(vertexai=True, project=project_id, location="us-central1")
    return _client


def ask_gemini_for_services(org_name: str, page_text: str) -> list[str]:
    """
    Open-ended service extraction — NOT limited to a fixed category list.
    Per the "essence" feedback: an org can and should get as many specific
    tags as actually apply, in its own words.

    TODO(student): once you see real output, decide how much post-processing
    is needed (e.g. trimming near-duplicate phrasing) before this becomes
    the canonical vocabulary step in Phase 2 (human QA).
    """
    if not page_text.strip():
        return []

    client = get_gemini_client()
    prompt = f"""Below is text scraped from {org_name}'s website (services/programs pages).

Read it and list every distinct service or program this organization appears to
offer. Be specific rather than broad (e.g. "ESOL classes" rather than just
"Education"). An organization can have many services — list all that you find.

Merge near-duplicates — do not list the same service twice because it is
offered in different formats or described in different words.
Aim for 5-12 tags.

Reply with one service per line, nothing else. No bullets, no numbering.

TEXT:
{page_text[:6000]}"""

    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return [line.strip("-•* ").strip() for line in response.text.splitlines() if line.strip()]


# ── per-org orchestration ───────────────────────────────────────────────────


def process_org(name: str, url: str) -> dict:
    print(f"  {name} ...")
    all_links = get_all_links(url)

    # keyword-locate pages, then Gemini open-ended extraction
    service_pages = find_service_pages(all_links)
    if not service_pages:
        service_pages = [url]  # fall back to homepage text
    combined_text = " ".join(get_page_text(p) for p in service_pages)

    try:
        landed = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT).url if url.startswith("http") else url
    except Exception:
        landed = url  # bookkeeping only — don't lose the row over it

    redirected = urlparse(landed).netloc != urlparse(url).netloc

    services, service_method = [], "none_found"
    if len(combined_text.strip()) < 500:
        service_method = "too_little_text"
    else:
        try:
            services = ask_gemini_for_services(name, combined_text)
            service_method = "redirected_domain" if redirected else "gemini"
        except Exception as e:
            print(f"    Gemini failed: {e}")
            service_method = "gemini_error"

    return {
        "Name": name,
        "Services": ", ".join(services),
        "Service_Method": service_method,
        "Source_URLs": " | ".join(service_pages),
        "Landed_URL": landed,
    }


def main():
    df = pd.read_csv(CSV_PATH, dtype=str).fillna("")
    df = df[df["Name"].str.strip() != ""].reset_index(drop=True)
    df = df.head(5)
    # NOTE (from skeleton): no .head(n) in the final version — the earlier
    # notebook silently ran on 10 orgs while its output CSV claimed 65.
    # Confirm this runs on the FULL df before trusting any coverage numbers.


    rows = []
    for i, row in df.iterrows():
        print(f"[{i + 1}/{len(df)}] {row['Name']} ...", end=" ", flush=True)
        start = time.time()

        # Run this org on its own thread with a hard time budget. If a slow
        # site or a stuck Gemini call blows past ORG_TIME_BUDGET, we give up
        # and move on instead of stalling the whole run on one bad org.
        # NOTE: Python can't force-kill a thread, so the abandoned call keeps
        # running in the background — this just stops it from blocking US.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(process_org, row["Name"], row["URL"])
        try:
            result = future.result(timeout=ORG_TIME_BUDGET)
            print(f"({time.time() - start:.0f}s)")
        except concurrent.futures.TimeoutError:
            print(f"TIMED OUT after {ORG_TIME_BUDGET}s — skipping")
            result = {"Name": row["Name"], "Services": "", "Service_Method": "timeout"}
        except Exception as e:
            print(f"ERROR: {e} — skipping")
            result = {"Name": row["Name"], "Services": "", "Service_Method": "error"}

        rows.append(result)
        # save after every org, not just at the end, so an interrupted or
        # killed run still leaves a usable CSV instead of an ambiguous partial one
        pd.DataFrame(rows).to_csv(OUTPUT_PATH, index=False)

        time.sleep(2)  # be polite to the sites we're hitting

    out = pd.DataFrame(rows).merge(df[["Name", "Services"]].rename(columns={"Services": "Old_Services"}), on="Name", how="left")
    out.to_csv(OUTPUT_PATH, index=False)
    n_services = (out["Services"] != "").sum()
    n_timeout = (out["Service_Method"] == "timeout").sum()
    print(f"\nDone. Services: {n_services}/{len(out)}")
    if n_timeout:
        print(
            f"({n_timeout} orgs timed out after {ORG_TIME_BUDGET}s — worth re-checking those sites by hand)"
        )
    print(
        f"Wrote {OUTPUT_PATH} — next: Phase 2 human QA pass on a sample of these rows."
    )


if __name__ == "__main__":
    main()
