-- 2026-09-11: measure three different things that were all invisible.
--
--   click_events   how much traffic we send each club — the number that
--                  matters commercially, and the one no off-the-shelf
--                  analytics tool can join to our own club records
--   search_events  what people searched for and whether we had anything,
--                  so "they searched Swanley and got nothing" is answerable
--   scrape_runs    our own request volume and health over time; `courses`
--                  already holds the LATEST status per sheet, but nothing
--                  kept history, so "how often do you hit us?" had no answer
--
-- PRIVACY, deliberately: no cookies, no identifiers, no IP, and NO FULL
-- POSTCODES. A full postcode plus a timestamp can identify a household, which
-- would make this personal data; the outward code ("TN13") or the resolved
-- town has the same analytical value and none of the exposure. The CHECK on
-- search_events.area enforces that rather than trusting the caller.

create table if not exists click_events (
    id          bigint generated always as identity primary key,
    created_at  timestamptz not null default now(),
    club_id     bigint references clubs(id) on delete set null,
    club_name   text not null,          -- kept denormalised: a click is history,
    course_name text,                   -- and stays true if a club is removed
    platform    text,
    tee_date    date,
    tee_time    time,
    players     int  check (players is null or players between 1 and 8),
    price       numeric check (price is null or price >= 0)
);
create index if not exists click_events_club_idx on click_events (club_name, created_at desc);
create index if not exists click_events_time_idx on click_events (created_at desc);

create table if not exists search_events (
    id            bigint generated always as identity primary key,
    created_at    timestamptz not null default now(),
    -- Outward code or town name only. Length cap is the enforcement: a full
    -- UK postcode is at least 6 characters with the space, an outward code at
    -- most 4, and town names are allowed but must not contain a digit-letter
    -- inward code pattern.
    area          text check (area is null or (length(area) <= 40
                              and area !~* '[0-9][a-z]{2}\s*$')),
    radius_km     numeric check (radius_km is null or radius_km between 0 and 200),
    tee_date      date,
    players       int  check (players is null or players between 1 and 8),
    result_count  int  check (result_count is null or result_count >= 0),
    club_count    int  check (club_count is null or club_count >= 0)
);
create index if not exists search_events_time_idx on search_events (created_at desc);

create table if not exists scrape_runs (
    id           bigint generated always as identity primary key,
    started_at   timestamptz not null,
    finished_at  timestamptz not null default now(),
    days         int,
    platforms    text,
    courses      int,      -- sheets attempted (= requests we made, near enough)
    ok           int,
    empty        int,
    error        int,
    rows_written int
);
create index if not exists scrape_runs_time_idx on scrape_runs (finished_at desc);

alter table click_events  enable row level security;
alter table search_events enable row level security;
alter table scrape_runs   enable row level security;

-- The page may ADD an event and nothing else. No select policy, so the anon
-- key cannot read anyone's events back — including its own. The scraper writes
-- scrape_runs over the direct connection as the owner, which bypasses RLS, so
-- that table needs no policy at all.
create policy "anon may record a click"  on click_events  for insert to anon, authenticated with check (true);
create policy "anon may record a search" on search_events for insert to anon, authenticated with check (true);

grant insert on click_events  to anon, authenticated;
grant insert on search_events to anon, authenticated;
