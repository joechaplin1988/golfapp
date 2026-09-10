"""
BRS Golf visitor tee sheet scraper.

BRS clubs share one host: https://visitors.brsgolf.com/{slug}. The page is a
SPA, but the JSON it calls is open — the only "auth" is a session cookie the
server sets when you load the club's page, which tells the API which club you
mean. So each scrape is two requests on one session:

    GET  {base_url}                          -> sets the club-context cookie
    GET  /api/casualBooking/teesheet?date=YYYY-MM-DD&course_id={course_id}

The API also wants the headers the app sends (X-Requested-With,
X-Booking-Source) and a Referer of the club page — it resolves the club from
cookie + Referer. Without the page visit it answers 500 "Could not get tee
sheet"; without the Referer, 400 "Object reference not set". Those two errors
are what made BRS look walled-off on the first recon.

Response (confirmed on 9 Kent/Sussex clubs, 2026-09-09):
  {"data": {"tee_times": [
     {"id": 553197, "time": "07:10",
      "slots": {"1": {"status": "Available"}, "2": {"status": "Booked"}, ...},
      "green_fees": [{"green_fee1_ball": "50.00", "green_fee2_ball": "100.00",
                      "green_fee3_ball": "150.00", "green_fee4_ball": "200.00",
                      "num_holes": "18", "type": "Standard",
                      "package_enabled": false, ...}],
      "is_hot_deal": false}, ...]}}

Notes that shaped the parser:
  - green_feeN_ball is the TOTAL for a party of N — Pyecombe's 4-ball is
    £150 against a £40 single, so a group discount exists. Store as-is, never
    multiply (same trap as Intelligent Golf / ClubV1).
  - Some clubs attach no green fee at all (Silvermere). Those slots are kept
    with null prices rather than dropped, so the club still appears; the
    search page shows "price on club site". Same policy as Gladstone.
  - `slots` is the four places on the tee; a party of N needs N "Available".
    So the bookable party sizes are 1..(number available), not always 1..4.
  - One tee time can carry several green_fees (Peacehaven lists an 18-hole
    and a 9-hole fee on the same slot). We keep one row per slot: the longest
    round, cheapest non-package fee, so `holes` means "longest bookable".
  - An empty tee_times list is also what a club with visitor booking switched
    OFF returns (Highwoods: 0 slots on every date, both tees), so "empty" is
    honest either way — nothing is bookable.

CONFIG: platform = brs, base_url = https://visitors.brsgolf.com/{slug},
course_id = the BRS course id from /api/courses/all (1 for nearly every club).

Usage:
    python brs_scraper.py clubs_config.csv --date 2026-09-12
"""

import argparse

import golf_common as gc

log = gc.log
PLATFORM = "brs"
HOST = "https://visitors.brsgolf.com"

# What the BRS front end sends on every API call (read from its bundle.js).
API_HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "X-Booking-Source": "ui",
}


def teesheet_url(club: gc.ClubConfig, date_iso: str) -> str:
    return f"{HOST}/api/casualBooking/teesheet?date={date_iso}&course_id={club.course_id or '1'}"


def booking_url(club: gc.ClubConfig) -> str:
    # The SPA has no date in its routes; this lands on the club's sheet.
    return f"{club.base_url.rstrip('/')}#/course/{club.course_id or '1'}"


def _single_fee(fee: dict) -> float:
    p = gc.parse_price(str(fee.get("green_fee1_ball") or ""))
    return p if p is not None else float("inf")


def _holes(fee: dict) -> int:
    try:
        return int(fee.get("num_holes") or 0)
    except ValueError:
        return 0


def _pick_green_fee(fees: list[dict]) -> dict | None:
    """Longest round first, then cheapest single-ball fee; packages only as a last resort."""
    usable = [f for f in fees if _single_fee(f) != float("inf")]
    if not usable:
        return None
    bare = [f for f in usable if not f.get("package_enabled")] or usable
    return min(bare, key=lambda f: (-_holes(f), _single_fee(f)))


def parse(payload: dict, club: gc.ClubConfig, date_iso: str) -> tuple[list[gc.TeeTimeResult], str]:
    results: list[gc.TeeTimeResult] = []
    tee_times = (payload.get("data") or {}).get("tee_times") if isinstance(payload, dict) else None
    if tee_times is None:
        log.error(f"[{club.club_name}] Response had no data.tee_times — "
                  f"API shape may have changed, or the club page didn't set its context")
        return results, "error"

    unpriced: list[str] = []
    for t in tee_times:
        n_avail = sum(1 for s in (t.get("slots") or {}).values() if s.get("status") == "Available")
        if n_avail < 1:
            continue
        tee_time = str(t.get("time") or "")
        if len(tee_time) != 5:
            continue
        fee = _pick_green_fee(t.get("green_fees") or [])
        prices: dict[str, float] = {}
        if fee:
            for n in range(1, min(4, n_avail) + 1):
                p = gc.parse_price(str(fee.get(f"green_fee{n}_ball") or ""))
                if p is not None and p > 0:
                    prices[str(n)] = p
        if not prices:
            # Some BRS clubs publish availability but no green fee at all
            # (Silvermere: 45 open slots, every green_fee1_ball null). Dropping
            # them hid the whole club — same "availability known, price not
            # published" case as Gladstone, so same policy: keep the slot with
            # null prices and let the page say "price on club site".
            unpriced.append(tee_time)
            prices = {str(n): None for n in range(1, min(4, n_avail) + 1)}
        results.append(gc.TeeTimeResult(
            club_name=club.club_name, platform=PLATFORM, date=date_iso, time=tee_time,
            holes=str(fee.get("num_holes") if fee else "18") or "18", prices_by_players=prices,
            booking_url=booking_url(club),
        ))
    if unpriced:
        log.info(f"[{club.club_name}] {len(unpriced)} of {len(results)} slot(s) have no green fee "
                 f"in BRS — kept without a price")
    return results, "ok" if results else "empty"


def scrape_club(club: gc.ClubConfig, date_iso: str, session) -> tuple[list[gc.TeeTimeResult], str]:
    # The host is shared, so a previous club's context cookie would be wrong
    # for this one — start clean and let the club page set it.
    session.cookies.clear()
    page = gc.fetch(session, club.club_name, "GET", club.base_url)
    if page is None:
        return [], "error"
    resp = gc.fetch(session, club.club_name, "GET", teesheet_url(club, date_iso),
                    headers={**API_HEADERS, "Referer": club.base_url})
    if resp is None:
        return [], "error"
    try:
        payload = resp.json()
    except ValueError:
        log.error(f"[{club.club_name}] Expected JSON but got something else — "
                  f"check base_url is https://visitors.brsgolf.com/<slug>")
        return [], "error"
    try:
        results, status = parse(payload, club, date_iso)
        if results:
            log.info(f"[{club.club_name}] Found {len(results)} available tee time(s) for {date_iso}")
        elif status == "empty":
            log.info(f"[{club.club_name}] No availability on {date_iso} — nothing to scrape")
        return results, status
    except Exception as e:
        log.error(f"[{club.club_name}] Parsing failed: {e}")
        return [], "error"


if __name__ == "__main__":
    gc.setup_logging()
    ap = argparse.ArgumentParser(description="Scrape BRS Golf visitor tee sheets")
    ap.add_argument("config")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--output", default="tee_times_brs.json")
    a = ap.parse_args()
    gc.run_platform(scrape_club, PLATFORM, a.config, a.date, a.output)
