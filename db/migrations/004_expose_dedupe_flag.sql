-- 2026-09-10: let the page collapse a shared-nines club into ONE card.
--
-- Migration 003 stopped the same tee time being listed under several
-- combinations, but the club was still split across cards: Singing Hills went
-- from 6 cards to 3, because different surviving times carry different combo
-- names ("Valley/Lake" at 13:50, "Lake/River" at 09:20). The page groups on
-- club + course and has no way to know those are one course.
--
-- Returning the flag is all it needs: group on club alone when it is set.
-- Kept as a separate column rather than blanking course_name, so the row
-- still says which combination it came from if we ever want to show it.

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
    yardage     int,
    one_course  boolean      -- club sells one physical course as several sheets
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
               co.yardage,
               c.dedupe_courses
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
