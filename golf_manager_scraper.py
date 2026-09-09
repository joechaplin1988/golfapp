"""
Golf Manager tee sheet scraper.

Golf Manager clubs live at {slug}.golfmanager.com and expose a clean, no-auth
JSON API — the easiest platform of the lot. The consumer booking page is a
SPA, but it just calls this endpoint, which we can hit directly:

    GET {base_url}/ebookings/init.api?start=YYYY-MM-DDT00:00:00

Response (confirmed against Redlibbets and Eastbourne Downs, 2026-09-09):
  {
    "availability": [
      { "date": "2026-09-09T08:32:00+01:00",
        "slots": 1,                       # spaces REMAINING on this tee time
        "types": [
          { "name": "Visitor - 18H", "price": 35, "min": 1, "max": 4,
            "resourceName": "Tee 1", "onlyMembers": false, "onlyPackage": false, ... }
        ] },
      ...
    ],
    "resources": [ {"id":100065,"name":"Tee 1"}, {"id":100066,"name":"Tee 10"} ]
  }

Notes that shaped the parser:
  - `price` is the per-PLAYER green fee. Golf Manager has no group discount in
    this sheet (all party sizes pay the same per head), so the total for N
    players is N x price. This is the one safe place to multiply, and it's
    verified, not assumed — do NOT copy this reasoning to other platforms.
  - A slot can carry several `types`: the plain green fee plus add-on packages
    like "Buggy Blitz - Golf & Buggy". We keep only the golf green fee and drop
    anything flagged onlyPackage / onlyMembers or named like an add-on, so the
    price reflects a bare round.
  - `slots` is availability remaining, not party size. We surface a slot as
    long as slots >= 1.
  - Multiple tees (1st / 10th) come back mixed; the default call returns the
    main tee. course_id, if set, is passed as idResource to pin one tee.

CONFIG: platform = golf_manager, base_url = https://{slug}.golfmanager.com,
course_id = optional idResource (leave blank for the default/main tee).

Usage:
    python golf_manager_scraper.py clubs_config.csv --date 2026-09-12
"""

import argparse
import re

import golf_common as gc

log = gc.log
PLATFORM = "golf_manager"

# Types that aren't a bare golf round — matched case-insensitively on name.
ADDON_NAME_RE = re.compile(r"buggy|cart|package|membership|academy|range|lesson", re.I)


def build_url(club: gc.ClubConfig, date_iso: str) -> str:
    url = f"{club.base_url.rstrip('/')}/ebookings/init.api?start={date_iso}T00:00:00"
    if club.course_id:
        url += f"&idResource={club.course_id}"
    return url


def _holes_from_name(name: str) -> str:
    # "Visitor - 9H" / "9 Hole" -> "9"; everything else defaults to 18.
    return "9" if re.search(r"\b9\s*h", name, re.I) else "18"


def _pick_green_fee(types: list[dict]) -> dict | None:
    """Return the cheapest bare golf green fee among a slot's types, or None."""
    candidates = [
        t for t in types
        if not t.get("onlyPackage") and not t.get("onlyMembers")
        and not ADDON_NAME_RE.search(t.get("name", ""))
        and t.get("price") is not None
    ]
    return min(candidates, key=lambda t: t["price"]) if candidates else None


def parse(payload: dict, club: gc.ClubConfig, date_iso: str) -> tuple[list[gc.TeeTimeResult], str]:
    results: list[gc.TeeTimeResult] = []
    availability = payload.get("availability")
    if availability is None:
        log.error(f"[{club.club_name}] Response had no 'availability' key — "
                  f"API shape may have changed or base_url is wrong")
        return results, "error"

    for entry in availability:
        if entry.get("slots", 0) < 1:
            continue
        gf = _pick_green_fee(entry.get("types", []))
        if not gf:
            continue

        raw = entry.get("date", "")
        tmatch = re.search(r"T(\d{2}:\d{2})", raw)
        if not tmatch:
            continue
        tee_time = tmatch.group(1)

        price = float(gf["price"])
        lo = int(gf.get("min") or 1)
        hi = int(gf.get("max") or lo)
        # Per-player fee, no group discount (see docstring) -> total = n x price.
        prices = {str(n): round(n * price, 2) for n in range(lo, hi + 1)}

        results.append(gc.TeeTimeResult(
            club_name=club.club_name, platform=PLATFORM, date=date_iso, time=tee_time,
            holes=_holes_from_name(gf.get("name", "")),
            prices_by_players=prices,
            booking_url=f"{club.base_url.rstrip('/')}/consumer/ebookings",
        ))
    return results, "ok"


def scrape_club(club: gc.ClubConfig, date_iso: str, session) -> tuple[list[gc.TeeTimeResult], str]:
    resp = gc.fetch(session, club.club_name, "GET", build_url(club, date_iso),
                    headers={"Accept": "application/json"})
    if resp is None:
        return [], "error"
    try:
        payload = resp.json()
    except ValueError:
        log.error(f"[{club.club_name}] Expected JSON but got something else — "
                  f"check base_url is the {{slug}}.golfmanager.com host")
        return [], "error"
    try:
        results, status = parse(payload, club, date_iso)
        if status == "error":
            return [], "error"
        if results:
            log.info(f"[{club.club_name}] Found {len(results)} available tee time(s) for {date_iso}")
            return results, "ok"
        log.info(f"[{club.club_name}] No availability on {date_iso} — nothing to scrape")
        return results, "empty"
    except Exception as e:
        log.error(f"[{club.club_name}] Parsing failed: {e}")
        return [], "error"


if __name__ == "__main__":
    gc.setup_logging()
    ap = argparse.ArgumentParser(description="Scrape Golf Manager tee sheets")
    ap.add_argument("config")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--output", default="tee_times_golf_manager.json")
    a = ap.parse_args()
    gc.run_platform(scrape_club, PLATFORM, a.config, a.date, a.output)
