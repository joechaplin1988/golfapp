"""
Intelligent Golf tee sheet scraper.

Covers any club on the Intelligent Golf platform, whether white-labeled on
the club's own domain (e.g. www.theridge.co.uk) or on a
<club>.intelligentgolf.co.uk subdomain. Server-rendered HTML, no auth.

Shared plumbing (output shape, config, retry, TLS fix, pacing) lives in
golf_common; this file is only the IG-specific fetch + parse.

CONFIG: platform = intelligent_golf, base_url = the .../visitorbooking path,
course_id = the numeric course (NOT always 1 — read it from a live booking
link; the page's own `course` input is often empty).

URL:  {base_url}?date=DD-MM-YYYY&course={course_id}

9 vs 18 holes is NOT a server parameter — the page toggle is a client-side
filter over `maxholes`, and one request already returns the full inventory.
`holes` is read per-slot from the booking href: "18" normally, "9" for slots
restricted to 9 (late twilight). See git history / README for the full note.

Usage:
    python intelligent_golf_scraper.py clubs_config.csv --date 2026-09-12
"""

import argparse
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import golf_common as gc

log = gc.log

PLATFORM = "intelligent_golf"

# Structural anchors that tell "this club genuinely has no availability today"
# apart from "this page isn't the tee sheet we think it is". Without the
# distinction both look identical (zero slots) and real breakage hides inside
# routine empty days. The no-availability message text differs per club, so we
# match on the CLASS, never the wording.
TEE_SHEET_CONTAINER_SELECTOR = "div.teebooking-teetimes"
NO_AVAILABILITY_SELECTOR = ".no-teetimes-message"


def to_ddmmyyyy(date_iso: str) -> str:
    return datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d-%m-%Y")


def build_url(club: gc.ClubConfig, date_iso: str) -> str:
    return f"{club.base_url.rstrip('/')}/?date={to_ddmmyyyy(date_iso)}&course={club.course_id}"


def parse(html: str, club: gc.ClubConfig, date_iso: str) -> tuple[list[gc.TeeTimeResult], str]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[gc.TeeTimeResult] = []

    slots = soup.select('div[class*="teetimes-slot"]')
    if not slots:
        if soup.select_one(NO_AVAILABILITY_SELECTOR):
            log.info(f"[{club.club_name}] No availability on {date_iso} "
                     f"(club's page says so explicitly) — nothing to scrape")
            return results, "empty"
        elif soup.select_one(TEE_SHEET_CONTAINER_SELECTOR):
            log.warning(f"[{club.club_name}] Tee sheet found for {date_iso} but it has no slots "
                        f"AND no no-availability message — markup may have changed")
        else:
            log.error(f"[{club.club_name}] Did not recognise this page as an Intelligent Golf "
                      f"tee sheet — check base_url/course_id, or the club may have changed platform")
        return results, "error"

    for slot in slots:
        fieldset = slot.find("fieldset")
        if not fieldset:
            continue
        legend = fieldset.find("legend")
        tmatch = re.search(r"\d{1,2}:\d{2}", legend.get_text() if legend else "")
        if not tmatch:
            continue
        tee_time = tmatch.group()

        booking_url = ""
        holes = "18"
        link = slot.find("a", href=True)
        if link:
            href = link["href"]
            booking_url = urljoin(f"{club.base_url.rstrip('/')}/", href)
            hm = re.search(r"[?&]holes=(\d+)", href)
            if hm:
                holes = hm.group(1)

        prices: dict[str, float] = {}
        for label in fieldset.select("label.player"):
            radio = label.find("input", {"name": "numslots"})
            price_span = label.find("span", class_="price")
            if radio and price_span and radio.get("value"):
                p = gc.parse_price(price_span.get_text())
                if p is not None:
                    prices[radio["value"]] = p
        if not prices:
            log.warning(f"[{club.club_name}] Slot at {tee_time} had no parseable prices — skipping")
            continue

        results.append(gc.TeeTimeResult(
            club_name=club.club_name, platform=PLATFORM, date=date_iso, time=tee_time,
            holes=holes, prices_by_players=prices, booking_url=booking_url,
        ))
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
    ap = argparse.ArgumentParser(description="Scrape Intelligent Golf tee sheets")
    ap.add_argument("config")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--output", default="tee_times_intelligent_golf.json")
    a = ap.parse_args()
    gc.run_platform(scrape_club, PLATFORM, a.config, a.date, a.output)
