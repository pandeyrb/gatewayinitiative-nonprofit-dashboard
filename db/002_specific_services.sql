-- 002: match the app's real taxonomy.
--
-- The app filters on category -> specific service (data/specific_services.csv),
-- not on the canonical tags from 001. A specific service name is only unique
-- within its category ("Youth Sports" exists under two), and one raw service
-- can sit under several specifics.
--
-- The canonical-tag tables are dropped: the app only used them in the search
-- text, and specific services (with Spanish labels) cover that.

drop table if exists quick_filter_tags;
drop table if exists service_tags;
drop table if exists tag_overrides;
drop table if exists tag_rules;
drop table if exists tags;

create table specific_services (
    id           bigint generated always as identity primary key,
    category_id  bigint not null references categories(id) on delete cascade,
    name_en      text not null,
    name_es      text not null,
    sort_order   int  not null,               -- order within its category
    unique (category_id, name_en)
);

-- Which specific services each org's raw service counts as.
create table service_specifics (
    service_id           bigint references services(id) on delete cascade,
    specific_service_id  bigint references specific_services(id) on delete cascade,
    source               text,                -- 'draft', 'added-2026-09-28', ...
    primary key (service_id, specific_service_id)
);

-- Every category a raw service is in, including ones with no specific yet
-- (from the boss's draft, service_categories.csv).
create table service_categories (
    service_id   bigint references services(id) on delete cascade,
    category_id  bigint references categories(id) on delete cascade,
    primary key (service_id, category_id)
);

-- The "what do you need" buttons: a title and a one-line description.
alter table quick_filters add column description_en text;
alter table quick_filters add column description_es text;

create table quick_filter_specifics (
    quick_filter_key     text references quick_filters(key) on delete cascade,
    specific_service_id  bigint references specific_services(id) on delete cascade,
    primary key (quick_filter_key, specific_service_id)
);

alter table specific_services      enable row level security;
alter table service_specifics      enable row level security;
alter table service_categories     enable row level security;
alter table quick_filter_specifics enable row level security;

create policy "public read" on specific_services      for select using (true);
create policy "public read" on service_specifics      for select using (true);
create policy "public read" on service_categories     for select using (true);
create policy "public read" on quick_filter_specifics for select using (true);
