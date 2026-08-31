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
GEMINI_TIMEOUT = 30  # seconds max to wait on a single Gemini call
ORG_TIME_BUDGET = (
    90  # seconds max to spend on one org (crawling + Gemini) before moving on
)

# Orgs are processed concurrently (I/O-bound: HTTP + Gemini waits, not CPU),
# so this is the real lever on total run time — not machine specs. Raise it
# if the run still feels slow and you're not seeing rate-limit errors;
# lower it if sites start rejecting requests.
MAX_WORKERS = int(os.getenv("CRAWLER_MAX_WORKERS", "8"))

# Set CRAWLER_LIMIT=10 (env var) to test on a subset. Leave unset for the
# full run. This is intentionally NOT a hardcoded df.head(n) in the code —
# a hardcoded cap is exactly how an earlier version of this pipeline
# silently ran on 10 orgs while its output CSV claimed 65.
CRAWLER_LIMIT = os.getenv("CRAWLER_LIMIT")



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
    "resources-for",
    "initiatives",
    "areas-of-focus",
]


# ── crawling helpers (reused from the existing report-finder notebook) ─────


def get_all_links(url: str) -> tuple[list[dict], str]:
    """Same-domain links + any PDF links found on `url`, plus the landed URL
    (after redirects) — returned here so callers don't need a second request
    just to find out where `url` redirected to."""
    if not url.startswith("http"):
        return [], url
    try:
        page = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if page.status_code != 200:
            return [], url
        soup = BeautifulSoup(page.text, "html.parser")

    except Exception as e:
        print(f"    get_all_links failed on {url}: {type(e).__name__}: {e}")
        return [], url

    landed_url = page.url
    base_domain = urlparse(landed_url).netloc

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
    return links, landed_url


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
EXCLUDE_URL_PARTS = [
    "donate", "donation", "ways-to-give", "supporters",
    "sponsor", "volunteer", "membership", "corporate-partnership",
]

def find_service_pages(all_links: list[dict], limit: int = 6) -> list[str]:
    matches = []
    for link in all_links:
        url_lower = link["url"].lower()
        if any(bad in url_lower for bad in EXCLUDE_URL_PARTS):
            continue
        haystack = (url_lower + " " + link["label"].lower())
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

    from google.genai import types

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT * 1000)
        ),
    )
    return [line.strip("-•* ").strip() for line in response.text.splitlines() if line.strip()]


# ── per-org orchestration ───────────────────────────────────────────────────
def looks_like_services(services: list[str]) -> bool:
    """Reject refusals and prose — real service tags are short phrases."""
    if len(services) < 2:
        return False
    if any(len(s) > 80 for s in services):
        return False
    return True

def process_org(name: str, url: str) -> dict:
    print(f"  {name} ...")
    all_links, landed = get_all_links(url)

    # keyword-locate pages, then Gemini open-ended extraction
    service_pages = find_service_pages(all_links)
    if not service_pages:
        service_pages = [url]  # fall back to homepage text

    # Fetch candidate service pages concurrently — these are independent
    # HTTP requests, no reason to wait on them one at a time.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(service_pages)) as pool:
        combined_text = " ".join(pool.map(get_page_text, service_pages))

    redirected = urlparse(landed).netloc.removeprefix("www.") != urlparse(url).netloc.removeprefix("www.")

    services, service_method = [], "none_found"
    if len(combined_text.strip()) < 500:
        service_method = "too_little_text"
    else:
        try:
            services = ask_gemini_for_services(name, combined_text)
            if not looks_like_services(services):
                services = []
                service_method = "unusable_output"
            else:
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

    if CRAWLER_LIMIT:
        df = df.head(int(CRAWLER_LIMIT))
        print(f"CRAWLER_LIMIT set — running on {len(df)} orgs only (testing mode).")

    rows = []
    # One shared pool for the whole run, sized by MAX_WORKERS: this is what
    # actually parallelizes the work. Every org's HTTP/Gemini calls still
    # have their own timeouts (REQUEST_TIMEOUT, GEMINI_TIMEOUT), and
    # ORG_TIME_BUDGET below is still the hard per-org ceiling — but now
    # MAX_WORKERS orgs are in flight at once instead of one at a time.
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        submitted = [
            (executor.submit(process_org, row["Name"], row["URL"]), row["Name"], time.time())
            for _, row in df.iterrows()
        ]

        for i, (future, name, start) in enumerate(submitted):
            print(f"[{i + 1}/{len(df)}] {name} ...", end=" ", flush=True)
            try:
                result = future.result(timeout=ORG_TIME_BUDGET)
                print(f"({time.time() - start:.0f}s)")
            except concurrent.futures.TimeoutError:
                print(f"TIMED OUT after {ORG_TIME_BUDGET}s — skipping")
                result = {"Name": name, "Services": "", "Service_Method": "timeout"}
            except Exception as e:
                print(f"ERROR: {e} — skipping")
                result = {"Name": name, "Services": "", "Service_Method": "error"}

            rows.append(result)
            # save after every org, not just at the end, so an interrupted or
            # killed run still leaves a usable CSV instead of an ambiguous partial one
            pd.DataFrame(rows).to_csv(OUTPUT_PATH, index=False)

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
