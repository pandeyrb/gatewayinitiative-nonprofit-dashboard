-- 004: Spanish summaries.
--
-- organizations.description (001) holds the plain-language English summary a
-- resident sees on an org's card. The Spanish page needs its own, written by
-- scripts/draft_descriptions.py and approved by a person like the English one.
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run.

alter table organizations add column if not exists description_es text;
