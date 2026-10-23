-- 003: organization-level hours.
--
-- An org's card shows one hours summary ("Main site clinic: Mon–Thu 8 AM–9 PM;
-- pharmacy: ..."), separate from each site's own hours. 001 only had hours on
-- locations, so multi-site orgs lost their summary. It lives on the
-- organization now; locations.hours_text is for a specific site's hours.

alter table organizations add column hours_text        text;
alter table organizations add column hours_source_url  text;
alter table organizations add column hours_verified_on date;
