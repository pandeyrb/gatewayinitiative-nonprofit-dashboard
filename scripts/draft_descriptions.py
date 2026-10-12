"""
Draft a specific, plain-language summary for every organization, for a person
to review.

Run from the repo root:
    python scripts/draft_descriptions.py                 # every org without a description
    python scripts/draft_descriptions.py --all           # redo every org
    python scripts/draft_descriptions.py --only "YWCA" --only "EforAll"
    python scripts/draft_descriptions.py --flagged       # redo rows that aren't ok yet
    CRAWLER_LIMIT=5 python scripts/draft_descriptions.py # try it on five first

Writes qa/descriptions_review.csv. Nothing is written to Supabase: a reviewer
checks each draft against its source pages, marks Approved = yes, and
scripts/load_descriptions.py loads the approved rows. Reruns merge into the
sheet and never overwrite an approved row or a reviewer's notes.

Per org:
  1. Crawl  the home page, its About / Mission / Who-we-are pages, and every
            program page, following program index pages one level down
            (/programs/ -> /programs/food-pantry/). Menus, footers and repeated
            slider text are stripped so the budget goes to real content.
  2. Draft  Gemini writes the summary from that text only, in English and
            Spanish: the programs by name, who they're for, what you actually
            get, cost and languages. It also lists the programs, quotes two
            sentences it relied on, and reports hours and phone if stated.
  3. Flag   rows a reviewer should look at first: no website, pages that
            wouldn't load, little text, a quote that isn't on the site, a
            number or program name the site never mentions (listed in
            Unverified), or summaries that still read as generic.

The first version of this script read only 3 program pages and spent most of
its text budget on navigation menus, which is why its summaries were vague.
"""

import argparse
import concurrent.futures
import csv
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)  # st.secrets looks for .streamlit/ in the working directory

from data_source import load_from_supabase  # noqa: E402
from unified_crawler_starter import get_gemini_client  # noqa: E402

OUTPUT = os.path.join(ROOT, "qa", "descriptions_review.csv")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_WORKERS = int(os.getenv("CRAWLER_MAX_WORKERS", "6"))
CRAWLER_LIMIT = os.getenv("CRAWLER_LIMIT")
MAX_PAGES = 12       # per org
PAGE_CHARS = 6000    # per page sent to Gemini
TOTAL_CHARS = 50000  # per org
MIN_TEXT = 600       # below this the draft is flagged as thin
TIMEOUT = 12

# Several sites' firewalls (YWCA, YMCA, Jeanne Geiger) reject a request that
# carries only a User-Agent; a normal browser's headers get the public pages.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}

# Which links are worth reading, best first. Matched against the link's URL
# path and its label.
PAGE_KINDS = [
    ("programs", 5, ["program", "service", "what-we-do", "what we do", "get-help", "get help",
                     "how-we-help", "how we help", "our-work", "our work", "resources",
                     "find-help", "find help", "clinic", "classes", "pantry", "shelter",
                     "eligib", "apply", "enroll", "admission", "servicios", "programas"]),
    ("about", 4, ["about", "mission", "who-we-are", "who we are", "our-story", "our story",
                  "vision", "what-we-believe", "quienes", "nosotros", "mision"]),
    ("practical", 2, ["faq", "hours", "contact", "visit", "location", "horario"]),
]
# Pages that never describe services. Whole URL path segments only, so
# "event" doesn't skip /prevention and "give" doesn't skip /caregivers.
SKIP = re.compile(
    r"(^|[/_-])(donate|donations?|give|giving|ways-to-give|volunteer\w*|careers?|"
    r"jobs-at\w*|events?|news|blog|press|shop|store|cart|login|sign-in|staff|board|"
    r"leadership|team|financials?|annual-reports?|privacy\w*|terms|sponsors?|"
    r"partners?|gala|wish-?list|merch\w*|newsletters?|subscribe|calendar|gallery|"
    r"photos?|videos?|podcasts?|tag|category|author|feed|wp-content|wp-json)"
    r"($|[/_.-])")
JUNK_FILES = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".doc", ".docx",
              ".xls", ".xlsx", ".zip", ".mp4", ".mp3")
# Page furniture that isn't about the org: removed before reading the text.
CHROME_TAGS = ["script", "style", "noscript", "svg", "iframe", "nav", "header",
               "footer", "aside", "form", "button"]
CHROME_HINT = re.compile(
    r"(^|[-_ ])(nav|navbar|menu|footer|header|cookie|breadcrumb|sidebar|popup|modal|"
    r"newsletter|social|share|skip|banner-top|topbar|utility)([-_ ]|$)", re.I)

# Phrases that make a summary say nothing. A draft using them is flagged.
GENERIC = re.compile(
    r"\b(various|variety of|a range of|wide range|range of services|array of|"
    r"high[- ]quality|empower\w*|holistic|comprehensive|resources and support|"
    r"support services|community members|individuals and families in need|"
    r"dedicated to|committed to|strives? to|works? to improve|"
    r"programs and services|services and programs)\b", re.I)

PROMPT = """You are writing the entry for {name} in a free directory that helps
residents of Lawrence, Massachusetts find local help. Readers are often in a
hurry, often not native English speakers. They need to know exactly what they
can get here, and whether it's for them.

Below is text from {name}'s own website (several pages, each starting with
its URL). Use ONLY this text. Never add anything it doesn't say.

summary_en: 3 or 4 sentences, 50 to 90 words.
  - Name the actual programs or services the way the site names them
    ("Emergency Food Pantry", "Head Start", "ESOL classes"), starting with the
    ones a resident would come for. Not categories, not "various services".
  - Say who each is for when the site says: ages, families, women, veterans,
    income limits, residents of which towns.
  - Say what people actually receive (groceries, a bed for the night, a
    lawyer, free tax filing, a certificate), with numbers if the site gives
    them (e.g. "6-week course", "ages 5-18").
  - Include cost (free, sliding scale, fee) and languages (e.g. Spanish
    spoken) only if the site states them.
  - Plain everyday words. No praise, no mission statement, no history, no
    "we", no "dedicated to", "empower", "holistic", "comprehensive",
    "various", "a range of", "high-quality".
  If the text doesn't say what the organization offers, return "".
summary_es: the same summary in natural Latin American Spanish, keeping
  program names as the site writes them.
programs: the organization's programs or services, as named on the site,
  each with 3 to 10 words on what it is. At most 12.
who_it_serves: who the organization serves, in a short phrase, or "".
cost: "free", "sliding scale", a fee, etc., only if stated. Otherwise "".
languages: languages services are offered in, only if stated. Otherwise "".
how_to_start: one short phrase on how to get help, only if the text says
  (e.g. "Walk in Mon-Fri, no appointment", "Call to apply"). Otherwise "".
evidence: two sentences copied word for word from the text that best support
  the summary, separated by " || ". Otherwise "".
hours: the hours the public can visit or get services, copied as written, only
  if stated. Otherwise "".
phone: the main public phone number, only if stated. Otherwise "".

WEBSITE TEXT
{text}"""

# Used only when the website gave nothing to work from (menus only, or the site
# is down). The service list was already checked by the team, so it's a safe
# source, but thinner: the row is flagged from_service_list for the reviewer.
SERVICES_PROMPT = """You are writing the entry for {name} in a free directory that helps
residents of Lawrence, Massachusetts find local help. Write plainly.

{name} is a {org_type}. Its services, as checked by our team:
{services}

summary_en: 2 or 3 sentences, at most 60 words, naming what someone can get
  there, most useful first. Everyday words, no praise, no "we", no "various".
  Only what the list says.
summary_es: the same in natural Latin American Spanish.
programs: the services above, as given. All other fields: ""."""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _bare(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _norm_url(url: str) -> str:
    """Drop #fragments, ?queries and trailing slashes, so /about and /about/#top are one page."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/') or '/'}"


def fetch(url: str) -> tuple[BeautifulSoup | None, str]:
    """(parsed page, landed url), or (None, url) if it didn't load."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return None, url
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
        return None, url
    return BeautifulSoup(r.text, "html.parser"), r.url


def page_links(soup: BeautifulSoup, base: str) -> list[tuple[str, str]]:
    """Same-site (url, label) pairs, menus included: the menu is where the
    program pages are listed, even though its text isn't worth reading."""
    host = urlparse(base).netloc.lower().removeprefix("www.")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"])
        p = urlparse(url)
        if p.scheme not in ("http", "https") or p.netloc.lower().removeprefix("www.") != host:
            continue
        path = p.path.lower()
        if path.endswith(JUNK_FILES) or SKIP.search(path):
            continue
        key = _norm_url(url)
        if key in seen or key == _norm_url(base):
            continue
        seen.add(key)
        out.append((url, _clean(a.get_text(" "))))
    return out


def page_text(soup: BeautifulSoup) -> str:
    """The page's own content: menus, headers, footers and pop-ups removed,
    and repeated lines (sliders, mobile copies of the menu) kept once."""
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    # Menus and footers are small next to the page. Anything holding much of
    # its text is a wrapper (an ASP.NET <form> around the whole page, a div
    # called "all-but-footer"), so it stays whatever it's named.
    total = len(soup.get_text(" ")) or 1
    for tag in soup.find_all(True):
        if tag.attrs is None or tag.name in ("body", "html", "main"):  # None: removed with a parent
            continue
        hint = " ".join(tag.get("class") or []) + " " + (tag.get("id") or "") + " " + (tag.get("role") or "")
        if (tag.name in CHROME_TAGS or CHROME_HINT.search(hint)) \
                and len(tag.get_text(" ")) < 0.4 * total:
            tag.decompose()
    lines, seen = [], set()
    for block in soup.get_text("\n").split("\n"):
        line = _clean(block)
        if len(line) < 3 or line.lower() in seen:
            continue
        seen.add(line.lower())
        lines.append(line)
    return " ".join(lines)


def _kind(url: str, label: str) -> tuple[int, str]:
    hay = urlparse(url).path.lower() + " " + label.lower()
    for kind, score, words in PAGE_KINDS:
        if any(w in hay for w in words):
            return score, kind
    return 0, ""


def crawl(url: str) -> tuple[list[str], str]:
    """(pages used, their text joined)."""
    soup, landed = fetch(url)
    if soup is None:
        return [], ""
    links = page_links(soup, landed)
    pages = [(landed, soup)]
    used = {_norm_url(landed)}

    # Best links from the home page first: programs, then about, then practical.
    ranked = sorted(((*_kind(u, lbl), u) for u, lbl in links), key=lambda t: -t[0])
    queue = [u for score, _k, u in ranked if score > 0]

    # Program index pages (/programs, /services) list the programs themselves
    # one level down: read those too, but after every page the home page links
    # to, so one program's history and FAQ pages can't crowd out the others.
    while queue and len(pages) < MAX_PAGES:
        u = queue.pop(0)
        if _norm_url(u) in used:
            continue
        used.add(_norm_url(u))
        s, landed_u = fetch(u)
        if s is None or any(_norm_url(landed_u) == _norm_url(p) for p, _s in pages):
            continue  # didn't load, or redirected to a page already read
        used.add(_norm_url(landed_u))
        if _kind(u, "")[1] == "programs":
            parent = urlparse(landed_u).path.rstrip("/") + "/"
            queued = {_norm_url(q) for q in queue}
            children = [cu for cu, _l in page_links(s, landed_u)
                        if urlparse(cu).path.startswith(parent)
                        and _norm_url(cu) not in used and _norm_url(cu) not in queued]
            queue += children[:4]
        pages.append((landed_u, s))

    chunks, kept, total = [], [], 0
    for u, s in pages:
        text = page_text(s)[:PAGE_CHARS]
        if len(text) < 80:
            continue
        kept.append(u)
        chunks.append(f"--- {u}\n{text}")
        total += len(text)
        if total >= TOTAL_CHARS:
            break
    return kept, "\n\n".join(chunks)[:TOTAL_CHARS]


def draft(name: str, text: str, prompt: str | None = None) -> dict:
    from google.genai import types
    from pydantic import BaseModel

    class Draft(BaseModel):
        summary_en: str
        summary_es: str
        programs: list[str]
        who_it_serves: str
        cost: str
        languages: str
        how_to_start: str
        evidence: str
        hours: str
        phone: str

    # Thinking stays on (the model's default): this runs offline, so a slower,
    # more careful read of 50,000 characters is worth it.
    config = types.GenerateContentConfig(
        temperature=0.2,
        response_mime_type="application/json",
        response_schema=Draft,
    )
    client = get_gemini_client()
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=prompt or PROMPT.format(name=name, text=text),
                config=config,
            )
            parsed = resp.parsed or Draft.model_validate_json(resp.text)
            return parsed.model_dump()
        except Exception as exc:
            if "429" in str(exc) and attempt < 3:
                time.sleep(2 ** attempt * 5)
                continue
            raise
    return {}


def unverified(summary: str, programs: list[str], text: str) -> list[str]:
    """Numbers and program names in the draft that the crawled pages never
    mention. Specific claims are the useful part of a summary and the costly
    part to get wrong, so each one has to be traceable to the site."""
    page = " " + _bare(text) + " "
    missing = []
    for num in re.findall(r"\d[\d,.]*\d|\d", summary):
        if f" {_bare(num)} " not in page and _bare(num).replace(" ", "") not in page.replace(" ", ""):
            missing.append(num)
    for p in programs:
        name = p.split(":")[0].strip()
        if 3 <= len(name) <= 60 and name.lower() in summary.lower() and _bare(name) not in page:
            missing.append(name)
    return list(dict.fromkeys(missing))


def draft_from_services(org: dict) -> dict:
    services = sorted({s for _c, s in org["Specifics"]}) or org["SvcTagList"]
    if not services:
        return {}
    prompt = SERVICES_PROMPT.format(
        name=org["Name"], org_type=org["OrgType"] or "local organization",
        services="\n".join(f"- {s}" for s in services),
    )
    return draft(org["Name"], "", prompt)


def process(org: dict) -> dict:
    row = {
        "Name": org["Name"], "URL": org["URL"], "Flag": "", "Approved": "",
        "Summary_EN": "", "Summary_ES": "", "Programs": "", "Who_It_Serves": "",
        "Cost": "", "Languages": "", "How_To_Start": "", "Evidence": "",
        "Hours_On_Site": "", "Hours_In_Dashboard": org["Hours"],
        "Phone_On_Site": "", "Phone_In_Dashboard": org["Phone"],
        "Pages_Used": "", "Unverified": "", "Reviewer_Notes": "",
    }
    url = org["URL"].strip()
    if not url:
        row["Flag"] = "no_website"
        return row
    if not url.startswith("http"):
        url = "https://" + url
    used, text = crawl(url)
    row["Pages_Used"] = " | ".join(used)
    try:
        d = draft(org["Name"], text) if len(text) >= 200 else {}
        from_list = not d.get("summary_en", "").strip()
        if from_list:
            d = draft_from_services(org)
    except Exception as exc:
        row["Flag"] = f"gemini_failed: {type(exc).__name__}"
        return row
    row.update({
        "Summary_EN": d.get("summary_en", "").strip(),
        "Summary_ES": d.get("summary_es", "").strip(),
        "Programs": "; ".join(p.strip() for p in d.get("programs", []) if p.strip()),
        "Who_It_Serves": d.get("who_it_serves", "").strip(),
        "Cost": d.get("cost", "").strip(),
        "Languages": d.get("languages", "").strip(),
        "How_To_Start": d.get("how_to_start", "").strip(),
        "Evidence": d.get("evidence", "").strip(),
        "Hours_On_Site": d.get("hours", "").strip(),
        "Phone_On_Site": d.get("phone", "").strip(),
    })
    flags = []
    if not text:
        flags.append("site_did_not_load")
    elif len(text) < MIN_TEXT:
        flags.append("thin_text")
    if not row["Summary_EN"]:
        flags.append("no_summary")
    elif from_list:
        flags.append("from_service_list")
    else:
        # A quote the model says is verbatim but isn't on the site is the
        # clearest sign it made something up. Letters and digits only, so
        # curly quotes and stray punctuation don't count as a mismatch.
        quotes = [q for q in row["Evidence"].split("||") if q.strip()]
        if not quotes:
            flags.append("no_evidence")
        elif any(_bare(q)[:80] not in _bare(text) for q in quotes):
            flags.append("evidence_not_on_page")
    if text and not from_list:
        row["Unverified"] = "; ".join(unverified(row["Summary_EN"], d.get("programs", []), text))
        if row["Unverified"]:
            flags.append("check_facts")
    if row["Summary_EN"] and GENERIC.search(row["Summary_EN"]):
        flags.append("generic_wording")
    if row["Hours_On_Site"] and not org["Hours"].strip():
        flags.append("hours_found")
    row["Flag"] = ", ".join(flags) or "ok"
    return row


FIELDS = ["Name", "Flag", "Approved", "Summary_EN", "Summary_ES", "Programs",
          "Who_It_Serves", "Cost", "Languages", "How_To_Start", "Evidence",
          "Hours_On_Site", "Hours_In_Dashboard", "Phone_On_Site", "Phone_In_Dashboard",
          "URL", "Pages_Used", "Unverified", "Reviewer_Notes"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="redo orgs that already have a description")
    ap.add_argument("--only", action="append", metavar="NAME", help="just these orgs (repeatable)")
    ap.add_argument("--flagged", action="store_true",
                    help="redo only rows in the review sheet that aren't ok yet")
    args = ap.parse_args()

    # Merge into the existing sheet: a rerun replaces only the orgs it redoes,
    # and never a row a reviewer has already approved.
    existing: dict[str, dict] = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, newline="", encoding="utf-8") as fh:
            existing = {r["Name"]: r for r in csv.DictReader(fh)}
    approved = {n for n, r in existing.items() if r.get("Approved", "").strip()}

    df, _locs, _tax = load_from_supabase()
    orgs = df.to_dict("records")
    if args.only:
        missing = set(args.only) - {o["Name"] for o in orgs}
        if missing:
            sys.exit(f"Not in the directory: {', '.join(sorted(missing))}")
        orgs = [o for o in orgs if o["Name"] in args.only]
    elif args.flagged:
        redo = {n for n, r in existing.items() if r.get("Flag") not in ("ok", "hours_found")}
        orgs = [o for o in orgs if o["Name"] in redo]
    elif not args.all:
        orgs = [o for o in orgs if not str(o.get("Description", "")).strip()]
    orgs = [o for o in orgs if o["Name"] not in approved]
    # Rows researched by hand (from_web_search: the site blocks crawlers) are
    # only redone when named with --only.
    if not args.only:
        orgs = [o for o in orgs
                if "from_web_search" not in existing.get(o["Name"], {}).get("Flag", "")]
    if CRAWLER_LIMIT:
        orgs = orgs[: int(CRAWLER_LIMIT)]
    print(f"Drafting {len(orgs)} organizations with {MODEL}...")

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process, o): o["Name"] for o in orgs}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            name = futures[fut]
            try:
                row = fut.result()
            except Exception as exc:  # one bad site never sinks the run
                row = {"Name": name, "Flag": f"crashed: {type(exc).__name__}"}
            rows.append(row)
            print(f"[{i}/{len(orgs)}] {name}: {row['Flag']}")

    for r in rows:
        old = existing.get(r["Name"], {})
        r["Reviewer_Notes"] = old.get("Reviewer_Notes", "")  # keep a reviewer's notes
        existing[r["Name"]] = r
    rows = list(existing.values())
    # Rows that need a look first, then alphabetical.
    rows.sort(key=lambda r: (r.get("Flag") == "ok", r["Name"].lower()))
    with open(OUTPUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    counts: dict[str, int] = {}
    for r in rows:
        for f in str(r.get("Flag", "")).split(", "):
            counts[f] = counts.get(f, 0) + 1
    print(f"\nWrote {os.path.relpath(OUTPUT, ROOT)}  ({len(rows)} rows)")
    print("Flags: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
