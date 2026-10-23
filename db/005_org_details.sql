-- 005: resident-facing details for each organization, and its IRS tax ID.
--
-- Filled by scripts/extract_details.py (Gemini reads the org's website) and
-- written here only after a person approves them (scripts/upload_details.py).
-- description already exists from 001.

alter table organizations add column if not exists ein                 text;  -- IRS tax ID, e.g. 04-1234567
alter table organizations add column if not exists who_its_for         text;         -- "Lawrence residents 60+", "anyone"
alter table organizations add column if not exists cost                text;         -- "Free", "Sliding scale"
alter table organizations add column if not exists languages           text;         -- "English, Spanish"
alter table organizations add column if not exists how_to_get_help     text;         -- "Walk in Mon–Fri 9–12; no appointment"
alter table organizations add column if not exists details_sources     text;         -- pages the details came from
alter table organizations add column if not exists details_reviewed_on date;         -- when a person approved them

-- One EIN per organization (a separate statement so it's safe to rerun).
create unique index if not exists organizations_ein_key on organizations (ein);
