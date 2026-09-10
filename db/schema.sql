-- Golf Tee Time Aggregator — database schema (Supabase / PostgreSQL + PostGIS)
--
-- Run once in the Supabase SQL editor. Written for Supabase but standard
-- Postgres apart from the postgis extension (which Supabase ships).
--
-- Shape: clubs (a physical venue, one location) 1--N courses (a bookable tee
-- sheet: platform + url + the platform's own course id) 1--N tee_times
-- (current availability, replaced wholesale on each scrape).
--
-- What this DOES store: public visitor tee-time availability and prices, and
-- the deep link to each slot on the club's own booking page.
-- What it deliberately does NOT store: bookings, payments, or any user PII.
-- This is a pure click-through aggregator; the booking always happens on the
-- club's own site. Keep it that way — it's the project's whole legal posture.

create extension if not exists postgis;


-- ============================================================================
-- clubs — one physical venue, one location, one radius-search target
-- ============================================================================
create table clubs (
    id              bigint generated always as identity primary key,
    slug            text not null unique,          -- url-safe id, e.g. 'west-malling'
    name            text not null,                 -- display name, e.g. 'West Malling'
    postcode        text,
    latitude        double precision,
    longitude       double precision,

    -- Radius search runs on this. Derived automatically from lat/long so it
    -- can never drift out of sync with them. Null until the club is geocoded
    -- (an un-geocoded club simply won't match any radius search — correct).
    location        geography(Point, 4326) generated always as (
                        st_setsrid(st_makepoint(longitude, latitude), 4326)::geography
                    ) stored,

    -- TRUE only for 27-hole clubs whose course combinations share nines, so
    -- the same physical tee time appears on more than one course sheet (The
    -- Heron). The read layer collapses those to one row per (club,date,time);
    -- see search_tee_times below. Default FALSE keeps genuinely separate
    -- courses (West Malling's Spitfire vs Hurricane) as distinct options.
    -- NOTE: that the shared slots are the *same bookable* slot is inferred
    -- from the shared nines, not yet confirmed with the club — hence a flag
    -- we can flip off, not a hard-coded merge.
    dedupe_courses  boolean not null default false,

    created_at      timestamptz not null default now()
);

create index clubs_location_gix on clubs using gist (location);


-- ============================================================================
-- courses — one bookable tee sheet (what a scraper actually fetches)
-- ============================================================================
create table courses (
    id              bigint generated always as identity primary key,
    club_id         bigint not null references clubs(id) on delete cascade,
    name            text not null,                 -- 'Spitfire', 'Kingfisher'; = club name if single-course

    -- Which scraper owns this row (mirrors clubs_config.csv's platform column).
    -- Format check only. This was a list of allowed platform names once; it
    -- drifted from golf_common.PLATFORMS and silently blocked two new
    -- scrapers (see db/migrations/001). The scrapers own the vocabulary.
    platform        text not null check (platform ~ '^[a-z][a-z0-9_]{2,31}$'),
    base_url        text not null,                 -- platform entry point
    course_ref      text not null default '',      -- the PLATFORM's own course id (IG course / ESP clubid /
                                                   -- GM idResource / ClubV1 courseId). '' where the platform needs
                                                   -- none (GM's default tee) — kept as '' not NULL so the unique
                                                   -- key and ON CONFLICT upsert below behave (NULLs don't compare equal).

    -- n8n polls only enabled courses. Clubs we can't scrape (login wall, no
    -- online booking) can still exist as rows with scrape_enabled = false.
    scrape_enabled  boolean not null default true,

    -- Populated by each scrape run so n8n / a dashboard can see freshness and
    -- failures without a separate table. 'ok' | 'empty' | 'error'.
    last_scraped_at timestamptz,
    last_status     text check (last_status in ('ok', 'empty', 'error')),
    last_error      text,

    -- Stops duplicate config: the same sheet can't be added twice.
    unique (platform, base_url, course_ref)
);

-- Partial index: the scheduler's "what do I need to poll?" query.
create index courses_scrape_enabled_idx on courses (scrape_enabled) where scrape_enabled;
create index courses_club_idx on courses (club_id);


-- ============================================================================
-- tee_times — current availability. Replaced wholesale per (course, date) on
-- every scrape, so a row existing means "bookable as of checked_at".
-- ============================================================================
create table tee_times (
    id              bigint generated always as identity primary key,
    course_id       bigint not null references courses(id) on delete cascade,

    tee_date        date not null,                 -- local calendar date
    tee_time        time not null,                 -- local wall-clock (BST/GMT); kept separate from date to avoid tz bugs
    holes           smallint not null check (holes in (9, 18)),  -- longest round bookable at this slot

    -- {"1": 43.0, "2": 86.0, ...} — TOTAL price for that party size.
    -- The KEYS are the party sizes actually bookable at this slot (a 2-player
    -- minimum slot has no "1" key; a nearly-full slot may only have "1"). The
    -- search never assumes 1-4 and never multiplies — it reads these directly.
    prices          jsonb not null,

    booking_url     text not null,                 -- deep link into the club's own booking flow for this slot
    checked_at      timestamptz not null,          -- when the scraper saw this slot (from the scraper, not now())

    -- The refresh key: one row per slot per sheet per date. Lets a re-scrape
    -- replace a (course, date)'s slots cleanly (see the refresh recipe below).
    unique (course_id, tee_date, tee_time)
);

-- Primary search access path: "slots on DATE", then join up to the club.
create index tee_times_date_course_idx on tee_times (tee_date, course_id);
-- Supports the jsonb `?` party-size test and containment on prices.
create index tee_times_prices_gin on tee_times using gin (prices);


-- ============================================================================
-- Row-level security
-- ----------------------------------------------------------------------------
-- All three tables hold public data (it's visible on the clubs' own sites),
-- so the anon API key may READ them. Nothing may WRITE via the API: the
-- scrapers/n8n write with the service_role key, which bypasses RLS entirely.
-- (courses exposes base_url / last_error too; if you'd rather not surface
-- scrape internals to the public API, put a view over courses with just the
-- display columns and read that instead.)
-- ============================================================================
alter table clubs      enable row level security;
alter table courses    enable row level security;
alter table tee_times  enable row level security;

create policy "public read clubs"      on clubs      for select to anon, authenticated using (true);
create policy "public read courses"    on courses    for select to anon, authenticated using (true);
create policy "public read tee_times"  on tee_times  for select to anon, authenticated using (true);


-- ============================================================================
-- search_tee_times — the product query, as one RPC the app can call.
-- "Slots on <date> within <radius> km of <lat,lng>, optionally for a party of
--  <players>, under <max_price>, for <holes> holes" — nearest then cheapest.
-- Party-size filtering keys off the prices map: `prices ? players` means that
-- party size is actually bookable at the slot.
-- ============================================================================
create or replace function search_tee_times(
    in_lat        double precision,
    in_lng        double precision,
    in_radius_km  double precision,
    in_date       date,
    in_players    int     default null,   -- null = don't filter/return a per-party price
    in_max_price  numeric default null,   -- only applied when in_players is given
    in_holes      int     default null    -- 9 or 18; null = either
)
returns table (
    club_id     bigint,
    club_name   text,
    course_name text,
    platform    text,
    distance_km double precision,
    tee_date    date,
    tee_time    time,
    holes       smallint,
    players     int,
    price       numeric,                   -- total for in_players, when given
    prices      jsonb,                     -- full party-size -> total map
    booking_url text,
    checked_at  timestamptz
)
language sql
stable
as $$
    select
        c.id,
        c.name,
        co.name,
        co.platform,
        st_distance(c.location,
                    st_setsrid(st_makepoint(in_lng, in_lat), 4326)::geography) / 1000.0,
        t.tee_date,
        t.tee_time,
        t.holes,
        in_players,
        case when in_players is not null
             then (t.prices ->> in_players::text)::numeric end,
        t.prices,
        t.booking_url,
        t.checked_at
    from tee_times t
    join courses co on co.id = t.course_id
    join clubs   c  on c.id  = co.club_id
    where t.tee_date = in_date
      and c.location is not null
      and st_dwithin(c.location,
                     st_setsrid(st_makepoint(in_lng, in_lat), 4326)::geography,
                     in_radius_km * 1000)
      and (in_players is null or t.prices ? in_players::text)
      and (in_holes   is null or t.holes = in_holes)
      and (in_max_price is null or in_players is null
           or (t.prices ->> in_players::text)::numeric <= in_max_price)
    -- order by column position (5 = distance, 10 = price, 7 = tee_time): a SQL
    -- function body can't see the RETURNS TABLE column names as aliases here.
    order by 5, 10 nulls last, 7;
$$;

grant execute on function search_tee_times to anon, authenticated;

-- 27-hole de-duplication: IMPLEMENTED 2026-09-10 in db/migrations/003 (this
-- file predates it; the migration is the live definition). For clubs with
-- dedupe_courses = true the function collapses on
--     (club_id, tee_date, tee_time, holes)
-- keeping the cheapest row, then the alphabetically-first course name so the
-- choice is stable between runs. Holes is part of the key on purpose: Test
-- Valley sells a full 18 and its own front 9 off the same start times, and
-- those are different products. Clubs without the flag are untouched, so
-- West Malling keeps Spitfire and Hurricane as separate options.


-- ============================================================================
-- REFRESH RECIPE (run by each scrape; not executed here — reference only)
-- ----------------------------------------------------------------------------
-- Per (course, date), in ONE transaction so search never sees a half-sheet.
-- Only replace when the scrape SUCCEEDED — on 'ok' or a genuine 'empty' day.
-- On 'error', do NOT delete: stale-but-real data beats a blank sheet. The
-- scraper already tells these three apart (its ok / no-availability / error
-- logging), so n8n branches on that.
--
--   -- success ('ok' with rows, or 'empty' with none):
--   begin;
--     delete from tee_times where course_id = :course and tee_date = :date;
--     insert into tee_times (course_id, tee_date, tee_time, holes, prices, booking_url, checked_at)
--       values (:course, :date, :time, :holes, :prices::jsonb, :url, :checked_at), ... ;
--     update courses
--        set last_scraped_at = now(),
--            last_status = case when :n_rows > 0 then 'ok' else 'empty' end,
--            last_error  = null
--      where id = :course;
--   commit;
--
--   -- failure: touch only the status, leave tee_times untouched:
--   update courses set last_scraped_at = now(), last_status = 'error', last_error = :msg
--    where id = :course;
--
-- Nightly tidy — drop dates that are in the past:
--   delete from tee_times where tee_date < current_date;
--
-- Price/demand HISTORY (a deferred moat idea, not built): don't accumulate it
-- in this live table — it's meant to shrink to "now". When wanted, INSERT a
-- copy of each scrape into a separate append-only table (course_id, tee_date,
-- tee_time, prices, checked_at) and analyse that instead.
-- ============================================================================
