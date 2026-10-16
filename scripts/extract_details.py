"""
Step B2: read each org's website and draft resident-facing details.

For each org, it fetches the homepage plus up to 5 pages that look like
services / programs / eligibility / contact, and asks Gemini to fill in:

    description      2-3 plain sentences: what a resident can get there
    who_its_for      who qualifies
    cost             free, sliding scale, fees
    languages        languages spoken, only if the site says so
    how_to_get_help  walk in, call, appointment, referral
    new_services     (only orgs with no services yet) picks from your
                     167 specific services, so new orgs show under categories

Every answer comes with the quote it's based on, and "not stated" when the
site doesn't say. Quotes that can't be found in the page text are flagged.
Nothing goes to Supabase until you approve it (scripts/upload_details.py).

Run from the repo root:
    python scripts/extract_details.py --org "ACT Lawrence"   # try one first
    python scripts/extract_details.py --only-new             # orgs just added
    python scripts/extract_details.py --only-missing         # orgs with no description yet
    python scripts/extract_details.py                        # every org with a website

Results go to review/details_review.csv. It's resumable: orgs already drafted
are skipped, so a crash or Ctrl+C loses nothing; orgs that failed (no
website, site down) are retried on the next run.
"""

import argparse
import concurrent.futures
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import REVIEW_DIR, ROOT, fetch_all, gemini, supabase  # noqa: E402

OUT = os.path.join(REVIEW_DIR, "details_review.csv")
FIELDS = ["description", "who_its_for", "cost", "languages", "how_to_get_help"]
COLUMNS = (["approve", "org", "website"] + FIELDS + ["new_services", "flags"]
           + [f"{f}_quote" for f in FIELDS] + ["services_quotes", "pages_read", "error"])

HEADERS = {"User-Agent": "Mozilla/5.0 (GatewayInitiative resource directory; contact via website)"}
PAGE_WORDS = ["service", "program", "help", "assist", "resource", "eligib", "qualif",
              "food", "pantry", "housing", "shelter", "clinic", "health", "youth",
              "family", "families", "adult", "senior", "class", "get-help", "contact",
              "about", "what-we-do", "how", "apply", "hours", "location", "espanol", "spanish"]
SKIP_WORDS = ["donate", "donation", "give", "volunteer", "event", "gala", "news", "blog",
              "career", "job-opening", "board", "staff", "privacy", "login", "cart", "shop",
              "press", "annual-report", "sponsor", "facebook", "instagram", "twitter", "linkedin"]
MAX_PAGES = 6
MAX_CHARS_PER_PAGE = 6000
MAX_CHARS_TOTAL = 24000


# ── reading the website ───────────────────────────────────────────────────────
def get(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.ok and "html" in r.headers.get("content-type", ""):
            return r
    except requests.RequestException:
        pass
    return None


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:MAX_CHARS_PER_PAGE]


def pick_pages(base: str, html: str) -> list[str]:
    """Same-site links that look like services/eligibility/contact pages."""
    host = urlparse(base).netloc.replace("www.", "")
    scored = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(base, a["href"]).split("#")[0].rstrip("/")
        if urlparse(url).netloc.replace("www.", "") != host or url == base.rstrip("/"):
            continue
        if re.search(r"\.(pdf|jpg|jpeg|png|gif|docx?|xlsx?|zip)$", url, re.I):
            continue
        hay = (url + " " + a.get_text(" ")).lower()
        if any(w in hay for w in SKIP_WORDS):
            continue
        score = sum(w in hay for w in PAGE_WORDS)
        if score:
            scored[url] = max(score, scored.get(url, 0))
    return [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])][: MAX_PAGES - 1]


def read_site(url: str) -> tuple[str, list[str]]:
    """(combined text, urls read)."""
    home = get(url)
    if home is None:
        return "", []
    texts, read = [f"PAGE: {home.url}\n{page_text(home.text)}"], [home.url]
    for link in pick_pages(home.url, home.text):
        r = get(link)
        if r is not None:
            texts.append(f"PAGE: {r.url}\n{page_text(r.text)}")
            read.append(r.url)
        time.sleep(0.5)  # be polite to small nonprofit websites
    return "\n\n".join(texts)[:MAX_CHARS_TOTAL], read


# ── asking Gemini ─────────────────────────────────────────────────────────────
PROMPT = """You are filling in a free community resource directory for residents of
Lawrence, Massachusetts. Below is text from the website of {org}.

Fill in each field for a resident deciding whether to go there. Rules:
- Use ONLY the website text below. If it doesn't say, the value is exactly: not stated
- Never guess eligibility, cost or languages. A wrong "free" or "no ID needed"
  sends someone on a wasted trip.
- description: 2-3 plain sentences on what a resident can actually get there
  (concrete services, not mission statements or slogans).
- who_its_for: who qualifies (age, income, residence, situation).
- cost: free / sliding scale / fees, only as stated.
- languages: only languages the site says services are offered in.
- how_to_get_help: walk in, call, apply online, appointment, referral; include
  hours only if clearly stated.
- For every field, quote: the exact sentence or phrase (under 30 words) from
  the text that supports it. Empty if "not stated".
{services_rule}

WEBSITE TEXT
{text}"""

SERVICES_RULE = """- services: which of these specific services the org directly provides,
  chosen ONLY from this list (copy category and specific exactly), each with a
  supporting quote. Skip anything not clearly offered.
{allowed}"""

NO_SERVICES_RULE = "- services: return an empty list."


def ask(client, model, thinking, org: str, text: str, allowed: list[tuple[str, str]] | None):
    from google.genai import types
    from pydantic import BaseModel

    class Field(BaseModel):
        value: str
        quote: str

    class Service(BaseModel):
        category: str
        specific: str
        quote: str

    class Details(BaseModel):
        description: Field
        who_its_for: Field
        cost: Field
        languages: Field
        how_to_get_help: Field
        services: list[Service]

    rule = (SERVICES_RULE.format(allowed="\n".join(f"  {c} > {s}" for c, s in allowed))
            if allowed else NO_SERVICES_RULE)
    prompt = PROMPT.format(org=org, text=text, services_rule=rule)
    config = types.GenerateContentConfig(
        temperature=0, response_mime_type="application/json",
        response_schema=Details, thinking_config=thinking,
    )
    for attempt in range(5):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            return resp.parsed or Details.model_validate_json(resp.text)
        except Exception as exc:
            if "429" in str(exc) and attempt < 4:
                time.sleep(2 ** attempt * 5)
                continue
            raise


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def to_row(org: str, url: str, text: str, read: list[str], d, allowed_set) -> dict:
    row = {"approve": "", "org": org, "website": url, "pages_read": " ".join(read), "error": ""}
    flags = []
    haystack = _norm(text)
    for f in FIELDS:
        fld = getattr(d, f)
        row[f] = fld.value.strip()
        row[f"{f}_quote"] = fld.quote.strip()
        if row[f].lower() != "not stated" and row[f"{f}_quote"] and _norm(row[f"{f}_quote"]) not in haystack:
            flags.append(f"{f}: quote not found on site")
    picks, quotes = [], []
    for s in d.services:
        pair = (s.category.strip(), s.specific.strip())
        if pair in allowed_set:
            picks.append(f"{pair[0]} > {pair[1]}")
            quotes.append(f"{pair[1]}: {s.quote.strip()}")
    row["new_services"] = "; ".join(dict.fromkeys(picks))
    row["services_quotes"] = " | ".join(quotes)
    row["flags"] = "; ".join(flags)
    # Every stated field was found word-for-word on the site, and there's a
    # real description: safe to publish without a person. Anything flagged
    # waits for review.
    if not flags and row["description"] and row["description"].lower() != "not stated":
        row["approve"] = "auto"
    return row


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", help="only this org (exact name)")
    ap.add_argument("--only-new", action="store_true", help="only orgs with no services yet")
    ap.add_argument("--only-missing", action="store_true", help="only orgs with no description")
    ap.add_argument("--limit", type=int, help="stop after this many orgs")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    sb = supabase()
    orgs = fetch_all(sb, "organizations")
    with_services = {s["organization_id"] for s in fetch_all(sb, "services")}
    cats = {c["id"]: c["name_en"] for c in fetch_all(sb, "categories", "sort_order")}
    allowed = [(cats[s["category_id"]], s["name_en"])
               for s in fetch_all(sb, "specific_services", "sort_order")]
    allowed_set = set(allowed)

    # Orgs already drafted are skipped. Orgs that errored last time are tried
    # again (e.g. after you fix their website in Supabase).
    done, sheet = set(), None
    if os.path.exists(OUT):
        sheet = pd.read_csv(OUT, dtype=str).fillna("")
        done = set(sheet.loc[sheet["error"] == "", "org"])

    todo = []
    for o in orgs:
        if args.org and o["name"] != args.org:
            continue
        if args.only_new and o["id"] in with_services:
            continue
        if args.only_missing and (o.get("description") or "").strip():
            continue
        if o["name"] in done:
            continue
        todo.append(o)
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        sys.exit("Nothing to do (already in the review sheet, or no org matched).")

    if sheet is not None:
        retry = {o["name"] for o in todo}
        sheet[~(sheet["org"].isin(retry) & (sheet["error"] != ""))].to_csv(OUT, index=False)

    client, model, thinking = gemini()
    print(f"Reading {len(todo)} websites with {model}…")
    os.makedirs(REVIEW_DIR, exist_ok=True)

    def work(o):
        url = (o.get("url") or "").strip()
        base = {c: "" for c in COLUMNS} | {"org": o["name"], "website": url}
        if not url:
            return base | {"error": "no website"}
        if not url.startswith("http"):
            url = "https://" + url
        text, read = read_site(url)
        if len(text) < 200:
            return base | {"error": "couldn't read the website", "pages_read": " ".join(read)}
        try:
            d = ask(client, model, thinking, o["name"], text,
                    allowed if o["id"] not in with_services else None)
        except Exception as exc:
            return base | {"error": f"Gemini: {type(exc).__name__}: {str(exc)[:150]}"}
        return to_row(o["name"], url, text, read, d, allowed_set)

    n_ok = n_err = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(work, todo):
            pd.DataFrame([row], columns=COLUMNS).to_csv(
                OUT, mode="a", header=not os.path.exists(OUT), index=False)
            if row["error"]:
                n_err += 1
                print(f"  ✗ {row['org']}: {row['error']}")
            else:
                n_ok += 1
                flag = f"  ({row['flags']})" if row["flags"] else ""
                print(f"  ✓ {row['org']}{flag}")

    print(f"\nDone: {n_ok} drafted, {n_err} need a look. Review {os.path.relpath(OUT, ROOT)}:")
    print('  put "yes" in approve for rows that are right (edit any cell first if needed),')
    print("  then run: python scripts/upload_details.py")


if __name__ == "__main__":
    main()
