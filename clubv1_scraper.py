"""
ClubV1 (Club Systems International) tee sheet scraper.

ClubV1 clubs expose a visitor tee sheet on a hub subdomain as server-rendered
HTML (no auth beyond an anonymous cookie the server sets itself):

    GET {base_url}/Visitors/TeeSheet?date=YYYY-MM-DD&courseId={course_id}

Structure (confirmed against Willingdon, 2026-09-09):
  <div class="tee available" data-teetime="2026-09-09 08:30">
    <div class="time">08:30</div>
    <div class="prices">
      <div class="price ball-1"><div class="value">65.00</div></div>   # 1 player TOTAL
      <div class="price ball-2"><div class="value">130.00</div></div>  # 2 players TOTAL
      ... ball-3, ball-4 ...
    </div>
    <a data-book href="/Visitors/BookingAdd?dateTime=...&courseId=...">Book</a>
  </div>

Notes that shaped the parser:
  - Unlike per-player platforms, ClubV1's ball-N value is the TOTAL for N
    players already — store it as-is, never multiply.
  - The booking <a href> is a genuine per-slot deep link into the club's flow.
  - A <div class="tee"> without "available" (e.g. class "sunset", or booked)
    is not bookable — we only take div.tee.available.
  - The "9 holes / 18 holes" toggle is client-side only: passing
    selectedHoleCount changes nothing server-side and the same slots come back.
    So one request is the full inventory; holes is recorded as "18".
  - courseId is required and club-specific (Willingdon = 10566); read it from
    the tee sheet's own date links.

CONFIG: platform = clubv1, base_url = https://{slug}.hub.clubv1.com,
course_id = the numeric courseId.

Usage:
    python clubv1_scraper.py clubs_config.csv --date 2026-09-12
"""

import argparse
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import golf_common as gc

log = gc.log
PLATFORM = "clubv1"


def build_url(club: gc.ClubConfig, date_iso: str) -> str:
    return f"{club.base_url.rstrip('/')}/Visitors/TeeSheet?date={date_iso}&courseId={club.course_id}"


def parse(html: str, club: gc.ClubConfig, date_iso: str) -> tuple[list[gc.TeeTimeResult], str]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[gc.TeeTimeResult] = []

    all_tees = soup.select("div.tee")
    if not all_tees:
        # No tee rows at all: either the page isn't a ClubV1 tee sheet, or the
        # club shows nothing for this date. The container id is the tell.
        if soup.select_one("#booking-teesheet-container"):
            log.info(f"[{club.club_name}] Tee sheet loaded but no tee rows for {date_iso} — "
                     f"treating as no availability")
            return results, "empty"
        log.error(f"[{club.club_name}] Did not recognise this page as a ClubV1 tee sheet — "
                  f"check base_url/course_id")
        return results, "error"

    available = soup.select("div.tee.available")
    for tee in available:
        teetime = tee.get("data-teetime", "")
        parts = teetime.split()
        if len(parts) != 2:
            continue
        tee_time = parts[1][:5]

        prices: dict[str, float] = {}
        for price_div in tee.select("div.price"):
            classes = price_div.get("class", [])
            ball = next((c for c in classes if c.startswith("ball-")), None)
            value_el = price_div.select_one(".value")
            if ball and value_el:
                p = gc.parse_price(value_el.get_text())
                if p is not None:
                    prices[ball.split("-", 1)[1]] = p  # ball-2 -> "2"
        if not prices:
            continue

        booking_url = ""
        book_link = tee.select_one("a[data-book][href]")
        if book_link:
            booking_url = urljoin(club.base_url.rstrip("/") + "/", book_link["href"])

        results.append(gc.TeeTimeResult(
            club_name=club.club_name, platform=PLATFORM, date=date_iso, time=tee_time,
            holes="18", prices_by_players=prices, booking_url=booking_url,
        ))

    if not available:
        log.info(f"[{club.club_name}] Tee sheet has rows but none available on {date_iso} "
                 f"(fully booked) — nothing to scrape")
        return results, "empty"
    return results, "ok"


def scrape_club(club: gc.ClubConfig, date_iso: str, session) -> tuple[list[gc.TeeTimeResult], str]:
    resp = gc.fetch(session, club.club_name, "GET", build_url(club, date_iso))
    if resp is None:
        return [], "error"
    try:
        results, status = parse(resp.text, club, date_iso)
        if results:
            log.info(f"[{club.club_name}] Found {len(results)} available tee time(s) for {date_iso}")
        return results, status
    except Exception as e:
        log.error(f"[{club.club_name}] Parsing failed: {e}")
        return [], "error"


if __name__ == "__main__":
    gc.setup_logging()
    ap = argparse.ArgumentParser(description="Scrape ClubV1 tee sheets")
    ap.add_argument("config")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--output", default="tee_times_clubv1.json")
    a = ap.parse_args()
    gc.run_platform(scrape_club, PLATFORM, a.config, a.date, a.output)
