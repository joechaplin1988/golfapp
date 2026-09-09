"""
Shiji (Concept Golf) hotel-resort tee sheet scraper.

Hotel groups run their visitor booking on Shiji's white-label app at a host
like bookteetime.dalehill.co.uk or book.eastsussexnational.co.uk. The page is
React, but it embeds the API it talks to and the tee sheet is one open JSON
call — no login, no session, no special headers (confirmed 2026-09-09 on
Dale Hill and East Sussex National):

    GET {base_url}/golf?course={course_id}
        -> window.__API_ENDPOINT__   e.g. https://dalehill-api-live.shiji.aws.prop.cm/api/v1
        -> window.__PRELOADED_STATE__.booking.golf.courseId   e.g. 44003-201-0000000003
    GET {api}/course/{apiCourseId}/availability/{YYYY-MM-DD}?players={n}&holes=18
        -> {"slots": {<slotId>: {<innerId>: {"time": "15:40:00", "players": 2,
                       "maxPlayers": 4, "prices": {"guest": 4500, ...}, ...}}}}

Notes that shaped the parser:
  - `prices.guest` is PENCE, per PLAYER, and does not change with party size
    (Dale Hill Saturday: 4500 for 1, 2, 3 and 4 players). So total = n x fee,
    verified — the same safe-multiply case as ESP and Golf Manager.
  - A request for `players=n` only returns slots with room for n. We ask for
    1..4 and record which sizes each time came back under, so
    prices_by_players keys are the sizes actually bookable.
  - `holes=9` is a 400 on these courses: 18 only.
  - The page's course code (C02, ESN, ESW) is what the booking URL wants; the
    API wants the long id, which the page reveals when loaded with ?course=.
    We fetch that page once per club per run and cache the mapping.

CONFIG: platform = shiji, base_url = the booking site origin
(https://bookteetime.dalehill.co.uk), course_id = the page's course code.

Usage:
    python shiji_scraper.py clubs_config.csv --date 2026-09-12
"""

import argparse
import json
import re

import golf_common as gc

log = gc.log
PLATFORM = "shiji"
PARTY_SIZES = (1, 2, 3, 4)

# (base_url, course_code) -> (api_endpoint, api_course_id), filled per run.
_course_cache: dict[tuple[str, str], tuple[str, str]] = {}


def _preloaded_state(html: str) -> dict:
    """The page embeds `window.__PRELOADED_STATE__ = {...};` — brace-match it out."""
    marker = "window.__PRELOADED_STATE__ = "
    i = html.find(marker)
    if i < 0:
        return {}
    i += len(marker)
    depth = 0
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[i:j + 1])
                except ValueError:
                    return {}
    return {}


def resolve_course(club: gc.ClubConfig, session) -> tuple[str, str] | None:
    key = (club.base_url.rstrip("/"), club.course_id)
    if key in _course_cache:
        return _course_cache[key]
    resp = gc.fetch(session, club.club_name, "GET", f"{key[0]}/golf?course={club.course_id}")
    if resp is None:
        return None
    m = re.search(r'window\.__API_ENDPOINT__\s*=\s*"([^"]+)"', resp.text)
    api = m.group(1).encode().decode("unicode_escape") if m else ""
    golf = (_preloaded_state(resp.text).get("booking") or {}).get("golf") or {}
    api_course = golf.get("courseId") or ""
    if not api or not api_course:
        log.error(f"[{club.club_name}] Booking page didn't expose the API endpoint / course id — "
                  f"check base_url is the Shiji booking site and course_id is its course code")
        return None
    _course_cache[key] = (api.rstrip("/"), api_course)
    return _course_cache[key]


def _flatten(payload: dict) -> list[dict]:
    out = []
    slots = payload.get("slots") if isinstance(payload, dict) else None
    for outer in (slots or {}).values() if isinstance(slots, dict) else []:
        for inner in outer.values() if isinstance(outer, dict) else []:
            if isinstance(inner, dict) and inner.get("time"):
                out.append(inner)
    return out


def scrape_club(club: gc.ClubConfig, date_iso: str, session) -> tuple[list[gc.TeeTimeResult], str]:
    resolved = resolve_course(club, session)
    if resolved is None:
        return [], "error"
    api, api_course = resolved

    # time -> {"fee": pence, "sizes": {n, ...}}
    by_time: dict[str, dict] = {}
    for n in PARTY_SIZES:
        resp = gc.fetch(session, club.club_name, "GET",
                        f"{api}/course/{api_course}/availability/{date_iso}?players={n}&holes=18",
                        headers={"Accept": "application/json"})
        if resp is None:
            return [], "error"
        try:
            payload = resp.json()
        except ValueError:
            log.error(f"[{club.club_name}] Availability response wasn't JSON")
            return [], "error"
        if payload.get("error"):
            log.error(f"[{club.club_name}] API error for {date_iso}: {payload.get('message') or payload['error']}")
            return [], "error"
        for s in _flatten(payload):
            if s.get("display") is False:
                continue
            fee = (s.get("prices") or {}).get("guest")
            if fee is None:
                continue
            t = str(s["time"])[:5]
            entry = by_time.setdefault(t, {"fee": int(fee), "sizes": set()})
            entry["sizes"].add(n)

    results = []
    for t in sorted(by_time):
        e = by_time[t]
        if e["fee"] <= 0:
            continue
        per_player = e["fee"] / 100.0
        results.append(gc.TeeTimeResult(
            club_name=club.club_name, platform=PLATFORM, date=date_iso, time=t, holes="18",
            prices_by_players={str(n): round(n * per_player, 2) for n in sorted(e["sizes"])},
            booking_url=f"{club.base_url.rstrip('/')}/golf?course={club.course_id}&date={date_iso}&holes=18",
        ))
    if results:
        log.info(f"[{club.club_name}] Found {len(results)} available tee time(s) for {date_iso}")
        return results, "ok"
    log.info(f"[{club.club_name}] No availability on {date_iso} — nothing to scrape")
    return results, "empty"


if __name__ == "__main__":
    gc.setup_logging()
    ap = argparse.ArgumentParser(description="Scrape Shiji hotel-resort tee sheets")
    ap.add_argument("config")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--output", default="tee_times_shiji.json")
    a = ap.parse_args()
    gc.run_platform(scrape_club, PLATFORM, a.config, a.date, a.output)
