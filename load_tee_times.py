"""
Load scraper output into the `tee_times` table, applying the replace-on-
refresh recipe per (course, date).

Reads, for each platform, the two files a scraper writes:
  - tee_times_<platform>.json : the slot records
  - run_<platform>.json       : the manifest (which course got which status)

The manifest is what makes the refresh correct: for each course scraped it
runs delete-then-insert (so slots booked since the last run disappear), a
genuine 'empty' day clears the sheet, and an 'error' leaves the existing rows
untouched. Everything for one course/date happens in one transaction.

Run the scrapers first (they produce both files), then this.

Usage:
    python load_tee_times.py --dry-run                      # print plan, no DB
    python load_tee_times.py                                # all four platforms; needs DATABASE_URL
    python load_tee_times.py --platforms esp golf_manager   # a subset
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import golf_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("golf")

ALL_PLATFORMS = ["intelligent_golf", "esp", "golf_manager", "clubv1"]


def load_platform_files(platform: str):
    """Return (manifest, results_by_club) or (None, None) if the files aren't there."""
    man_path = Path(f"run_{platform}.json")
    res_path = Path(f"tee_times_{platform}.json")
    if not man_path.exists() or not res_path.exists():
        log.warning(f"[{platform}] missing {man_path.name} or {res_path.name} — run the scraper first; skipping")
        return None, None
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    results = json.loads(res_path.read_text(encoding="utf-8"))
    by_club: dict[str, list] = {}
    for r in results:
        by_club.setdefault(r["club_name"], []).append(r)
    return manifest, by_club


def plan_for(platform: str):
    manifest, by_club = load_platform_files(platform)
    if manifest is None:
        return []
    date_iso = manifest["date"]
    plan = []
    for course in manifest["courses"]:
        rows = by_club.get(course["club_name"], [])
        if course["status"] == "error":
            action = "leave existing rows, mark error"
        else:
            action = f"replace with {len(rows)} row(s)" + (" (clears sheet)" if not rows else "")
        plan.append((date_iso, course, rows, action))
    return plan


def print_plan(platforms) -> None:
    for platform in platforms:
        plan = plan_for(platform)
        if not plan:
            continue
        print(f"\n{platform} (date {plan[0][0]}):")
        for _date, course, _rows, action in plan:
            print(f"  {course['status']:6} {course['club_name']:28} -> {action}")


def load(platforms) -> None:
    total = 0
    with golf_db.get_connection() as conn:
        for platform in platforms:
            plan = plan_for(platform)
            for date_iso, course, rows, _action in plan:
                # One transaction per course/date, so a failure can't leave a
                # half-written sheet visible to search.
                with conn.transaction(), conn.cursor() as cur:
                    n = golf_db.refresh_tee_times(
                        cur, platform=course["platform"], base_url=course["base_url"],
                        course_ref=course["course_ref"], date_iso=date_iso,
                        results=rows, status=course["status"],
                    )
                total += n
            log.info(f"[{platform}] applied {len(plan)} course refresh(es)")
    log.info(f"Wrote {total} tee-time row(s) total")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Load scraper output into tee_times")
    ap.add_argument("--platforms", nargs="+", default=ALL_PLATFORMS, choices=ALL_PLATFORMS)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, don't touch the DB")
    a = ap.parse_args()

    if a.dry_run:
        print_plan(a.platforms)
        print("\n(dry run — nothing written)")
        sys.exit(0)
    load(a.platforms)
