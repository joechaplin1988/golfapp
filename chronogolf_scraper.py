"""
Chronogolf (Lightspeed Golf) tee sheet scraper.

Chronogolf hosts each club's booking widget at
https://www.chronogolf.com/club/{club_id}/widget. The widget is an Angular
app talking to an open marketplace API — no auth, no session, and no
reCAPTCHA on these endpoints (the original survey said reCAPTCHA; it said the
same about BRS and was wrong there too):

    GET /marketplace/clubs/{club_id}/courses
        -> [{"id": 22908, "name": "Manor Course", "holes": 18}, ...]
    GET /marketplace/organizations/{club_id}/affiliation_types
        -> the club's player types; the visitor one is what we price against
    GET /marketplace/clubs/{club_id}/teetimes
        ?date=YYYY-MM-DD&course_id={course}&nb_holes={holes}
        &affiliation_type_ids[]={aff}   (REPEATED ONCE PER PLAYER)
        -> [{"start_time": "08:30", "out_of_capacity": false,
             "restrictions": [], "green_fees": [{"green_fee": 75.0}, ...]}]

Notes that shaped the parser:
  - Party size is expressed by REPEATING affiliation_type_ids[], once per
    player. Asking for four players returns the same tee times with
    `out_of_capacity` set on any that can't seat four, so we ask 1..4 and
    record which sizes each time actually allows.
  - `green_fees` comes back with ONE ENTRY PER PLAYER, so the party total is
    the sum of that list. No multiplying: unlike ESP and Golf Manager, this
    platform hands us the real total for the party we asked about, which is
    also why a club with a discounted four-ball prices correctly for free.
  - `restrictions` is a list of human-readable reasons a slot is unavailable
    to the chosen player type ("The following players don't have the right to
    play at that time"). A slot with any restriction is not bookable.
  - The visitor affiliation type must be discovered PER CLUB, not assumed:
    at Bramshaw the type named "Visitors" yields 44 bookable slots while the
    one named "Public" yields none. probe_course_ids.py picks whichever
    public-role type actually produces availability, and stores its id.

CONFIG: platform = chronogolf,
        base_url  = https://www.chronogolf.com/club/{club_id},
        course_id = "{course_id}/{affiliation_type_id}".

Usage:
    python chronogolf_scraper.py clubs_config.csv --date 2026-09-12
"""

import argparse
import re
import time

import golf_common as gc

log = gc.log
PLATFORM = "chronogolf"
HOST = "https://www.chronogolf.com"
PARTY_SIZES = (1, 2, 3, 4)
# One sheet costs four requests (one per party size). golf_common only paces
# BETWEEN clubs, so without this they go out back-to-back and Chronogolf
# answers 429. Seen during the first probe run, which is exactly the kind of
# thing that would otherwise show up as a club with "no availability".
BETWEEN_PARTY_SIZES_SECONDS = 1.0

# club_id -> {course_id: holes}, filled once per club per run.
_course_holes: dict[str, dict] = {}


def _club_id(club: gc.ClubConfig) -> str:
    m = re.search(r"/club/(\d+)", club.base_url)
    return m.group(1) if m else ""


def _split_ref(course_id: str) -> tuple[str, str]:
    parts = (course_id or "").split("/")
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def course_holes(club_id: str, club: gc.ClubConfig, session) -> dict:
    """{course_id: holes} for a club. One cheap call, so the sheet's hole
    count comes from the platform rather than being assumed to be 18."""
    if club_id in _course_holes:
        return _course_holes[club_id]
    resp = gc.fetch(session, club.club_name, "GET", f"{HOST}/marketplace/clubs/{club_id}/courses",
                    headers={"Accept": "application/json", "Referer": f"{HOST}/club/{club_id}/widget"})
    holes = {}
    if resp is not None:
        try:
            holes = {str(c["id"]): int(c.get("holes") or 18) for c in resp.json()}
        except (ValueError, KeyError, TypeError):
            holes = {}
    _course_holes[club_id] = holes
    return holes


def teetimes_url(club_id: str, course: str, aff: str, holes: int, players: int) -> str:
    ids = "&".join(f"affiliation_type_ids%5B%5D={aff}" for _ in range(players))
    return (f"{HOST}/marketplace/clubs/{club_id}/teetimes"
            f"?date={{date}}&course_id={course}&nb_holes={holes}&{ids}")


def bookable(slot: dict) -> bool:
    return not slot.get("out_of_capacity") and not slot.get("restrictions")


def party_total(slot: dict) -> float | None:
    """Sum of the per-player green fees — the real total for this party size."""
    fees = slot.get("green_fees") or []
    total = 0.0
    for f in fees:
        v = f.get("green_fee")
        if v is None:
            return None
        total += float(v)
    return round(total, 2) if fees else None


def scrape_club(club: gc.ClubConfig, date_iso: str, session) -> tuple[list[gc.TeeTimeResult], str]:
    club_id = _club_id(club)
    course, aff = _split_ref(club.course_id)
    if not club_id or not course or not aff:
        log.error(f"[{club.club_name}] need base_url .../club/<id> and "
                  f"course_id '<course>/<affiliation>', got {club.base_url!r} / {club.course_id!r}")
        return [], "error"

    holes = course_holes(club_id, club, session).get(course, 18)
    headers = {"Accept": "application/json", "Referer": f"{HOST}/club/{club_id}/widget"}

    # time -> {"holes": n, "prices": {players: total}}
    found: dict[str, dict] = {}
    for i, n in enumerate(PARTY_SIZES):
        if i:
            time.sleep(BETWEEN_PARTY_SIZES_SECONDS)
        url = teetimes_url(club_id, course, aff, holes, n).format(date=date_iso)
        resp = gc.fetch(session, club.club_name, "GET", url, headers=headers)
        if resp is None:
            return [], "error"
        try:
            slots = resp.json()
        except ValueError:
            log.error(f"[{club.club_name}] tee-times response wasn't JSON")
            return [], "error"
        if isinstance(slots, dict):
            log.error(f"[{club.club_name}] API said: {slots.get('error', {}).get('message', slots)}")
            return [], "error"
        for s in slots:
            if not bookable(s):
                continue
            t = str(s.get("start_time") or "")[:5]
            if len(t) != 5:
                continue
            total = party_total(s)
            if total is None or total <= 0:
                continue
            entry = found.setdefault(t, {"holes": str(holes), "prices": {}})
            entry["prices"][str(n)] = total

    results = [
        gc.TeeTimeResult(
            club_name=club.club_name, platform=PLATFORM, date=date_iso, time=t,
            holes=found[t]["holes"], prices_by_players=found[t]["prices"],
            booking_url=f"{HOST}/club/{club_id}/widget?medium=widget&source=club",
        )
        for t in sorted(found) if found[t]["prices"]
    ]
    if results:
        log.info(f"[{club.club_name}] Found {len(results)} available tee time(s) for {date_iso}")
        return results, "ok"
    log.info(f"[{club.club_name}] No availability on {date_iso} — nothing to scrape")
    return results, "empty"


if __name__ == "__main__":
    gc.setup_logging()
    ap = argparse.ArgumentParser(description="Scrape Chronogolf tee sheets")
    ap.add_argument("config")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--output", default="tee_times_chronogolf.json")
    a = ap.parse_args()
    gc.run_platform(scrape_club, PLATFORM, a.config, a.date, a.output)
