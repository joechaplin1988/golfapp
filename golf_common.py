"""
Shared plumbing for every platform scraper.

One place for the things that must behave identically across platforms:
the output record shape, config loading, the polite-request policy (user
agent, pacing, retry-once-then-skip) and the local-machine TLS fix. A
platform scraper imports this and only writes the part that is genuinely
platform-specific: how to fetch a day's tee sheet and how to parse it.

Output contract — every scraper emits TeeTimeResult records with the same
fields, so the aggregator can merge them without caring which platform a
club runs. The contract is deliberately small; if a platform can't fill a
field honestly, leave it at the documented default rather than inventing.
"""

import csv
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Machines running HTTPS-scanning antivirus (AVG/Avast/Kaspersky) or sitting
# behind a corporate TLS proxy are served a re-signed certificate whose root
# is in the OS trust store but not in certifi's bundle, so requests rejects
# it. truststore makes Python verify against the OS store instead. Optional
# by design: on a clean server (the n8n host, CI) it simply isn't installed
# and verification falls back to certifi as normal.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

log = logging.getLogger("golf")

# --- Polite-request policy — applies to every platform ---------------------

USER_AGENT = "ServiceSynkGolfAggregator/0.1 (contact: joe@servicesynk.com)"
REQUEST_DELAY_SECONDS = 2.0     # pause between clubs
REQUEST_TIMEOUT_SECONDS = 45   # some Golf Manager hosts take 20-30s to answer init.api

# Observed in a real 17-sheet run: one club returned RemoteDisconnected and
# silently lost 55 tee times that were definitely there. One retry recovers
# it. Deliberately conservative — one extra attempt, only for errors that are
# plausibly transient, so a genuinely broken club still fails fast instead of
# doubling every run's traffic.
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 3.0

PLATFORMS = ("intelligent_golf", "esp", "golf_manager", "clubv1", "brs", "shiji", "gladstone",
             "chronogolf")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# --- Config ------------------------------------------------------------------

@dataclass
class ClubConfig:
    club_name: str
    platform: str
    base_url: str
    course_id: str = ""          # meaning is platform-specific — see each scraper's docstring
    postcode: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None


DISABLED_VALUES = {"false", "0", "no", "n", "off"}


def is_enabled(row: dict) -> bool:
    """clubs_config.csv rows can be parked with scrape_enabled=false.

    Used for courses we can reach but the scheduled job cannot — Mytime
    Active's Cloudflare answers 403 to the CI runner. Deleting the row would
    lose the club and the reason; leaving it enabled buries every real failure
    under 49 identical errors a run. The column is optional: a missing or
    blank value means enabled.
    """
    return str(row.get("scrape_enabled", "") or "").strip().lower() not in DISABLED_VALUES


def load_clubs(config_path: str, platform: Optional[str] = None) -> list[ClubConfig]:
    """Rows with no base_url are skipped (logged) — safe to leave partially filled.

    Whether course_id is required is up to each platform; Golf Manager and
    ESP get by without one, Intelligent Golf and ClubV1 can't.
    """
    clubs: list[ClubConfig] = []
    with open(config_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("club_name") or "unknown").strip()
            plat = (row.get("platform") or "").strip().lower()
            if platform and plat != platform:
                continue
            if not (row.get("base_url") or "").strip():
                log.warning(f"Skipping '{name}' — base_url not filled in yet")
                continue
            if plat not in PLATFORMS:
                log.warning(f"Skipping '{name}' — platform '{plat}' not one of {PLATFORMS}")
                continue
            if not is_enabled(row):
                log.info(f"Skipping '{name}' — parked (scrape_enabled=false)")
                continue
            clubs.append(ClubConfig(
                club_name=name,
                platform=plat,
                base_url=row["base_url"].strip(),
                course_id=(row.get("course_id") or "").strip(),
                postcode=(row.get("postcode") or "").strip(),
                latitude=float(row["latitude"]) if row.get("latitude") else None,
                longitude=float(row["longitude"]) if row.get("longitude") else None,
            ))
    return clubs


# --- Output contract ----------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class TeeTimeResult:
    club_name: str
    platform: str
    date: str                       # YYYY-MM-DD, normalised regardless of source format
    time: str                       # HH:MM, 24h
    holes: str                      # longest round bookable at this slot: "18" or "9"
    prices_by_players: dict         # {"1": 38.0, "2": 76.0, ...} TOTAL price for that party size.
                                    # The KEYS are the bookable party sizes — a slot that only
                                    # offers {"2","3","4"} has a 2-player minimum. Never assume 1-4.
    booking_url: str                # deep link into the club's own booking flow for this slot
    checked_at: str = field(default_factory=utc_now_iso)


# --- Course status ------------------------------------------------------------
#
# Clubs publish a line about the state of the course — "greens 9, 10, 14, 15 are
# temporary due to drainage works", "preferred lies in play", "buggies off".
# Testers asked for it: paying a visitor green fee and arriving to find half the
# greens under maintenance is the worst surprise in golf, and the club has
# already said so on the very page we read.
#
# It is collected alongside the tee sheet at no extra request, so only the
# platforms that publish it on that page carry one. The note is the club's own
# words — never reworded, never summarised, and stored with the time we read it
# so the page can say how fresh it is. A club that publishes nothing shows
# nothing; we never infer "course open" from silence.

STATUS_MAX_CHARS = 400
_STATUS_LEAD = re.compile(r"^[\s●•▪–—\-*:]+")
# "Cooden Beach Golf Course: COURSE OPEN..." — the club's own name back at it is
# noise on a card that already says which club this is.
_CLUB_WORDS = re.compile(r"[^a-z0-9]+")
_GENERIC = {"golf", "club", "course", "the", "gc", "g", "c", "links", "park", "and"}
# The heading above the notice, when a theme has it in an unexpected tag.
_STATUS_LABEL = re.compile(r"^(course|greens?)?\s*status(\s+update)?\s*[:\-–—]?\s*", re.I)
# Clubs that keep the block but have nothing in it. "Course open" is a status;
# "no updates available" is an empty box, and showing it implies we know
# something we don't.
_SAYS_NOTHING = re.compile(
    r"^(no\s+(current\s+)?(updates?|news|information|reports?)(\s+available)?|"
    r"nothing\s+to\s+report|none|n/?a|tbc|tba|coming\s+soon|latest\s+news)\s*[.!]?$", re.I)

_status_notes: dict[tuple[str, str, str], str] = {}


def _club_key(club: "ClubConfig") -> tuple[str, str, str]:
    return (club.platform, club.base_url, club.course_id or "")


def clean_status_note(text: str, club_name: str = "") -> str:
    """Tidy a club's status line without changing what it says.

    Only cosmetic: collapse whitespace, drop the bullet the platform draws, drop
    the club's own name where it prefixes its own notice, and cut an essay down
    to a readable length on a word boundary. If what's left is too short to mean
    anything ("Open" alone is fine, "." is not), return "" so nothing is shown.
    """
    note = re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()
    note = _STATUS_LEAD.sub("", _STATUS_LABEL.sub("", _STATUS_LEAD.sub("", note)))
    head, sep, tail = note.partition(":")
    if sep and len(head) <= 60 and tail.strip():
        words = {w for w in _CLUB_WORDS.split(head.lower()) if w} - _GENERIC
        club = {w for w in _CLUB_WORDS.split(club_name.lower()) if w} - _GENERIC
        # Both sides must actually name something: a club whose name is all
        # generic words would otherwise match every prefix and eat real text.
        if words and club and (words <= club or club <= words):
            note = _STATUS_LEAD.sub("", tail.strip())
    if _SAYS_NOTHING.match(note):
        return ""
    if len(note) > STATUS_MAX_CHARS:
        cut = note[:STATUS_MAX_CHARS].rsplit(" ", 1)[0]
        note = cut.rstrip(" ,;.") + "…"
    return note if len(note) >= 8 else ""


def record_status_note(club: "ClubConfig", text: str) -> None:
    """Called by a scraper when the page it just read carried a status line."""
    note = clean_status_note(text, club.club_name)
    if note:
        _status_notes[_club_key(club)] = note


def status_note_for(club: "ClubConfig") -> str:
    return _status_notes.get(_club_key(club), "")


def write_results(results: list[TeeTimeResult], output_path: str) -> None:
    Path(output_path).write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    log.info(f"Wrote {len(results)} tee time record(s) to {output_path}")


# --- HTTP ---------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch(session: requests.Session, club_name: str, method: str, url: str, **kwargs) -> Optional[requests.Response]:
    """One request with the shared retry policy. Returns None on failure (already logged).

    A 4xx means we asked for the wrong thing — retrying just repeats the
    mistake. Connection drops, timeouts and 5xx are worth one more go.
    """
    kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = session.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            status = e.response.status_code if e.response is not None else None
            transient = status is None or status >= 500
            if not transient:
                log.error(f"[{club_name}] Request failed with HTTP {status} — not retrying: {e}")
                return None
            if attempt == MAX_ATTEMPTS:
                log.error(f"[{club_name}] Request failed after {MAX_ATTEMPTS} attempts: {e}")
                return None
            log.warning(f"[{club_name}] Attempt {attempt}/{MAX_ATTEMPTS} failed ({e}) — "
                        f"retrying in {RETRY_BACKOFF_SECONDS:g}s")
            time.sleep(RETRY_BACKOFF_SECONDS)
    return None


def pause_between_clubs() -> None:
    time.sleep(REQUEST_DELAY_SECONDS)


def parse_price(text: str) -> Optional[float]:
    """'£38.00' / '38.00' / '£1,250' -> float. None if no number (defensive)."""
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else None


# --- Orchestration shared by every scraper -----------------------------------

# A platform scraper provides a function with this signature. It returns the
# slots it found AND a status, so the DB loader can tell a genuinely empty day
# (safe to clear the sheet) from a failed scrape (leave the last-good data).
#   status: "ok"    — scraped, found slots
#           "empty" — scraped, club has no availability that day (normal)
#           "error" — could not scrape (network, or page not recognised)
ScrapeClubFn = Callable[["ClubConfig", str, "object"], "tuple[list[TeeTimeResult], str]"]


def _manifest_path(output_path: str, platform: str) -> str:
    return str(Path(output_path).with_name(f"run_{platform}.json"))


def run_platform(scrape_club: ScrapeClubFn, platform: str, config_path: str,
                 date_iso: str, output_path: str) -> None:
    """Load this platform's clubs, scrape each, write results + a run manifest.

    Two files are written:
      - output_path              : the flat list of TeeTimeResult records
      - run_<platform>.json      : per-course {status, n} so the DB loader knows
                                   which sheets were attempted and how each went
                                   (needed to correctly clear now-booked slots
                                   without wiping a sheet whose scrape errored).
    """
    clubs = load_clubs(config_path, platform=platform)
    log.info(f"Loaded {len(clubs)} {platform} club(s) with complete config")
    session = make_session()

    all_results: list[TeeTimeResult] = []
    manifest: list[dict] = []
    for i, club in enumerate(clubs):
        results, status = scrape_club(club, date_iso, session)
        all_results.extend(results)
        manifest.append({
            "club_name": club.club_name,
            "platform": club.platform,
            "base_url": club.base_url,
            "course_ref": club.course_id,
            "status": status,
            "n": len(results),
            "status_note": status_note_for(club),
        })
        if i < len(clubs) - 1:
            pause_between_clubs()

    write_results(all_results, output_path)
    manifest_doc = {"platform": platform, "date": date_iso,
                    "generated_at": utc_now_iso(), "courses": manifest}
    Path(_manifest_path(output_path, platform)).write_text(
        json.dumps(manifest_doc, indent=2), encoding="utf-8")
    ok = sum(1 for m in manifest if m["status"] == "ok")
    empty = sum(1 for m in manifest if m["status"] == "empty")
    err = sum(1 for m in manifest if m["status"] == "error")
    log.info(f"{platform}: {ok} ok, {empty} empty, {err} error across {len(manifest)} course(s)")
