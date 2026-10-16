"""
One command that finds missing Lawrence nonprofits, adds them, and fills in
details for every org, with no manual review except the uncertain cases.

Run from the repo root:
    python scripts/auto_update.py --dry-run   # everything except writing to Supabase
    python scripts/auto_update.py

Steps (each is also a script you can run alone):
  1. find_missing_orgs   IRS file -> review/missing_org_candidates.csv
                         (skipped if data/irs/eo_ma.csv isn't there)
  2. auto_triage         Gemini + Google Search decides keep / skip, finds websites
  3. add_orgs            adds "keep" orgs to Supabase
  4. extract_details     reads each website, drafts details; rows fully backed by
                         quotes are marked approve = auto
  5. upload_details      writes approved + auto rows to Supabase

At the end it lists what still needs a person: candidates the AI was unsure
about, and detail rows with flags or errors. Everything else is done.
Rerun monthly (or after downloading a newer IRS file); finished work is skipped.
"""

import argparse
import os
import subprocess
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import REVIEW_DIR, ROOT  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def run(script, *args, answer=None):
    print(f"\n━━ {script} {' '.join(args)}")
    r = subprocess.run([sys.executable, os.path.join(HERE, script), *args],
                       cwd=ROOT, input=answer, text=True)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't write to Supabase")
    args = ap.parse_args()
    dry = ["--dry-run"] if args.dry_run else []

    if os.path.exists(os.path.join(ROOT, "data", "irs", "eo_ma.csv")):
        run("find_missing_orgs.py")
        run("auto_triage.py")
        run("add_orgs.py", *dry)
    else:
        print("No data/irs/eo_ma.csv — skipping the new-org steps.")

    if args.dry_run:
        print("\nDry run: stopping before website reading (new orgs weren't added).")
        return

    run("extract_details.py", "--only-missing")
    run("upload_details.py")

    # What's left for a person
    print("\n━━ Needs a person")
    cand = os.path.join(REVIEW_DIR, "missing_org_candidates.csv")
    if os.path.exists(cand):
        c = pd.read_csv(cand, dtype=str).fillna("")
        unsure = c[(c["decision"] == "") & c["ai_reason"].str.startswith("UNSURE")]
        print(f"  {len(unsure)} candidates the AI was unsure about "
              f"(set decision in {os.path.relpath(cand, ROOT)}, then rerun)")
        held = c[(c["decision"] == "keep") & c["notes"].str.startswith("POSSIBLE DUPLICATE")]
        print(f"  {len(held)} kept candidates held back as possible duplicates of listed orgs "
              f"(see notes in {os.path.relpath(cand, ROOT)})")
    det = os.path.join(REVIEW_DIR, "details_review.csv")
    if os.path.exists(det):
        d = pd.read_csv(det, dtype=str).fillna("")
        todo = d[d["approve"] == ""]
        print(f"  {len(todo)} detail rows flagged or failed "
              f"(fix and put yes in approve in {os.path.relpath(det, ROOT)}, then rerun)")


if __name__ == "__main__":
    main()
