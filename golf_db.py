"""
Database access for the aggregator (Supabase / PostgreSQL).

Connection comes from the DATABASE_URL environment variable — Supabase's
connection string (Project Settings -> Database -> Connection string -> URI).
Never hardcode it; never commit it. The scrapers/loaders connect with the
service-role/database credentials, which bypass RLS so they can write.

This module owns the two write paths that must match db/schema.sql exactly:
  - upsert_club / upsert_course : idempotent config load
  - refresh_tee_times           : the replace-on-refresh recipe, per (course,date)
"""

import logging
import os
from typing import Optional

log = logging.getLogger("golf")

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # the loaders' --dry-run must work without the driver installed
    psycopg = None
    Jsonb = None


def get_connection(dsn: Optional[str] = None):
    if psycopg is None:
        raise RuntimeError("psycopg not installed — run:  pip install 'psycopg[binary]'")
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it to your Supabase connection string, e.g.\n"
            "  export DATABASE_URL='postgresql://postgres:<pw>@<host>:5432/postgres'   (bash)\n"
            "  $env:DATABASE_URL='postgresql://postgres:<pw>@<host>:5432/postgres'      (PowerShell)"
        )
    return psycopg.connect(dsn)


def upsert_club(cur, *, slug, name, postcode, latitude, longitude, dedupe_courses) -> int:
    cur.execute(
        """
        insert into clubs (slug, name, postcode, latitude, longitude, dedupe_courses)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (slug) do update set
            name = excluded.name,
            postcode = excluded.postcode,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            dedupe_courses = excluded.dedupe_courses
        returning id
        """,
        (slug, name, postcode or None, latitude, longitude, dedupe_courses),
    )
    return cur.fetchone()[0]


def upsert_course(cur, *, club_id, name, platform, base_url, course_ref, scrape_enabled,
                  course_type=None, yardage=None) -> int:
    """`course_type`/`yardage` come from the hand-checked course_profiles.csv.

    A blank there must NOT wipe a value already stored — coalesce keeps the
    existing one — so a half-filled profiles file can be committed safely.
    """
    cur.execute(
        """
        insert into courses (club_id, name, platform, base_url, course_ref, scrape_enabled,
                             course_type, yardage)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (platform, base_url, course_ref) do update set
            club_id = excluded.club_id,
            name = excluded.name,
            scrape_enabled = excluded.scrape_enabled,
            course_type = coalesce(excluded.course_type, courses.course_type),
            yardage = coalesce(excluded.yardage, courses.yardage)
        returning id
        """,
        (club_id, name, platform, base_url, course_ref or "", scrape_enabled,
         course_type or None, yardage),
    )
    return cur.fetchone()[0]


def record_scrape_run(cur, *, started_at, days, platforms, courses, ok, empty, error,
                      rows_written) -> None:
    """One row per scheduled run. `courses` already holds the LATEST status per
    sheet; this is the history, which is what answers "how often do you hit us?"
    if a club ever asks — and what shows a platform degrading over days rather
    than only in the run that finally fails."""
    cur.execute(
        """
        insert into scrape_runs (started_at, days, platforms, courses, ok, empty,
                                 error, rows_written)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (started_at, days, platforms, courses, ok, empty, error, rows_written),
    )


def refresh_tee_times(cur, *, platform, base_url, course_ref, date_iso, results, status,
                      error: Optional[str] = None) -> int:
    """Apply one scrape's outcome for one (course, date). Mirrors the recipe in schema.sql.

    `results` are plain dicts (as written to the tee_times_*.json output).
    Returns the number of tee-time rows written (0 for empty/error).

    On status 'error' the existing rows are LEFT ALONE — stale-but-real data
    beats a blank sheet — and only the course's status is updated.
    """
    cur.execute(
        "select id from courses where platform = %s and base_url = %s and course_ref = %s",
        (platform, base_url, course_ref or ""),
    )
    row = cur.fetchone()
    if row is None:
        raise LookupError(
            f"No course row for platform={platform} base_url={base_url} course_ref={course_ref!r}. "
            f"Run load_config_to_db.py first."
        )
    course_id = row[0]

    if status == "error":
        cur.execute(
            "update courses set last_scraped_at = now(), last_status = 'error', last_error = %s where id = %s",
            (error or "scrape error", course_id),
        )
        return 0

    # Success (ok or a genuine empty day): replace this sheet's slots atomically.
    cur.execute("delete from tee_times where course_id = %s and tee_date = %s", (course_id, date_iso))
    rows = [
        (course_id, date_iso, r["time"], int(r["holes"]),
         Jsonb(r["prices_by_players"]), r["booking_url"], r["checked_at"])
        for r in results
    ]
    if rows:
        cur.executemany(
            """
            insert into tee_times (course_id, tee_date, tee_time, holes, prices, booking_url, checked_at)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    cur.execute(
        "update courses set last_scraped_at = now(), last_status = %s, last_error = null where id = %s",
        ("ok" if rows else "empty", course_id),
    )
    return len(rows)
