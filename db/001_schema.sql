-- GWI resource directory schema (Supabase / Postgres)
-- Loosely follows Open Referral HSDS: organization -> location, organization -> service.
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run.

-- ── where we operate ─────────────────────────────────────────────────────────
-- New city = new row here, not new code.
create table regions (
    id          bigint generated always as identity primary key,
    name        text not null,                -- 'Lawrence'
    state       text not null default 'MA',
    unique (name, state)
);

-- ── organizations ────────────────────────────────────────────────────────────
create table organizations (
    id                  bigint generated always as identity primary key,
    name                text not null unique,
    url                 text,
    org_type            text,                 -- 'Non-profit organization', 'K-12 School', ...
    service_area        text,                 -- 'Human Services', 'Youth Development', ...
    description         text,                 -- mission / about text; searchable
    phone               text,
    strategic_plan_url  text,
    impact_report_url   text,
    is_active           boolean not null default true,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create table organization_regions (
    organization_id  bigint references organizations(id) on delete cascade,
    region_id        bigint references regions(id) on delete cascade,
    primary key (organization_id, region_id)
);

-- ── physical sites ───────────────────────────────────────────────────────────
-- Every org gets at least one row (its main address from GWIorgs_v6).
-- Extra sites come from org_locations.csv.
create table locations (
    id                bigint generated always as identity primary key,
    organization_id   bigint not null references organizations(id) on delete cascade,
    region_id         bigint references regions(id),
    name              text,                   -- 'St. Martha''s Food Pantry'; null = main office
    is_main           boolean not null default false,
    kind              text not null default 'public' check (kind in ('public', 'admin')),
    address           text,
    city              text,
    state             text default 'MA',
    zip               text,                   -- text, not integer: keeps the leading 0 in 01840
    latitude          double precision,
    longitude         double precision,
    geo_source        text,                   -- 'census', 'nominatim', 'manual'
    phone             text,
    hours_text        text,                   -- raw text, parsed by hours.py at read time
    services_here     text,
    source_url        text,
    verified_on       date,
    updated_at        timestamptz not null default now()
);
create index on locations (organization_id);

-- ── taxonomy ─────────────────────────────────────────────────────────────────
create table categories (
    id          bigint generated always as identity primary key,
    name_en     text not null unique,
    name_es     text not null,
    sort_order  int  not null                 -- urgency order (CATEGORY_ORDER)
);

-- Canonical tags = the filter options. Each belongs to exactly one category.
create table tags (
    id           bigint generated always as identity primary key,
    name_en      text not null unique,
    name_es      text not null,
    category_id  bigint not null references categories(id)
);

-- Raw service strings as each org words them ("Food Pantry Services").
create table services (
    id               bigint generated always as identity primary key,
    organization_id  bigint not null references organizations(id) on delete cascade,
    name             text not null,
    description      text,
    source           text,                    -- 'gemini-2026-09', 'manual', 'boss-draft'
    unique (organization_id, name)
);

-- A raw service can map to several tags (community garden -> food AND environment).
create table service_tags (
    service_id  bigint references services(id) on delete cascade,
    tag_id      bigint references tags(id) on delete cascade,
    primary key (service_id, tag_id)
);

-- ── vocabulary that used to live in .py files ────────────────────────────────
-- Replaces ALIAS_GROUPS: every term in the same group is interchangeable.
create table synonyms (
    id          bigint generated always as identity primary key,
    group_name  text not null,                -- 'esl', 'childcare', ...
    term        text not null,
    lang        text not null default 'en',
    unique (group_name, term)
);

-- Replaces OVERRIDES: exact raw text -> tag. tag_id null = deliberately untagged.
create table tag_overrides (
    raw_text  text primary key,               -- stored lowercase
    tag_id    bigint references tags(id),
    reason    text
);

-- Replaces RULES. Optional: fine to keep the regexes in code for now.
-- Lower priority runs first, since rule order matters (rehab before "therapy").
create table tag_rules (
    id        bigint generated always as identity primary key,
    pattern   text not null,                  -- Postgres/Python-compatible regex
    tag_id    bigint not null references tags(id),
    priority  int not null,
    note      text
);

-- Replaces QUICK_FILTERS (the big "what do you need" buttons).
create table quick_filters (
    key         text primary key,             -- 'quick_food_pantry'
    label_en    text not null,
    label_es    text not null,
    sort_order  int  not null
);
create table quick_filter_tags (
    quick_filter_key  text references quick_filters(key) on delete cascade,
    tag_id            bigint references tags(id) on delete cascade,
    primary key (quick_filter_key, tag_id)
);

-- ── search analytics ─────────────────────────────────────────────────────────
-- Zero-result queries are the to-do list for new synonyms.
create table search_log (
    id            bigint generated always as identity primary key,
    query         text not null,
    lang          text,
    result_count  int,
    created_at    timestamptz not null default now()
);

-- ── updated_at bookkeeping ───────────────────────────────────────────────────
create function touch_updated_at() returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end $$;

create trigger organizations_touch before update on organizations
    for each row execute function touch_updated_at();
create trigger locations_touch before update on locations
    for each row execute function touch_updated_at();

-- ── access rules ─────────────────────────────────────────────────────────────
-- Supabase exposes every table through its public API. Without these, anyone
-- with the app's public key could edit the directory.
-- Public: read the directory, and write to search_log only.
alter table regions               enable row level security;
alter table organizations         enable row level security;
alter table organization_regions  enable row level security;
alter table locations             enable row level security;
alter table categories            enable row level security;
alter table tags                  enable row level security;
alter table services              enable row level security;
alter table service_tags          enable row level security;
alter table synonyms              enable row level security;
alter table tag_overrides         enable row level security;
alter table tag_rules             enable row level security;
alter table quick_filters         enable row level security;
alter table quick_filter_tags     enable row level security;
alter table search_log            enable row level security;

create policy "public read" on regions              for select using (true);
create policy "public read" on organizations        for select using (is_active);
create policy "public read" on organization_regions for select using (true);
create policy "public read" on locations            for select using (true);
create policy "public read" on categories           for select using (true);
create policy "public read" on tags                 for select using (true);
create policy "public read" on services             for select using (true);
create policy "public read" on service_tags         for select using (true);
create policy "public read" on synonyms             for select using (true);
create policy "public read" on quick_filters        for select using (true);
create policy "public read" on quick_filter_tags    for select using (true);
create policy "log searches" on search_log          for insert with check (true);
-- tag_overrides / tag_rules: no public policy; only the loader (service key) uses them.

-- ── later: semantic search ───────────────────────────────────────────────────
-- create extension if not exists vector;
-- alter table organizations add column embedding vector(768);
