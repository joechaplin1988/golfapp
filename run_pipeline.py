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
from datetime import date, timedelta

import golf_common as gc
import golf_db

import intelligent_golf_scraper
import esp_scraper
import golf_manager_scraper
import clubv1_scraper
import brs_scraper
import shiji_scraper
import gladstone_scraper

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
}


def date_window(days: int, start: str | None) -> list[str]:
    d0 = date.fromisoformat(start) if start else date.today()
    return [(d0 + timedelta(days=i)).isoformat() for i in range(days)]


def run(config_path: str, platforms: list[str], dates: list[str], to_db: bool) -> None:
    log.info(f"Pipeline: {len(platforms)} platform(s) x {len(dates)} date(s) "
             f"({dates[0]}..{dates[-1]}){' -> DB' if to_db else ' (no DB)'}")
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
    a = ap.parse_args()
    run(a.config, a.platforms, date_window(a.days, a.start), to_db=not a.no_db)
