"""
Load clubs_config.csv into the `clubs` and `courses` tables.

Splits the config's flat rows into the normalised model: a multi-course row
like "West Malling (Spitfire)" becomes club "West Malling" + course
"Spitfire"; a single-course club's course takes the club's own name. Location
(postcode/lat/long) attaches to the club. Idempotent — safe to re-run when
you add clubs; existing rows are updated in place.

Rows with no base_url (clubs we can't scrape — login wall, no online booking)
are skipped and listed, so they don't become empty club rows.

Usage:
    python load_config_to_db.py clubs_config.csv --dry-run   # print plan, no DB
    python load_config_to_db.py clubs_config.csv             # needs DATABASE_URL
"""

import argparse
import csv
import logging
import re
import sys

import golf_common
import golf_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("golf")

# 27-hole clubs whose course combinations share nines, so the same physical
# slot appears on more than one course sheet. Flagged on the club so the read
# layer can collapse those duplicates (see search_tee_times in schema.sql).
# Provisional — the shared slots being the same booking is inferred, not
# confirmed with the club.
SHARED_NINES_CLUBS = {
    "The Heron",              # Heron/Hawk/Vixen -> 3 combos
    "Singing Hills Golf Course",  # Lake/River/Valley -> 6 combos, each slot on 2 sheets
    "Stonelees Golf Centre",  # Executive + Heights + a 9/9 combo of both
    "Cams Hall Estate Golf Course",  # Creek + Park nines -> 5 combos
    "Weybrook Park Golf Club",       # East + West nines -> 3 combos
    "Test Valley Golf Club",         # full 18 + its own front 9
    "Royal Mid-Surrey Golf Club",    # Pam Barton + J H Taylor + a Composite of both
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def split_club_course(config_name: str) -> tuple[str, str]:
    """'West Malling (Spitfire)' -> ('West Malling', 'Spitfire'); 'The Ridge' -> ('The Ridge','The Ridge')."""
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", config_name)
    return (m.group(1).strip(), m.group(2).strip()) if m else (config_name.strip(), config_name.strip())


PROFILES_PATH = "course_profiles.csv"


def load_profiles(path: str = PROFILES_PATH) -> dict:
    """config club_name -> {course_type, yardage}, from the hand-checked CSV.

    Optional and allowed to be partial: a missing file or a blank cell means
    "leave whatever the DB already has" (see golf_db.upsert_course), never
    "blank it".
    """
    profiles: dict[str, dict] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = (row.get("club_name") or "").strip()
                if not name:
                    continue
                y = (row.get("yardage") or "").strip()
                profiles[name] = {
                    "course_type": (row.get("course_type") or "").strip() or None,
                    "yardage": int(y) if y.isdigit() else None,
                }
    except FileNotFoundError:
        log.info(f"No {path} — course type/yardage left as they are")
    return profiles


def build_clubs(config_path: str) -> tuple[dict, list[str]]:
    """Group config rows into {base_name: club-with-courses}. Returns (clubs, skipped_names)."""
    clubs: dict[str, dict] = {}
    skipped: list[str] = []
    profiles = load_profiles()
    with open(config_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("club_name") or "").strip()
            if not (row.get("base_url") or "").strip():
                skipped.append(name or "(unnamed)")
                continue
            base, course = split_club_course(name)
            club = clubs.setdefault(base, {
                "slug": slugify(base), "name": base, "postcode": "",
                "latitude": None, "longitude": None,
                "dedupe_courses": base in SHARED_NINES_CLUBS, "courses": [],
            })
            if not club["postcode"] and row.get("postcode"):
                club["postcode"] = row["postcode"].strip()
            if club["latitude"] is None and row.get("latitude"):
                club["latitude"] = float(row["latitude"])
                club["longitude"] = float(row["longitude"])
            prof = profiles.get(name, {})
            club["courses"].append({
                "name": course,
                "platform": row["platform"].strip(),
                "base_url": row["base_url"].strip(),
                "course_ref": (row.get("course_id") or "").strip(),
                "scrape_enabled": golf_common.is_enabled(row),
                "course_type": prof.get("course_type"),
                "yardage": prof.get("yardage"),
            })
    return clubs, skipped


def print_plan(clubs: dict, skipped: list[str]) -> None:
    print(f"\n{len(clubs)} club(s), {sum(len(c['courses']) for c in clubs.values())} course(s):\n")
    for base, c in clubs.items():
        geo = f"{c['latitude']},{c['longitude']}" if c["latitude"] is not None else "NOT GEOCODED"
        flag = "  [dedupe_courses]" if c["dedupe_courses"] else ""
        print(f"  {c['name']}  ({c['postcode'] or '—'}, {geo}){flag}")
        for co in c["courses"]:
            prof = " ".join(x for x in (co.get("course_type") or "",
                                        f"{co['yardage']}yds" if co.get("yardage") else "") if x)
            print(f"      - {co['name']:22} {co['platform']:16} ref={co['course_ref']!r:8} {prof}")
    if skipped:
        print(f"\nskipped (no base_url, not scraped): {', '.join(skipped)}")


def load(clubs: dict) -> None:
    """One transaction PER CLUB. The first version wrapped the whole sync in
    one transaction, so when the DB rejected two new platforms (a CHECK
    constraint on courses.platform, 2026-09-09) it silently rolled back every
    other new club too — a whole Surrey batch scraped fine and then had no
    course rows to write to. Now a bad club is logged and skipped, the rest
    go live."""
    n_clubs = n_courses = 0
    failed: list[str] = []
    with golf_db.get_connection() as conn:
        for c in clubs.values():
            try:
                with conn.transaction(), conn.cursor() as cur:
                    club_id = golf_db.upsert_club(
                        cur, slug=c["slug"], name=c["name"], postcode=c["postcode"],
                        latitude=c["latitude"], longitude=c["longitude"],
                        dedupe_courses=c["dedupe_courses"],
                    )
                    for co in c["courses"]:
                        golf_db.upsert_course(
                            cur, club_id=club_id, name=co["name"], platform=co["platform"],
                            base_url=co["base_url"], course_ref=co["course_ref"],
                            scrape_enabled=co["scrape_enabled"],
                            course_type=co.get("course_type"), yardage=co.get("yardage"),
                        )
                n_clubs += 1
                n_courses += len(c["courses"])
            except Exception as e:
                failed.append(c["name"])
                log.error(f"config -> DB: skipped club {c['name']!r}: {e}")
    log.info(f"Upserted {n_clubs} club(s) and {n_courses} course(s)"
             + (f"; {len(failed)} club(s) skipped: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Load club config into clubs/courses tables")
    ap.add_argument("config")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, don't touch the DB")
    a = ap.parse_args()

    clubs, skipped = build_clubs(a.config)
    if a.dry_run:
        print_plan(clubs, skipped)
        print("\n(dry run — nothing written)")
        sys.exit(0)
    if skipped:
        log.info(f"Skipping {len(skipped)} row(s) with no base_url: {', '.join(skipped)}")
    load(clubs)
