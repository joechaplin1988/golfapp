"""
One-command pipeline: scrape every platform for a window of upcoming dates and
write straight into Supabase. This is the single entry point n8n calls on a
cron (see n8n/golf_pipeline.workflow.json).

For each date in the window, for each platform, for each configured course:
scrape it and apply the replace-on-refresh recipe (golf_db.refresh_tee_times)
in its own transaction. Booked slots vanish, empty sheets clear, errored
sheets keep their last-good rows — all per (course, date).

Unlike running the individual scrapers, this writes to the DB directly and
skips the JSON files (those stay for manual/debug runs). One scraper failing a
club, or one club erroring, never stops the rest.

Platforms run in parallel, one thread each, so wall time is roughly the
slowest platform rather than the sum. Each thread keeps the same per-club
pacing as before and its own session, so no single club (or shared host) is
hit any faster — only the platforms overlap. All DB writes happen on the main
thread, in arrival order, so the connection is never shared.

Environment: DATABASE_URL (Supabase connection string) — unless --no-db.

Usage:
    python run_pipeline.py --days 3            # today .. today+2, all platforms -> DB
    python run_pipeline.py --days 1 --no-db    # scrape only, print a summary, no DB (no DATABASE_URL needed)
    python run_pipeline.py --days 3 --platforms esp golf_manager
"""

import argparse
import dataclasses
import logging
import queue
import threading
from datetime import date, datetime, timedelta, timezone

import golf_common as gc
import golf_db

import intelligent_golf_scraper
import esp_scraper
import golf_manager_scraper
import clubv1_scraper
import brs_scraper
import shiji_scraper
import gladstone_scraper
import chronogolf_scraper

gc.setup_logging()
log = gc.log

SCRAPE_FNS = {
    "intelligent_golf": intelligent_golf_scraper.scrape_club,
    "esp": esp_scraper.scrape_club,
    "golf_manager": golf_manager_scraper.scrape_club,
    "clubv1": clubv1_scraper.scrape_club,
    "brs": brs_scraper.scrape_club,
    "shiji": shiji_scraper.scrape_club,
    "gladstone": gladstone_scraper.scrape_club,
    "chronogolf": chronogolf_scraper.scrape_club,
}


# London and the Home Counties, by postcode area: where the MVP launches. Testers
# judge us on whether times are current, so the schedule refreshes these clubs
# every two hours and the rest of England less often. Hampshire's coast (PO,
# SO) is deliberately outside; add areas here to widen the launch.
LAUNCH_POSTCODE_AREAS = frozenset({
    "E", "EC", "N", "NW", "SE", "SW", "W", "WC",                  # London
    "BR", "CR", "DA", "EN", "HA", "IG", "KT", "RM", "SM", "TW", "UB", "WD",
    "CT", "ME", "TN",                                              # Kent
    "BN", "RH", "GU",                                              # Sussex, Surrey
    "CM", "CO", "SS",                                              # Essex
    "AL", "SG", "LU", "HP", "MK",                                  # Herts, Beds, Bucks
    "SL", "RG", "OX",                                              # Berks, Oxon
})


def in_launch_area(club: gc.ClubConfig) -> bool:
    pc = club.postcode.strip().upper()
    return (pc[:2] if pc[:2].isalpha() else pc[:1]) in LAUNCH_POSTCODE_AREAS


# How stale each kind of run may get before a scheduled slot does it instead of
# the launch-area refresh. GitHub's scheduler delays and drops runs for this
# repository (2026-09-13: the overnight full run never fired, and the launch
# slots that did fire were 35 minutes to 2 hours late). So scheduled runs don't
# trust WHICH cron fired; each asks the run history what is most overdue.
FULL_RUN_MAX_AGE = timedelta(hours=24)
ALL_ENGLAND_MAX_AGE = timedelta(hours=12)


def decide(last_full, last_all_england, now):
    """(days, area, reason) for a scheduled run. Pure, so it can be tested
    without a database: pass the finished_at of the latest all-England 7-day
    run and the latest all-England run of any length, or None for never."""
    if last_full is None or now - last_full > FULL_RUN_MAX_AGE:
        age = "never" if last_full is None else f"{(now - last_full).total_seconds() / 3600:.1f}h ago"
        return 7, "all", f"last full 7-day run {age}"
    if last_all_england is None or now - last_all_england > ALL_ENGLAND_MAX_AGE:
        age = "never" if last_all_england is None else f"{(now - last_all_england).total_seconds() / 3600:.1f}h ago"
        return 3, "all", f"last all-England run {age}"
    return 3, "launch", "full and all-England runs are both recent enough"


def decide_from_history():
    """Read scrape_runs and decide. A run that dies before recording itself
    leaves no row, so the next slot simply tries again: dropped or killed runs
    delay the work, they never lose it."""
    with golf_db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select
                max(finished_at) filter (where days >= 7),
                max(finished_at)
            from scrape_runs
            where platforms not like '%[launch area]%'
            """
        )
        last_full, last_all = cur.fetchone()
    return decide(last_full, last_all, datetime.now(timezone.utc))


def date_window(days: int, start: str | None) -> list[str]:
    d0 = date.fromisoformat(start) if start else date.today()
    return [(d0 + timedelta(days=i)).isoformat() for i in range(days)]


def run(config_path: str, platforms: list[str], dates: list[str], to_db: bool,
        area: str = "all") -> None:
    started_at = datetime.now(timezone.utc)
    log.info(f"Pipeline: {len(platforms)} platform(s) x {len(dates)} date(s) "
             f"({dates[0]}..{dates[-1]}), area={area}{' -> DB' if to_db else ' (no DB)'}")
    if to_db:
        # Keep the DB's clubs/courses in step with clubs_config.csv on every
        # run, so a club added to the CSV and pushed goes live on the next
        # scheduled run — no separate, credentialed loader step. Without this,
        # refresh_tee_times raises "No course row" for any club the loader
        # hasn't seen, and the scheduled job has no way to run the loader.
        # Idempotent upserts; a few dozen rows, negligible cost per run.
        try:
            import load_config_to_db
            clubs_cfg, _skipped = load_config_to_db.build_clubs(config_path)
            load_config_to_db.load(clubs_cfg)
        except Exception as e:
            # Non-fatal by design: a sync failure must never stop the refresh
            # of clubs the DB already knows. Any NEW club then logs "No course
            # row" below and is skipped until the sync succeeds.
            log.error(f"config -> DB sync failed (continuing with existing courses): {e}")

    work = {p: gc.load_clubs(config_path, platform=p) for p in platforms}
    if area == "launch":
        # Filter only what gets SCRAPED. The sync above still ran on the whole
        # config, so clubs outside the launch area stay in the DB untouched
        # and keep their tee times until the next all-England run.
        work = {p: [c for c in clubs if in_launch_area(c)] for p, clubs in work.items()}
        log.info(f"Launch area only: {sum(len(c) for c in work.values())} course(s) "
                 f"in London and the Home Counties")
    work = {p: c for p, c in work.items() if c}
    q: queue.Queue = queue.Queue()

    def scrape_platform(platform: str, clubs: list) -> None:
        session = gc.make_session()
        log.info(f"[{platform}] {len(clubs)} course(s)")
        for date_iso in dates:
            for club in clubs:
                try:
                    results, status = SCRAPE_FNS[platform](club, date_iso, session)
                except Exception as e:  # a scrape blowing up must not stop the run
                    log.error(f"[{club.club_name}] {date_iso} scrape raised: {e}")
                    results, status = [], "error"
                q.put((platform, club, date_iso, results, status))
                gc.pause_between_clubs()
        q.put((platform, None, None, None, None))  # this platform is finished

    threads = [threading.Thread(target=scrape_platform, args=(p, c), daemon=True, name=p)
               for p, c in work.items()]
    for t in threads:
        t.start()

    conn = golf_db.get_connection() if to_db else None
    tally = {"ok": 0, "empty": 0, "error": 0, "rows": 0}
    try:
        remaining = len(threads)
        while remaining:
            platform, club, date_iso, results, status = q.get()
            if club is None:
                remaining -= 1
                continue
            tally[status] = tally.get(status, 0) + 1
            if to_db:
                try:
                    with conn.transaction(), conn.cursor() as cur:
                        n = golf_db.refresh_tee_times(
                            cur, platform=platform, base_url=club.base_url,
                            course_ref=club.course_id, date_iso=date_iso,
                            results=[dataclasses.asdict(r) for r in results],
                            status=status,
                        )
                    tally["rows"] += n
                except Exception as e:
                    log.error(f"[{club.club_name}] {date_iso} DB write failed: {e}")
            else:
                tally["rows"] += len(results)
    finally:
        if conn is not None:
            conn.close()

    log.info(f"Done: {tally['ok']} ok, {tally['empty']} empty, {tally['error']} error; "
             f"{tally['rows']} tee-time row(s) {'written' if to_db else 'found'}")

    if to_db:
        # Its own connection: the one above is closed, and a failure to record
        # the run must never be mistaken for a failure of the run itself.
        try:
            with golf_db.get_connection() as c2:
                with c2.cursor() as cur:
                    golf_db.record_scrape_run(
                        cur, started_at=started_at, days=len(dates),
                        platforms=",".join(sorted(work)) + (" [launch area]" if area == "launch" else ""),
                        courses=sum(len(v) for v in work.values()) * len(dates),
                        ok=tally["ok"], empty=tally["empty"], error=tally["error"],
                        rows_written=tally["rows"],
                    )
                c2.commit()
        except Exception as e:
            log.warning(f"couldn't record this run in scrape_runs (scrape itself was fine): {e}")
    # Non-zero exit if EVERY course errored — lets n8n alert on a total failure
    # (e.g. site-wide block or bad DATABASE_URL) without crying over one club.
    if tally["error"] and tally["ok"] == 0 and tally["empty"] == 0:
        raise SystemExit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape all platforms for a date window into Supabase")
    ap.add_argument("--config", default="clubs_config.csv")
    ap.add_argument("--days", type=int, default=7, help="number of days from --start (default 7)")
    ap.add_argument("--start", default=None, help="first date YYYY-MM-DD (default: today)")
    ap.add_argument("--platforms", nargs="+", default=list(SCRAPE_FNS), choices=list(SCRAPE_FNS))
    ap.add_argument("--no-db", action="store_true", help="scrape and summarise only; don't write to the DB")
    ap.add_argument("--area", choices=("all", "launch", "auto"), default="all",
                    help="launch = London and the Home Counties only (see LAUNCH_POSTCODE_AREAS); "
                         "auto = do whatever the run history says is most overdue, ignoring --days")
    a = ap.parse_args()
    days, area = a.days, a.area
    if area == "auto":
        if a.no_db:
            raise SystemExit("--area auto reads the run history, so it needs the database (drop --no-db)")
        days, area, reason = decide_from_history()
        log.info(f"Auto: {days} day(s), area={area} ({reason})")
    run(a.config, a.platforms, date_window(days, a.start), to_db=not a.no_db, area=area)
