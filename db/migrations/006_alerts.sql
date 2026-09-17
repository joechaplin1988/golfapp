-- 2026-09-17: tee-time alerts. A golfer saves a search and is emailed when
-- tee times matching it appear.
--
-- PRIVACY: this is the first table holding personal data (an email address
-- tied to where someone plays). So:
--   - RLS is on with NO policies. The anon key the website uses can neither
--     read nor write these tables. Only server-side code with the service
--     role (the Netlify functions) or the database connection (send_alerts.py
--     in GitHub Actions) touches them.
--   - Double opt-in: an alert does nothing until the link in its first email
--     is used. Unconfirmed alerts are deleted after 48 hours.
--   - Unsubscribing DELETES the alert, and with it the email address and the
--     record of what was sent. Alerts expire after 90 days on their own.
--   - The location is stored as coordinates plus the place name the person
--     searched, never a full postcode.

create table if not exists alerts (
    id            uuid primary key default gen_random_uuid(),
    -- The only secret: it goes out in emails and nowhere else, and both
    -- confirming and unsubscribing need it.
    token         uuid not null unique default gen_random_uuid(),
    email         text not null check (length(email) <= 254
                                       and email ~* '^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$'),
    place         text check (place is null or length(place) <= 80),
    lat           double precision not null check (lat between 49 and 61),
    lng           double precision not null check (lng between -9 and 3),
    radius_km     numeric not null check (radius_km between 1 and 81),
    -- ISO weekdays, 1 = Monday .. 7 = Sunday. Empty means any day.
    days          smallint[] not null default '{}'
                  check (days <@ array[1,2,3,4,5,6,7]::smallint[]),
    time_from     time not null default '06:00',
    time_to       time not null default '20:00',
    players       int not null check (players between 1 and 4),
    max_price     numeric check (max_price is null or max_price >= 0),
    holes         smallint check (holes is null or holes in (9, 18)),
    created_at    timestamptz not null default now(),
    confirmed_at  timestamptz,
    last_sent_at  timestamptz,
    expires_at    timestamptz not null default now() + interval '90 days',
    check (time_from < time_to)
);
create index if not exists alerts_email_idx on alerts (lower(email));
create index if not exists alerts_active_idx on alerts (confirmed_at) where confirmed_at is not null;

-- One row per tee time already emailed for an alert, so the same slot is
-- never sent twice. Keyed the way search_tee_times names a slot.
create table if not exists alert_sent (
    alert_id     uuid not null references alerts(id) on delete cascade,
    club_id      bigint not null,
    course_name  text not null,
    tee_date     date not null,
    tee_time     time not null,
    sent_at      timestamptz not null default now(),
    primary key (alert_id, club_id, course_name, tee_date, tee_time)
);

alter table alerts enable row level security;
alter table alert_sent enable row level security;
revoke all on alerts, alert_sent from anon, authenticated;
