-- 2026-09-10: collapse the same physical tee time at 27-hole clubs.
--
-- A club built from three nines sells its 18 holes as several combinations,
-- and each combination has its own sheet. Measured on live data, Singing
-- Hills on 2026-09-12 returned 50 rows across 6 course cards for 20 real tee
-- times: 13:50 appeared as both "Valley/River £50" and "Valley/Lake £50".
-- In a Burgess Hill search that one club took 6 of the 13 cards on screen.
--
-- `clubs.dedupe_courses` has flagged these clubs since the schema was
-- written (The Heron, Singing Hills, Stonelees, and now Cams Hall, Weybrook
-- Park, Test Valley) but nothing acted on it, because changing the function
-- needed a psql session. db/migrations removed that blocker.
--
-- Collapsing on (club, date, time, HOLES) rather than (club, date, time) is
-- deliberate: Test Valley sells the full 18 and its own front 9 off the same
-- start times, and Cams Hall sells 18- and 9-hole combinations. Those are
-- genuinely different products at the same moment, so both survive; only
-- same-length duplicates are merged.
--
-- Clubs NOT flagged are untouched: the CASE makes course id part of the key
-- for them, so West Malling's Spitfire and Hurricane both stay.
--
-- Of the duplicates, the row kept is the cheapest for the requested party
-- size, then the alphabetically-first course name so the choice is stable
-- between runs rather than flickering as sheets are re-scraped.

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
    select * from (
        select distinct on (
                   c.id, t.tee_date, t.tee_time, t.holes,
                   case when c.dedupe_courses then null else co.id end
               )
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
        -- DISTINCT ON needs ORDER BY to lead with its own expressions; the
        -- two after them decide WHICH duplicate survives.
        order by c.id, t.tee_date, t.tee_time, t.holes,
                 case when c.dedupe_courses then null else co.id end,
                 (case when in_players is not null
                       then (t.prices ->> in_players::text)::numeric end) nulls last,
                 co.name
    ) d
    -- ordinal positions: a SQL function body can't alias the RETURNS TABLE names
    order by 5, 10 nulls last, 7;
$$;

grant execute on function search_tee_times to anon, authenticated;
