# Lawrence Resource Finder

A free, bilingual (English / Spanish) directory that helps residents of
Lawrence, Massachusetts find local help: food, housing, health care, jobs,
English classes, legal and immigration help, youth programs and more.
Built by the Gateway Initiative with Streamlit, Supabase and Google Gemini.

Residents can:
- search in their own words ("I can't pay my rent", "necesito trabajo"), with
  typo correction and an AI layer that explains why each result fits;
- see results beside a map, sorted by best match, distance or open now;
- open any organization's full profile: hours, services, locations, who it's
  for, cost and how to get help;
- see emergency numbers first when a search sounds like a crisis.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

The app reads its data from Supabase. It needs `.streamlit/secrets.toml`
(ask a maintainer; never commit it):

```toml
DATA_SOURCE = "supabase"
SUPABASE_URL = "..."
SUPABASE_KEY = "..."          # the public (publishable) key
```

Without that file it falls back to the CSVs in `data/`.

## Where the data lives

**Supabase is the source of truth.** Edits made there (in the Table Editor or
by the scripts below) show on the site within 10 minutes. The CSVs in `data/`
are the original seed data and a backup, not the live directory: organizations
added later exist only in Supabase.

> **Never run `archive/scripts/load_supabase.py` against the live database.**
> It deletes every table and reloads them from the CSVs, which would erase every
> organization, summary and hour added in Supabase since.

## Folders

| Folder | What's in it |
|---|---|
| *(root)* | The app: `app.py` (the page), `data_source.py` (loads from Supabase), `search_engine.py` + `search_utils.py` (search), `hours.py` (open/closed logic), `service_taxonomy.py` (service categories) |
| `data/` | Seed CSVs, the Lawrence boundary map, approved summaries (`org_descriptions.csv`). `data/archive/` holds old versions. `data/irs/` is local only (IRS download, not committed). |
| `db/` | Supabase migrations, numbered in the order they were run (`001` to `005`) |
| `scripts/` | Command-line tools for keeping the data up to date (see below) |
| `tests/` | Search test queries and scores, and checks for the hours logic |
| `qa/` | Records of past data reviews (categorization, contacts, locations, summaries) |
| `docs/` | Design notes, such as the service category draft |
| `archive/` | Earlier notebooks and one-off scripts, kept for reference; not used by the app |
| `review/` | Local only (not committed): review sheets the pipeline writes for a person to check |

## Scripts

Run from the repo root with the virtual environment active. Scripts that
write to Supabase need `SUPABASE_URL` and `SUPABASE_SECRET_KEY` in `.env`;
scripts that call Gemini need `GOOGLE_APPLICATION_CREDENTIALS` and
`GEMINI_PROJECT_ID`. Most have a `--dry-run` option.

**Finding and adding organizations** (run all with `scripts/auto_update.py`)

| Script | Does |
|---|---|
| `find_missing_orgs.py` | Lists Lawrence nonprofits on the IRS list (`data/irs/eo_ma.csv`) that aren't in the directory yet |
| `auto_triage.py` | Gemini + Google Search decides keep / skip / unsure for each, and finds its website |
| `add_orgs.py` | Adds "keep" orgs to Supabase; holds back possible duplicates for a person |
| `extract_details.py` | Reads each org's website and drafts its details, with a quote for every claim |
| `upload_details.py` | Publishes approved details; new orgs stay hidden until approved |

Drafts wait in `review/details_review.csv` and `review/missing_org_candidates.csv`:
put `yes` in the `approve` (or `decision`) column, then rerun the upload step.

**Summaries, hours and search**

| Script | Does |
|---|---|
| `draft_descriptions.py` | Drafts plain-language English and Spanish summaries from each org's website |
| `load_descriptions.py` | Loads approved summaries into Supabase and `data/org_descriptions.csv` |
| `score_search.py` | Scores search against `tests/search_queries.csv` (`--ai` to include Gemini) |
| `search_report.py` | What residents search for, and which searches found nothing |

**Audits** (from the CSV era; read `data/GWIorgs_v6.csv`)

`audit_contact_info.py`, `audit_service_tags.py`, `check_specific_services.py`,
`verify_locations.py`. Their findings are in `qa/`.

**Helpers** `_common.py` (shared Supabase and Gemini setup) and
`unified_crawler_starter.py` (website-reading helpers) are imported by the
scripts above. The one-time setup script that first built the database from
the CSVs is archived in `archive/scripts/load_supabase.py`; see the warning above.

## Tests

```bash
python tests/test_hours.py                      # open/closed logic
python scripts/score_search.py                  # search quality, keyword only
python scripts/score_search.py --ai --compare   # with the AI layer
```

## Never commit

`.env`, `.streamlit/secrets.toml`, `review/` and `data/irs/`. They hold keys
or data still under review, and are listed in `.gitignore`.
