"""
ESP (e-s-p.com / EliteLive) tee sheet scraper.

ESP clubs put their booking on a shared host, www.e-s-p.com/elitelive, keyed
by a numeric clubid. It's a stateful, session-based flow rather than a single
URL, so this scraper walks the same three steps a browser does:

  1. GET  {base}/book_start.php?clubid={clubid}
         Sets a PHPSESSID cookie and lands on the group chooser.
  2. Follow the "18 Holes" visitor group link (book_group.php?...GroupSelected
         =...&GD=18+Holes...). This primes the session with the activity.
  3. POST {base}/ajax/ajax_gettime.php?d=DD/MM/YY&redirect_page=book_date.php
         Returns an HTML fragment of the day's available slots.

Fragment structure (confirmed against Westerham, 2026-09-09):
  <div class="fullsheet_container_available">
    <a href="?gotdata=2&StartDate=10/09/26&...&Start=06:48&...&Price=70.00&">
      06:48 <br> £70.00
    </a>
  </div>

Notes that shaped the parser:
  - ESP shows ONE price per slot — the per-player visitor green fee — not a
    per-party-size breakdown. Verified: the price is identical whether the
    session's party count is 1 or 2 (no group discount). So the total for N
    players is N x the green fee. Like Golf Manager, this is a verified-safe
    multiply, specific to ESP; don't generalise it. Party sizes default to
    1-4 (ESP's Non-Member activity min 1, max 4).
  - The price is taken from the href's `Price=` param (authoritative), with
    the visible "£xx.xx" as a fallback.
  - Per-slot hrefs are session-bound and useless as a shared link, so the
    booking_url is the club's stable ESP entry (book_start.php?clubid=X).
  - We select the 18-hole visitor group, so holes is "18". (ESP does have a
    separate 9-hole / Par-3 group on some clubs; out of scope for v1.)
  - Group naming varies per club, so step 2 discovers the link from the page
    (prefer one whose GD/label contains "18") rather than hardcoding it.

CONFIG: platform = esp, base_url = https://www.e-s-p.com/elitelive,
course_id = the club's numeric ESP clubid.

Usage:
    python esp_scraper.py clubs_config.csv --date 2026-09-12
"""

import argparse
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import golf_common as gc

log = gc.log
PLATFORM = "esp"

ESP_MAX_PARTY = 4  # ESP Non-Member activity is min 1, max 4


def _to_ddmmyy(date_iso: str) -> str:
    return datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d/%m/%y")


ESP_BACKEND_ERROR_RE = re.compile(r"RestAPI error|Connection timed out after|Fatal error", re.I)


def _find_18_hole_group(html: str) -> str | None:
    """Pick the visitor 18-hole group link from a chooser page, else None.

    A chooser can carry two kinds of link. gotdata=1 selects a real booking
    group. gotdata=2 opens a CATEGORY, which is a second chooser of groups,
    and some clubs put every course behind one: Garon Park's page offers
    "Main Courses", "Par 3 Course", "Adventure Golf" and "Carvery Bookings",
    with "Starts On West/South/East Course" one level down. Treating the
    category as the group selected nothing, and every date came back as an
    honest-looking "empty" -- a live club with no tee times, ever.
    A category now scores just below a real group, so it is only followed
    when nothing better is on the page (see scrape_club).
    """
    soup = BeautifulSoup(html, "html.parser")
    group_links = [a for a in soup.find_all("a", href=True) if "book_group.php" in a["href"]]
    if not group_links:
        return None

    def score(a):
        blob = (a["href"] + " " + a.get_text(" ", strip=True)).lower()
        if ("par 3" in blob or "par3" in blob or re.search(r"\b9\b", blob)
                or any(w in blob for w in ("adventure", "footgolf", "carvery", "mini golf", "academy"))):
            return -1
        value = 1 if "18" in blob else 0
        if "gotdata=2" in blob:
            value -= 0.5
        return value

    best = max(group_links, key=score)
    return best["href"] if score(best) > -1 else None


def parse_fragment(html: str, club: gc.ClubConfig, date_iso: str) -> list[gc.TeeTimeResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[gc.TeeTimeResult] = []
    booking_entry = f"{club.base_url.rstrip('/')}/book_start.php?clubid={club.course_id}"

    for box in soup.select("div.fullsheet_container_available"):
        link = box.find("a", href=True)
        if not link:
            continue
        text = link.get_text(" ", strip=True)
        tmatch = re.search(r"\b\d{1,2}:\d{2}\b", text)
        if not tmatch:
            continue
        tee_time = tmatch.group()

        # Price from the href param first, visible text as fallback.
        price = None
        pmatch = re.search(r"[?&]Price=([\d.]+)", link["href"])
        if pmatch:
            price = float(pmatch.group(1))
        else:
            price = gc.parse_price(text)
        if price is None:
            continue

        # Flat per-player green fee, no group discount (see docstring).
        prices = {str(n): round(n * price, 2) for n in range(1, ESP_MAX_PARTY + 1)}

        results.append(gc.TeeTimeResult(
            club_name=club.club_name, platform=PLATFORM, date=date_iso, time=tee_time,
            holes="18", prices_by_players=prices, booking_url=booking_entry,
        ))
    return results


def scrape_club(club: gc.ClubConfig, date_iso: str, session) -> tuple[list[gc.TeeTimeResult], str]:
    base = club.base_url.rstrip("/")

    # ESP's flow is stateful and per-club, so each club starts from a clean
    # cookie jar (the shared runner reuses one session across clubs).
    session.cookies.clear()

    # Step 1 — start session.
    start = gc.fetch(session, club.club_name, "GET",
                     f"{base}/book_start.php?clubid={club.course_id}&")
    if start is None:
        return [], "error"

    # Step 2 — select the 18-hole visitor group. Some clubs land straight on a
    # date chooser with the activity pre-selected (book_date.php, or the
    # calendar variant book_widedaterange.php seen on Prince's) — those skip
    # the group step entirely and go straight to the AJAX fetch.
    DATE_CHOOSER_PAGES = ("book_date.php", "book_widedaterange.php")
    if not any(p in start.url for p in DATE_CHOOSER_PAGES):
        group_href = _find_18_hole_group(start.text)
        if not group_href:
            # ESP's own backend sometimes fails behind its front end: the page
            # renders normally, with the club's name, and simply has no groups
            # plus a line like "ESP RestAPI error: Connection timed out after
            # 10001 milliseconds" (seen on Cranleigh, which scrapes fine
            # hours either side). Blaming the config for that sends whoever
            # reads the log hunting for a wrong clubid.
            if ESP_BACKEND_ERROR_RE.search(start.text):
                log.error(f"[{club.club_name}] ESP's own backend errored on the group page "
                          f"— transient on their side, not a config problem; keeping existing rows")
            else:
                # Not always a config fault, and saying so sends people
                # hunting. Prince's fails this way on one date and returns 50
                # tee times on the next in the SAME run, alongside a
                # RemoteDisconnected from the same host — ESP dropping the
                # session, not a wrong clubid. Only a club that fails on every
                # date, every run, is a config problem.
                log.error(f"[{club.club_name}] No booking group link and not on a date chooser — "
                          f"ESP may have dropped the session (transient, retries next run); "
                          f"if it fails on every date every run, check clubid={club.course_id}")
            return [], "error"
        grp = gc.fetch(session, club.club_name, "GET", urljoin(start.url, group_href))
        if grp is None:
            return [], "error"
        # A category link lands on a second chooser: choose again, once.
        if "gotdata=2" in group_href:
            inner = _find_18_hole_group(grp.text)
            if not inner or "gotdata=2" in inner:
                log.error(f"[{club.club_name}] ESP category page offered no bookable group")
                return [], "error"
            grp = gc.fetch(session, club.club_name, "GET", urljoin(grp.url, inner))
            if grp is None:
                return [], "error"
    gc.pause_between_clubs()

    # Step 3 — fetch the day's slot fragment.
    frag = gc.fetch(session, club.club_name, "POST",
                    f"{base}/ajax/ajax_gettime.php?d={_to_ddmmyy(date_iso).replace('/', '%2F')}"
                    f"&redirect_page=book_date.php&",
                    headers={"X-Requested-With": "XMLHttpRequest"})
    if frag is None:
        return [], "error"
    try:
        results = parse_fragment(frag.text, club, date_iso)
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
    ap = argparse.ArgumentParser(description="Scrape ESP / EliteLive tee sheets")
    ap.add_argument("config")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--output", default="tee_times_esp.json")
    a = ap.parse_args()
    gc.run_platform(scrape_club, PLATFORM, a.config, a.date, a.output)
