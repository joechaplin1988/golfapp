-- 2026-09-22: show what the club says about the state of the course.
--
-- Tester: "nothing worse than when you pay a lot of money only for certain
-- aspects of the course to be under maintenance" — with a screenshot of a club
-- announcing four temporary greens on its own booking page. We were reading
-- that page anyway and throwing the line away.
--
-- Stored per course, not per tee time: the note is about the course, and
-- duplicating it onto every slot would be a lot of repeated text for nothing.
-- status_note_at is when WE last read it, so the page can say how fresh the
-- note is rather than implying it is live. A club that publishes nothing keeps
-- NULL and the card shows nothing — silence is never read as "course open".

alter table courses add column if not exists status_note    text;
alter table courses add column if not exists status_note_at timestamptz;

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
    club_id        bigint,
    club_name      text,
    course_name    text,
    platform       text,
    distance_km    double precision,
    tee_date       date,
    tee_time       time,
    holes          smallint,
    players        int,
    price          numeric,
    prices         jsonb,
    booking_url    text,
    checked_at     timestamptz,
    course_type    text,
    yardage        int,
    one_course     boolean,     -- club sells one physical course as several sheets
    status_note    text,        -- the club's own words about the course today
    status_note_at timestamptz  -- when we last read that line
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
               c.dedupe_courses,
               co.status_note,
               co.status_note_at
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
