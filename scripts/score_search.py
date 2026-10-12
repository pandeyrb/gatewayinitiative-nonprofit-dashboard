"""
Score the dashboard's search against tests/search_queries.csv.

Run from the repo root:
    python scripts/score_search.py                  # keyword search only (no API calls)
    python scripts/score_search.py --ai             # keyword + AI match, as smart_search does
    python scripts/score_search.py --ai --compare   # both, side by side
    python scripts/score_search.py --ai --show "I can't pay my rent"   # one query, with reasons
Add --all to list every query instead of only misses, --save to log the score
to tests/search_history.csv.

AI answers are cached in tests/.ai_cache.json, so rerunning costs nothing unless
the data, prompt, model or query changed. Keep that file out of git.

Per query:
  hit@5   at least one expected org is in the first 5 results
  found   share of the expected orgs that appear anywhere in the results
  extra   results that weren't expected (noise)
"""

import argparse
import csv
import os
import sys
import time
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # st.secrets looks for .streamlit/ in the working directory

import search_utils  # noqa: E402
from data_source import load_from_supabase  # noqa: E402
from search_engine import GeminiMatcher, smart_search  # noqa: E402

QUERIES = os.path.join(ROOT, "tests", "search_queries.csv")
HISTORY = os.path.join(ROOT, "tests", "search_history.csv")
AI_CACHE = os.path.join(ROOT, "tests", ".ai_cache.json")


def score(df, tax, tests, matcher):
    rows = []
    for t in tests:
        expected = {e.strip() for e in t["expected_orgs"].split(";") if e.strip()}
        res, _reasons, mode = smart_search(df, t["query"], tax, matcher)
        names = res["Name"].tolist()
        rows.append({
            "t": t,
            "hit5": bool(expected & set(names[:5])),
            "found": len(expected & set(names)) / len(expected) if expected else 0.0,
            "extra": len(set(names) - expected),
            "n": len(names),
            "mode": mode,
        })
    return rows


def summary(rows) -> dict:
    by_type = {}
    for r in rows:
        by_type.setdefault(r["t"]["type"], []).append(r["hit5"])
    n = len(rows)
    return {
        "n": n,
        "hits": sum(r["hit5"] for r in rows),
        "found": sum(r["found"] for r in rows) / n,
        "zero": sum(r["n"] == 0 for r in rows),
        "extra": sum(r["extra"] for r in rows) / n,
        "by_type": {k: (sum(v), len(v)) for k, v in sorted(by_type.items())},
    }


def print_summary(label, s):
    print(f"\n{label}: {s['hits']}/{s['n']} queries hit in top 5 ({s['hits'] / s['n']:.0%})")
    print(f"  avg share of expected orgs found: {s['found']:.0%}")
    print(f"  queries with zero results:        {s['zero']}")
    print(f"  avg unexpected results (noise):   {s['extra']:.1f}")
    print("  by type: " + ", ".join(f"{k} {h}/{n}" for k, (h, n) in s["by_type"].items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ai", action="store_true", help="use the AI layer (calls Gemini)")
    ap.add_argument("--compare", action="store_true", help="with --ai: also show keyword-only")
    ap.add_argument("--show", metavar="QUERY", help="run one query and print results with reasons")
    ap.add_argument("--fresh", action="store_true",
                    help="with --show: skip the cache, to time a real Gemini call")
    ap.add_argument("--all", action="store_true", help="list every query, not just misses")
    ap.add_argument("--save", action="store_true", help="append the summary to search_history.csv")
    ap.add_argument("--queries", default=QUERIES, metavar="CSV",
                    help="test set to score (default tests/search_queries.csv; "
                         "tests/search_queries_holdout.csv is the held-out set)")
    args = ap.parse_args()

    df, _locs, tax = load_from_supabase()
    search_utils.ALIAS_GROUPS = tax.alias_groups  # as app.py does
    matcher = GeminiMatcher(cache_path=AI_CACHE) if args.ai else None

    if args.show:
        if args.fresh and matcher:
            matcher.cache.clear()
            matcher.cache_path = None  # don't wipe the saved cache
        start = time.perf_counter()
        res, reasons, mode = smart_search(df, args.show, tax, matcher)
        took = time.perf_counter() - start
        print(f"\n{args.show!r} -> {len(res)} results ({mode}) in {took:.1f}s")
        for i, name in enumerate(res["Name"], 1):
            print(f"{i:3}. {name}" + (f"  — {reasons[name]}" if name in reasons else ""))
        return

    with open(args.queries, newline="", encoding="utf-8") as fh:
        tests = [r for r in csv.DictReader(fh) if r["query"].strip()]

    rows = score(df, tax, tests, matcher)
    s = summary(rows)

    if args.compare and args.ai:
        base = summary(score(df, tax, tests, None))
        print_summary("Keyword only", base)
        print_summary("Keyword + AI", s)
    else:
        print_summary("Keyword + AI" if args.ai else "Keyword only", s)

    print(f"\n{'':2}{'query':45} {'type':8} {'found':>6} {'extra':>6} {'results':>8}  mode")
    for r in rows:
        if args.all or not r["hit5"]:
            mark = "ok" if r["hit5"] else "--"
            print(f"{mark:2}{r['t']['query'][:44]:45} {r['t']['type']:8} "
                  f"{r['found']:6.0%} {r['extra']:6} {r['n']:8}  {r['mode']}")

    if args.save:
        new = not os.path.exists(HISTORY)
        with open(HISTORY, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["date", "mode", "queries", "hit_at_5", "avg_found", "zero_results", "note"])
            note = input("One-line note for this run (what changed?): ").strip()
            w.writerow([date.today().isoformat(), "ai" if args.ai else "keyword",
                        s["n"], s["hits"], f"{s['found']:.2f}", s["zero"], note])
        print(f"Saved to {os.path.relpath(HISTORY, ROOT)}")


if __name__ == "__main__":
    main()
