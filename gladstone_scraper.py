"""
Gladstone Go (council / leisure-trust) golf tee sheet scraper.

Leisure trusts such as Mytime Active and TM Active run their golf courses on
Gladstone's booking system at {tenant}.gladstonego.cloud. The site is an
Angular app, but its API is open to anonymous users once you (a) start an
anonymous session and (b) send the one header the app adds to every call:

    GET {base_url}/api/samlauthentication/anonymous        -> sets a Jwt cookie
    GET {base_url}/api/availability/V2/sessions
        ?webBookableOnly=true&siteIds={SITE}&activityGroupIds={GROUP}
        &dateFrom=YYYY-MM-DDT00:00:00.000Z&dateTo=YYYY-MM-DDT23:59:59.000Z
      with header  X-Use-Sso: 1          (without it: 401 on everything)

Each tee time is its own "activity" in an activity group ("18 Holes",
"9 Holes - Ruxley"), with one slot carrying the spaces left:

    {"id": "HE8GO1806320326", "name": "18 Holes", "activityGroupId": "HEG18H",
     "siteId": "HEG", "date": "2026-09-10",
     "capacity": {"maxInCentreBookees": 4},
     "locations": [{"slots": [{"startTime": "2026-09-10T06:32:00Z",
                               "availability": {"inCentre": 3}, "status": "Available"}]}]}

Notes that shaped the parser:
  - NO PRICE. Gladstone only prices a slot when it is leased into a basket,
    which holds the tee time for real customers — not something to do on a
    schedule. Rows carry the bookable party sizes with null prices, and the
    search page shows "price on club site". Agreed with Joe 2026-09-09.
  - Times are UTC in the API; we convert to Europe/London for the sheet.
  - Groups also contain named blocks (societies, seniors, lessons). Only
    activities named like "18 Holes" / "9 Hole(s)…" are open tee times.
  - Bookable party sizes: 1..availability.inCentre (capped at maxInCentreBookees).

CONFIG: platform = gladstone, base_url = https://{tenant}.gladstonego.cloud,
course_id = "SITE/GROUP" e.g. HEG/HEG18H (site id from /api/configuration/sites,
group id from /api/configuration/activity-groups). One config row per group,
because the 18- and 9-hole groups share tee times.

Usage:
    python gladstone_scraper.py clubs_config.csv --date 2026-09-12
"""

import argparse
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import golf_common as gc

log = gc.log
PLATFORM = "gladstone"
LONDON = ZoneInfo("Europe/London")
OPEN_TEE_TIME_RE = re.compile(r"^\s*(\d+)\s*holes?\b", re.I)

# tenant base_url -> True once the anonymous session is established this run.
_sessions_started: set[str] = set()


def _headers(base: str) -> dict:
    return {"Accept": "application/json", "X-Use-Sso": "1", "Referer": f"{base}/book"}


def _start_session(base: str, club: gc.ClubConfig, session) -> bool:
    if base in _sessions_started:
        return True
    resp = gc.fetch(session, club.club_name, "GET", f"{base}/api/samlauthentication/anonymous",
                    headers=_headers(base))
    if resp is None:
        return False
    _sessions_started.add(base)
    return True


def _local_hhmm(iso_utc: str) -> str | None:
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(LONDON).strftime("%H:%M")


def parse(payload, club: gc.ClubConfig, date_iso: str) -> tuple[list[gc.TeeTimeResult], str]:
    if not isinstance(payload, list):
        log.error(f"[{club.club_name}] Expected a list of sessions — got {type(payload).__name__}; "
                  f"check base_url / course_id (SITE/GROUP)")
        return [], "error"
    results: list[gc.TeeTimeResult] = []
    seen: set[str] = set()
    for a in payload:
        if a.get("date") != date_iso or not a.get("webBookable", True):
            continue
        m = OPEN_TEE_TIME_RE.match(a.get("name") or "")
        if not m:
            continue   # a society / seniors / lesson block, not an open tee time
        holes = m.group(1)
        cap = int((a.get("capacity") or {}).get("maxInCentreBookees") or 4)
        for loc in a.get("locations") or []:
            for sl in loc.get("slots") or []:
                if sl.get("status") != "Available":
                    continue
                spaces = int((sl.get("availability") or {}).get("inCentre") or 0)
                if spaces < 1:
                    continue
                t = _local_hhmm(sl.get("startTime") or "")
                if not t or t in seen:
                    continue
                seen.add(t)
                results.append(gc.TeeTimeResult(
                    club_name=club.club_name, platform=PLATFORM, date=date_iso, time=t, holes=holes,
                    # Bookable sizes with no price (see docstring) — null, never 0.
                    prices_by_players={str(n): None for n in range(1, min(cap, spaces) + 1)},
                    booking_url=f"{club.base_url.rstrip('/')}/book/calendar/{a.get('id')}",
                ))
    results.sort(key=lambda r: r.time)
    return results, "ok" if results else "empty"


def scrape_club(club: gc.ClubConfig, date_iso: str, session) -> tuple[list[gc.TeeTimeResult], str]:
    base = club.base_url.rstrip("/")
    if "/" not in club.course_id:
        log.error(f"[{club.club_name}] course_id must be SITE/GROUP (e.g. HEG/HEG18H), got {club.course_id!r}")
        return [], "error"
    site, group = club.course_id.split("/", 1)
    if not _start_session(base, club, session):
        return [], "error"
    url = (f"{base}/api/availability/V2/sessions?webBookableOnly=true&siteIds={site}"
           f"&activityGroupIds={group}&dateFrom={date_iso}T00:00:00.000Z&dateTo={date_iso}T23:59:59.000Z")
    resp = gc.fetch(session, club.club_name, "GET", url, headers=_headers(base))
    if resp is None:
        return [], "error"
    try:
        payload = resp.json()
    except ValueError:
        log.error(f"[{club.club_name}] Sessions response wasn't JSON")
        return [], "error"
    try:
        results, status = parse(payload, club, date_iso)
        if results:
            log.info(f"[{club.club_name}] Found {len(results)} available tee time(s) for {date_iso} (no prices — Gladstone)")
        elif status == "empty":
            log.info(f"[{club.club_name}] No availability on {date_iso} — nothing to scrape")
        return results, status
    except Exception as e:
        log.error(f"[{club.club_name}] Parsing failed: {e}")
        return [], "error"


if __name__ == "__main__":
    gc.setup_logging()
    ap = argparse.ArgumentParser(description="Scrape Gladstone Go golf tee sheets")
    ap.add_argument("config")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--output", default="tee_times_gladstone.json")
    a = ap.parse_args()
    gc.run_platform(scrape_club, PLATFORM, a.config, a.date, a.output)
