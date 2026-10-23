"""
Decide keep / skip for each candidate automatically, using Gemini with
Google Search, and find its website. Replaces reviewing the candidates sheet
by hand.

Run from the repo root (after find_missing_orgs.py), or let
scripts/auto_update.py run it for you:
    python scripts/auto_triage.py

For every row in review/missing_org_candidates.csv with an empty decision,
Gemini searches the web and answers:
    keep    active, with programs residents can use, in or serving Lawrence
    skip    worship-only church, PTO/booster club, sports club, foundation,
            social club, defunct, or not serving Lawrence
    unsure  not enough information; left blank for a person

A "keep" whose website doesn't load is downgraded to unsure. Rows already
decided (by you or a previous run) are never touched, so it's safe to rerun.

A "keep" whose name or IRS type points to a kind of organization the
directory usually doesn't list (worship, lodge, foundation or trust, alumni,
union, chamber of commerce) is also downgraded to unsure: some of these do run
real services (a church food pantry), so a person decides.

Gemini 2.5 Flash includes 1,500 search-grounded prompts per day at no extra
charge on Vertex AI; one candidate = one grounded prompt. Thinking stays on
here: triage runs offline, and a wrong keep or skip costs more than a slower run.
"""

import concurrent.futures
import json
import os
import re
import sys
import threading
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import REVIEW_DIR, ROOT, gemini  # noqa: E402

CANDIDATES = os.path.join(REVIEW_DIR, "missing_org_candidates.csv")
HEADERS = {"User-Agent": "Mozilla/5.0 (GatewayInitiative resource directory)"}
NOT_A_WEBSITE = ("guidestar", "candid.org", "propublica", "charitynavigator", "greatnonprofits",
                 "causeiq", "opencorporates", "bizapedia", "irs.gov", "taxexemptworld", "yelp",
                 "vertexaisearch")

PROMPT = """You are helping build a free directory that helps residents of Lawrence,
Massachusetts find local help: food, housing, health, mental health, education,
English classes, jobs, legal and immigration help, youth programs, seniors,
disability services, and similar.

This organization is on the IRS list of tax-exempt organizations:
  Name:     {irs_name}
  Address:  {street}, {city}, MA {zip}
  IRS type: {category} ({ntee})

Search the web and decide:
  keep    it is active (signs of activity in the last two years) and offers
          programs or services the public can use, in or serving Lawrence
  skip    worship-only congregation, PTO/booster/alumni group, sports league or
          club, private foundation or grantmaker, social or fraternal club,
          condo or housing association, political group, or no longer active
  unsure  you cannot find enough information to tell

Reply with ONLY this JSON, no other text:
{{"decision": "keep|skip|unsure",
  "display_name": "the name the public knows it by",
  "website": "its own official homepage URL, or empty string",
  "what_they_do": "one short sentence",
  "reason": "one short sentence explaining the decision"}}

The website must be the organization's own site, not a listing on GuideStar,
ProPublica, Charity Navigator, GreatNonprofits or similar. A Facebook page is
acceptable only if it is clearly the organization's main web presence."""


# Names or IRS types that usually mean "not a service residents can use".
# A match never skips an org outright, it only sends a "keep" to a person.
NEEDS_A_PERSON = [
    ("place of worship", re.compile(
        r"\b(church|iglesia|parish|parroquia|congregation|ministr(y|ies)|ministerio|temple|"
        r"templo|mosque|masjid|synagogue|chapel|capilla|shrine|evangel\w*|pentecost\w*|"
        r"assembl(y|ies) of god|asambleas? de dios|baptist|bautista|methodist|metodista|"
        r"apostolic|apost[oó]lic[ao]|bah[aá]['ʼ]?[ií]s?|cristo|cristian[ao]|dios)\b", re.I)),
    ("lodge or fraternal club", re.compile(
        r"\b(lodge|elks|knights of columbus|masonic|freemason\w*|moose|odd fellows|"
        r"auxiliary|memorial guard|grange)\b", re.I)),
    ("foundation or trust", re.compile(r"\b(foundation|fundaci[oó]n|trust|endowment)\b", re.I)),
    ("alumni group", re.compile(r"\b(alumni|alumnae|alumnos)\b", re.I)),
    ("union", re.compile(
        r"\b((?<!credit )union|local \d+|(officers|staff|teachers|employees|administrators|"
        r"firefighters|workers) association)\b", re.I)),
    ("chamber of commerce", re.compile(r"\b(chamber of commerce|c[aá]mara de comercio)\b", re.I)),
]
# IRS NTEE groups for religion (X) and grantmaking foundations (T).
NEEDS_A_PERSON_NTEE = {"X": "place of worship", "T": "foundation or trust"}


def needs_a_person(row, display_name: str = "") -> str:
    """Why a "keep" should go to a person instead, or "" if it's fine."""
    names = f"{row.get('irs_name', '')} {display_name or row.get('display_name', '')}"
    for why, pattern in NEEDS_A_PERSON:
        if pattern.search(names):
            return why
    return NEEDS_A_PERSON_NTEE.get(str(row.get("ntee_code", ""))[:1].upper(), "")


def ask(client, model, thinking, row) -> dict:
    from google.genai import types

    prompt = PROMPT.format(irs_name=row["irs_name"], street=row["street"], city=row["city"],
                           zip=row["zip"], category=row["category"],
                           ntee=row["ntee_code"] or "no code")
    # Search grounding can't be combined with a JSON schema on 2.5 models,
    # so the JSON is asked for in the prompt and pulled out of the text.
    # thinking_config=None: the model's default thinking. Triage runs offline,
    # so accuracy matters more than speed (search_engine.py keeps it off for
    # live searches).
    config = types.GenerateContentConfig(
        temperature=0,
        tools=[types.Tool(google_search=types.GoogleSearch())],
        thinking_config=None,
    )
    for attempt in range(5):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            m = re.search(r"\{.*\}", resp.text or "", re.S)
            if not m:
                return {"decision": "unsure", "reason": "no answer from Gemini"}
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"decision": "unsure", "reason": "unreadable answer from Gemini"}
        except Exception as exc:
            if "429" in str(exc) and attempt < 4:
                time.sleep(2 ** attempt * 5)
                continue
            return {"decision": "unsure", "reason": f"error: {type(exc).__name__}: {str(exc)[:80]}"}
    return {"decision": "unsure", "reason": "rate limited"}


def clean_website(url: str) -> str:
    url = (url or "").strip()
    if not url or any(bad in url.lower() for bad in NOT_A_WEBSITE):
        return ""
    return url if url.startswith("http") else "https://" + url


def website_works(url: str) -> bool:
    try:
        return requests.get(url, headers=HEADERS, timeout=15).status_code < 400
    except requests.RequestException:
        return False


def main():
    if not os.path.exists(CANDIDATES):
        sys.exit("Run scripts/find_missing_orgs.py first.")
    sheet = pd.read_csv(CANDIDATES, dtype=str).fillna("")
    for col in ("decided_by", "ai_reason"):
        if col not in sheet.columns:
            sheet[col] = ""

    # Undecided and not already looked at by the AI ("unsure" rows wait for a person).
    todo = [i for i in sheet.index
            if not sheet.at[i, "decision"].strip() and sheet.at[i, "decided_by"] != "ai"]
    if not todo:
        print("auto_triage: no undecided candidates.")
        return

    client, model, thinking = gemini()
    print(f"Checking {len(todo)} candidates with {model} + Google Search…")
    lock = threading.Lock()
    counts = {"keep": 0, "skip": 0, "unsure": 0}

    def work(i):
        a = ask(client, model, thinking, sheet.loc[i])
        decision = str(a.get("decision", "unsure")).strip().lower()
        decision = decision if decision in counts else "unsure"
        website = clean_website(a.get("website", ""))
        reason = str(a.get("reason", "")).strip()
        if decision == "keep" and not (website and website_works(website)):
            decision = "unsure"
            reason = (reason + " — " if reason else "") + "no working website found"
        if decision == "keep":
            why = needs_a_person(sheet.loc[i], str(a.get("display_name", "")))
            if why:
                decision = "unsure"
                reason = (reason + " — " if reason else "") + f"looks like a {why}; a person should decide"
        what = str(a.get("what_they_do", "")).strip()
        with lock:
            sheet.at[i, "decision"] = "" if decision == "unsure" else decision
            if str(a.get("display_name", "")).strip():
                sheet.at[i, "display_name"] = str(a["display_name"]).strip()
            if website:
                sheet.at[i, "website"] = website
            sheet.at[i, "ai_reason"] = f"{decision.upper()}: {reason}" + (f" | {what}" if what else "")
            sheet.at[i, "decided_by"] = "ai"
            counts[decision] += 1
            sheet.to_csv(CANDIDATES, index=False)  # save as it goes
        return sheet.at[i, "display_name"], decision

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for name, decision in pool.map(work, todo):
            print(f"  {decision:6} {name}")

    print(f"auto_triage: keep {counts['keep']}, skip {counts['skip']}, unsure {counts['unsure']}")


if __name__ == "__main__":
    main()
