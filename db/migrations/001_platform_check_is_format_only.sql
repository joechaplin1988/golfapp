-- 2026-09-09: courses.platform had a CHECK listing every allowed platform.
-- That list duplicated golf_common.PLATFORMS and drifted: adding the `shiji`
-- and `gladstone` scrapers made the config->DB sync fail, which (before the
-- per-club commit fix) rolled back a whole Surrey batch, so 49 courses
-- scraped fine and then had nowhere to write. The scrapers are the authority
-- on platform names; the DB only needs to reject junk.
alter table courses drop constraint if exists courses_platform_check;

alter table courses add constraint courses_platform_check
    check (platform ~ '^[a-z][a-z0-9_]{2,31}$');
