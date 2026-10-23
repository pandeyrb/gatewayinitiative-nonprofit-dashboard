"""Shared setup for the scripts in this folder: repo paths, Supabase, Gemini.

Everything reads .env in the repo root:
  SUPABASE_URL, SUPABASE_SECRET_KEY              (scripts write, so the secret key)
  GOOGLE_APPLICATION_CREDENTIALS, GEMINI_PROJECT_ID
  GEMINI_MODEL (optional, default gemini-2.5-flash), GEMINI_LOCATION (optional)
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_DIR = os.path.join(ROOT, "review")
LAWRENCE_ZIPS = {"01840", "01841", "01842", "01843"}


def load_env():
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))


def supabase():
    load_env()
    from supabase import create_client

    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SECRET_KEY in .env")
    return create_client(url, key)


def fetch_all(client, table: str, order: str = "id", page: int = 1000) -> list[dict]:
    rows, start = [], 0
    while True:
        batch = (client.table(table).select("*").order(order)
                 .range(start, start + page - 1).execute().data)
        rows.extend(batch)
        if len(batch) < page:
            return rows
        start += page


def gemini():
    """(client, model, thinking_config) set up like search_engine.GeminiMatcher."""
    load_env()
    from google import genai
    from google.genai import types

    project = os.getenv("GEMINI_PROJECT_ID")
    if not project or not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        sys.exit("Set GOOGLE_APPLICATION_CREDENTIALS and GEMINI_PROJECT_ID in .env")
    client = genai.Client(
        vertexai=True, project=project,
        location=os.getenv("GEMINI_LOCATION", "us-central1"),
        http_options=types.HttpOptions(timeout=120_000),
    )
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    thinking = None
    if "2.5-flash" in model.lower():
        thinking = types.ThinkingConfig(thinking_budget=0)
    elif model.lower().startswith("gemini-3"):
        thinking = types.ThinkingConfig(thinking_level="LOW")
    return client, model, thinking


def is_yes(v) -> bool:
    return str(v or "").strip().lower() in {"y", "yes", "x", "keep", "ok", "approve", "approved", "1", "true"}
