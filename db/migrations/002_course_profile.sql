-- 2026-09-10: course character, for people who don't know the area.
-- The search page could only say "20 tee times from £45", which decides
-- nothing for a stranger. Type and length are what golfers actually choose
-- on. These are static facts, so they live on `courses` and are synced from
-- a hand-checked course_profiles.csv, not from the 2-hourly scrape.
alter table courses add column if not exists course_type text;
alter table courses add column if not exists yardage    int
    check (yardage is null or yardage between 1000 and 8000);

-- search_tee_times has to return them, and Postgres won't let CREATE OR
-- REPLACE change a function's OUT columns — it must be dropped first.
drop function if exists search_tee_times(double precision, double precision, double precision,
                                         date, int, numeric, int);

create function search_tee_times(
    in_lat        double precision,
    in_lng        double precision,
    in_radius_km  double precision,
    in_date       date,
    in_players    int     default null,
    in_max_price  numeric default null,
    in_holes      int     default null
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
    price       numeric,
    prices      jsonb,
    booking_url text,
    checked_at  timestamptz,
    course_type text,
    yardage     int
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
        t.checked_at,
        co.course_type,
        co.yardage
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
    -- ordinal positions: a SQL function body can't alias the RETURNS TABLE names
    order by 5, 10 nulls last, 7;
$$;

grant execute on function search_tee_times to anon, authenticated;
