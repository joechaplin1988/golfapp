"""
Club discovery pipeline — finds which booking platform a golf club uses,
without needing a search engine for most of them.

WHY THIS EXISTS: manually identifying a club's platform (this session's
approach for The Ridge, Cinque Ports, Nizels, The Heron etc.) takes real
effort per club — DNS checks, fingerprinting requests, browser recon. Kent
and Sussex alone have ~160 golf clubs (per OpenStreetMap); the original
34-club survey covered a fraction of that. This script automates the
mechanical parts.

PIPELINE (three stages, each independently rerunnable):

  1. enumerate  — pull the candidate club list. Two sources, both free and
     needing no search API:
       - OpenStreetMap Overpass API: every leisure=golf_course feature in a
         region (name + coordinates). Gets us WHICH clubs exist.
       - County golf union directories (kentgolf.org, sussexgolf.org):
         structured club listings with each club's own website URL. Gets us
         WHERE each club's site is, without a single WebSearch call.
     IMPORTANT CAUGHT BUG: the county union pages also contain a generic
     "Created by intelligentgolf version X" CMS credit in their OWN site
     footer — this is about kentgolf.org's website, NOT about any club's
     booking platform, and must not be read as a platform signal. Verified
     this concretely: it appeared on Birchwood Park's county page, but
     Birchwood Park is confirmed ESP, not Intelligent Golf. Only markers
     found on the CLUB'S OWN site (fetched separately) count.

  2. fingerprint — for each club with a known website, fetch that site (and
     one likely booking-link hop) and match against the same platform
     markers used by intelligent_golf_scraper.py / esp_scraper.py /
     golf_manager_scraper.py / clubv1_scraper.py, plus markers for the
     deferred platforms (BRS, Chronogolf, Shiji) so we can at least COUNT
     them without building a scraper yet. Never writes to clubs_config.csv —
     always to a review file (candidates_review.csv) for a human pass, per
     the project's stated review-gate.

  3. (separately, manual) once a batch is approved, a human — or a follow-up
     script — moves confirmed rows from the review file into clubs_config.csv,
     using the SAME per-platform course_id discovery already proven in each
     scraper (a live-date probe to read course=/clubid=/courseId= off a real
     booking link). Deliberately NOT automated in this script: getting the
     course_id right needs a live availability check, which is exactly the
     kind of per-club nuance (course_id often isn't 1, multi-course clubs,
     login walls) that burned real time this session and deserves a look.

Usage:
    python discover_clubs.py enumerate --county kent   --out candidates_kent.json
    python discover_clubs.py enumerate --county sussex --out candidates_sussex.json
    python discover_clubs.py fingerprint candidates_kent.json candidates_sussex.json \
        --config clubs_config.csv --out candidates_review.csv
"""

import argparse
import csv
import io
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("discover")

USER_AGENT = "ServiceSynkGolfAggregator/0.1 (contact: joe@servicesynk.com; research use)"
REQUEST_TIMEOUT = 20
REQUEST_DELAY_SECONDS = 1.5  # per-club pacing — same politeness stance as the scrapers

# --- Stage 1a: OSM enumeration -----------------------------------------------

# The public overpass-api.de instance is a shared free resource and can be
# slow/504 under load. Try it once, then fall back to the kumi.systems
# mirror — don't hammer either with repeated retries.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Names that show up as OSM leisure=golf_course but aren't a bookable full
# course a visitor would tee off on: closed venues, pitch & putt, driving
# ranges, standalone "academy" sub-features of a course already counted
# under its own name.
NOISE_NAME_RE = re.compile(
    r"closed|pitch\s*&?\s*putt|driving range|academy course|par\s?3\b", re.I
)

COUNTY_BOUNDS = {
    "kent": ["Kent"],
    "sussex": ["East Sussex", "West Sussex"],
    "surrey": ["Surrey"],
    # Southend-on-Sea and Thurrock are unitary authorities, so OSM does not
    # nest them under "Essex" — name them or the coast is missed.
    "essex": ["Essex", "Southend-on-Sea", "Thurrock"],
    # Mainland only. Hampshire's union also covers the Isle of Wight and the
    # Channel Islands, but our distances are straight-line: an IoW course
    # would show up 15 "miles" from Portsmouth and need a ferry.
    "hampshire": ["Hampshire", "Southampton", "Portsmouth"],
    "hertfordshire": ["Hertfordshire"],
    # Berkshire has NO county-level area in OSM — it is six unitary
    # authorities, so naming "Berkshire" alone would find nothing at all.
    "berkshire": ["Reading", "West Berkshire", "Wokingham", "Bracknell Forest",
                  "Windsor and Maidenhead", "Slough"],
    # Greater London is a REGION, not a county: it sits at admin_level 5.
    # Asking for it at 6 returns nothing at all, silently — the same trap
    # Berkshire sets. 128 golf features at the right level.
    "london": ["Greater London"],
}

# Areas whose OSM boundary is not at the usual admin_level 6.
COUNTY_ADMIN_LEVEL = {"london": 5}
DEFAULT_ADMIN_LEVEL = 6

# Clubs the county union lists that are not reachable by road from the county
# (see hampshire above). Applied only to the union-only additions.
COUNTY_UNION_EXCLUDE = {
    # Hampshire's union also covers the Isle of Wight and the Channel Islands.
    # Search distances are straight-line, so an IoW course would advertise
    # itself as ~15 miles from Portsmouth and then need a ferry. Matched on
    # club names, not island names: several give no island in their title
    # (L'Ancresse, Les Mielles and La Grande Mare are all Channel Islands).
    "hampshire": re.compile(
        r"\b(alderney|jersey|guernsey|sark|herm|la moye|l'?ancresse|"
        r"la grande mare|les mielles|les ormes|st\.? ?pierre park|st\.? ?clements|"
        r"isle of wight|freshwater|shanklin|osborne|ryde|ventnor|cowes|"
        r"newport|westridge)\b", re.I),
}


def norm_club_name(s: str) -> str:
    """Loose match key for club names across sources that name the same club
    differently — OSM says "Barnehurst Golf Course", the county union says
    "Barnehurst Golf Club", our config just says "Barnehurst". Strip generic
    suffix words BEFORE collapsing to alnum-only, so all three converge on
    the same key. Used everywhere two name lists need matching in this file
    — kept as one function so a fix here can't miss a second call site (it
    did, once, during this script's own build)."""
    # "and"/"the" stripped too: the county union writes "Rochester & Cobham
    # Park" where OSM writes "Rochester and Cobham Park" — the "&" vanishes
    # in the alnum collapse but "and" didn't, so the two never matched.
    s = re.sub(r"\b(golf|club|course|centre|center|links|and|the)\b", " ", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


# norm_club_name() of a config entry -> norm_club_name() of the club's full
# name as OSM / the county union write it. See cmd_fingerprint.
KNOWN_ALIASES = {
    "regc": "royaleastbourne",
}


def enumerate_osm(county: str, refresh: bool = False) -> list[dict]:
    # Cache aggressively: golf courses appear/disappear on a timescale of
    # years, and the public Overpass mirrors are a shared free resource that
    # 504'd/timed out on three separate runs while building this. Re-query
    # only on --refresh.
    cache = Path(f"osm_{county}.json")
    if cache.exists() and not refresh:
        clubs = json.loads(cache.read_text(encoding="utf-8"))
        log.info(f"[{county}] OSM: {len(clubs)} clubs from cache {cache.name} (--refresh to re-query Overpass)")
        return clubs

    areas = COUNTY_BOUNDS[county]
    # Not every area we want sits at the same admin level: Greater London is
    # a REGION (level 5), and asking for it at 6 matches nothing at all —
    # the same silent zero Berkshire returns if you ask for it by name.
    level = COUNTY_ADMIN_LEVEL.get(county, DEFAULT_ADMIN_LEVEL)
    area_defs = "\n".join(
        f'area["name"="{a}"]["admin_level"="{level}"]->.a{i};' for i, a in enumerate(areas)
    )
    area_queries = "\n".join(f"nwr[\"leisure\"=\"golf_course\"](area.a{i});" for i in range(len(areas)))
    query = f"[out:json][timeout:60];\n{area_defs}\n(\n{area_queries}\n);\nout center tags;"

    last_err = None
    resp = None
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(url, data={"data": query},
                                 headers={"User-Agent": USER_AGENT}, timeout=90)
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            last_err = e
            log.warning(f"Overpass endpoint {url} failed ({e}) — trying next")
            resp = None
    if resp is None:
        raise last_err
    elements = resp.json().get("elements", [])

    seen = {}
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name or NOISE_NAME_RE.search(name):
            continue
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        # The query has always asked for tags; we were only keeping name and
        # coordinates and throwing the rest away. Plenty of OSM golf features
        # carry the club's website, which is exactly what fingerprinting
        # needs — and it is the ONLY source for a county whose union has no
        # usable directory (Hertfordshire's is offline).
        site = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
        seen.setdefault(name, {"name": name, "lat": lat, "lon": lon, "source": "osm",
                               "osm_site": _clean_url(site) or ""})
    log.info(f"[{county}] OSM: {len(elements)} raw features -> {len(seen)} distinct real clubs")
    clubs = list(seen.values())
    cache.write_text(json.dumps(clubs, indent=2), encoding="utf-8")
    return clubs


# --- Stage 1b: county union directory ----------------------------------------

COUNTY_UNION_URLS = {
    "kent": "https://www.kentgolf.org/countyclubs.php",
    "sussex": "https://www.sussexgolf.org/countyclubs.php",
    "surrey": "https://www.surreygolf.org/countyclubs.php",   # same CMS, 111 clubs
    "essex": "https://www.essexgolf.org/countyclubs.php",     # same CMS, 70 clubs
    # .org.uk, not .org — and the union's own "/clubs" page is a different
    # layout, but countyclubs.php is there and is the same CMS. 77 clubs.
    "hampshire": "https://www.hampshiregolf.org.uk/countyclubs.php",
    # Hertfordshire's union runs on Intelligent Golf rather than the shared
    # county CMS, and as of 2026-09-10 its site answers "Website Disabled".
    # No directory to merge, so that county is OSM-only and will miss any
    # club OSM lacks — re-check if the union site comes back.
    "hertfordshire": None,
    # Berkshire's union is Berks/Bucks/Oxon (bbogolf.com), a WordPress site
    # with no club directory to parse — and it would pull in two counties we
    # aren't covering anyway. OSM-only, which now works because we keep OSM's
    # own website tag.
    "berkshire": None,
    # There is no single London golf union; the county unions either side
    # already list the boroughs' clubs, and many London clubs are already
    # live because they sit inside our Kent/Surrey/Essex boundaries.
    # OSM-only, plus its website tags.
    "london": None,
}

SOCIAL_HOSTS = ("facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com")


def _clean_url(u: str) -> Optional[str]:
    """County union pages have malformed hrefs like 'http:////site.co.uk' or
    'http://http://site.co.uk' — collapse to a single valid scheme+host."""
    if not u:
        return None
    # Strip EVERY leading scheme, not just one: the directory emits
    # 'http://https://site', 'http://http://site' and 'http:////site'. The
    # first regex here only peeled one layer, leaving 'https://https://site'
    # — which requests then tried to connect to as host='https'.
    stripped = re.sub(r"^(?:https?:/+)+", "", u.strip())
    if not stripped or "." not in stripped.split("/")[0]:
        return None
    return "https://" + stripped


def enumerate_county_union(county: str, refresh: bool = False) -> dict[str, str]:
    """Returns {club_name: official_website_url}. Verified NOT to expose the
    club's booking platform (see module docstring) — only the site URL.
    Cached like the OSM data: ~80 detail-page fetches per county that give
    the same answer every time."""
    cache = Path(f"union_{county}.json")
    if cache.exists() and not refresh:
        sites = json.loads(cache.read_text(encoding="utf-8"))
        log.info(f"[{county}] union directory: {len(sites)} clubs from cache {cache.name} (--refresh to re-fetch)")
        return sites

    base = COUNTY_UNION_URLS.get(county)
    if not base:   # None or absent — county has no usable directory
        log.warning(f"No county union URL configured for '{county}'")
        return {}
    resp = requests.get(base, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    club_links = [a for a in soup.find_all("a", href=True) if "clubid=" in a["href"]]

    sites: dict[str, str] = {}
    for a in club_links:
        name = a.get_text(strip=True)
        href = a["href"] if a["href"].startswith("http") else urljoin(base, a["href"])
        try:
            r = requests.get(href, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
            soup2 = BeautifulSoup(r.text, "html.parser")
            ext = [
                _clean_url(x["href"]) for x in soup2.find_all("a", href=True)
                if x["href"].startswith("http")
                and urlparse(base).netloc.replace("www.", "") not in x["href"]   # the union's own pages
                and not any(s in x["href"] for s in SOCIAL_HOSTS)
            ]
            ext = [u for u in ext if u]
            if ext:
                sites[name] = ext[0]
        except requests.RequestException as e:
            log.warning(f"[{county} union] {name}: {e}")
        time.sleep(REQUEST_DELAY_SECONDS / 3)  # county pages are cheap same-host hits
    log.info(f"[{county}] union directory: {len(sites)}/{len(club_links)} clubs resolved to a website")
    cache.write_text(json.dumps(sites, indent=2), encoding="utf-8")
    return sites


# County unions list artisan sections, ladies' sections and society groups as
# separate "clubs". They play the host club's course and have no tee sheet of
# their own, so they are not candidates.
NOT_A_SEPARATE_COURSE_RE = re.compile(
    r"\bartisans?\b|\bladies only\b|\bfriends of\b|\bsociety\b|\bseniors\b|\bveterans\b",
    re.I)


def cmd_enumerate(args):
    osm = enumerate_osm(args.county, refresh=args.refresh)
    union_sites = enumerate_county_union(args.county, refresh=args.refresh)

    # Fuzzy-match union site names onto OSM names (county union naming
    # ("Ashford (Kent) Golf Club") and OSM naming ("Ashford Golf Club",
    # or "...Golf Course") differ slightly — norm_club_name() strips the
    # generic words so both converge).
    union_by_norm = {norm_club_name(k): v for k, v in union_sites.items()}
    for club in osm:
        n = norm_club_name(club["name"])
        site = union_by_norm.get(n)
        if not site:
            for uk, uv in union_sites.items():
                if norm_club_name(uk) in n or n in norm_club_name(uk):
                    site = uv
                    break
        # OSM's own website tag is the fallback when the union has no entry
        # (or no directory at all).
        club["official_site"] = site or club.get("osm_site") or None

    with_site = sum(1 for c in osm if c.get("official_site"))
    log.info(f"[{args.county}] matched {with_site}/{len(osm)} OSM clubs to a website via the county union")

    # Clubs the county union lists but OSM has no feature for were silently
    # DROPPED until 2026-09-10 — 49 across Kent/Sussex/Surrey/Essex, including
    # Rochford Hundred, Romford, Southend-on-Sea and Gosfield Lake. OSM
    # coverage of golf courses is good but not complete, and the union is the
    # authoritative membership list, so keep both. No lat/lon on these: the
    # postcode comes off the club's own site at probe time and geocode fills
    # the coordinates, exactly as it does for an OSM club with no postcode.
    osm_norms = {norm_club_name(c["name"]) for c in osm}

    def already_have(name: str) -> bool:
        n = norm_club_name(name)
        return any(n == o or (n and (n in o or o in n)) for o in osm_norms)

    extra = 0
    off_county = COUNTY_UNION_EXCLUDE.get(args.county)
    for name, site in union_sites.items():
        if already_have(name) or NOT_A_SEPARATE_COURSE_RE.search(name):
            continue
        if off_county and off_county.search(name):
            continue
        osm.append({"name": name, "official_site": site, "lat": None, "lon": None,
                    "source": "county_union"})
        osm_norms.add(norm_club_name(name))
        extra += 1
    if extra:
        log.info(f"[{args.county}] + {extra} club(s) the union lists that OSM does not have")

    Path(args.out).write_text(json.dumps(osm, indent=2), encoding="utf-8")
    log.info(f"Wrote {len(osm)} candidate(s) -> {args.out}")


# --- Stage 2: fingerprint each club's own site -------------------------------

PLATFORM_MARKERS = {
    "intelligent_golf": ["intelligentgolf.co.uk", "teebooking-teetimes"],
    "esp": ["e-s-p.com/elitelive", "elitelive/book_"],
    "golf_manager": ["golfmanager.com"],
    "clubv1": ["hub.clubv1.com"],
    # Deferred platforms — not yet scraped, but worth counting so future
    # scraper-building is prioritised by real numbers, not the old survey.
    "brs": ["brsgolf.com", "brs-golf.com"],
    "chronogolf": ["chronogolf.com", "lightspeedhq.com"],
    "shiji": ["shiji.aws.prop.cm", "conceptspaandgolf"],
    # Not a golf platform — a generic leisure-centre booking system that
    # council-run courses (MyTime Active: Orpington, Cobtree, Barnehurst)
    # sit behind. Counted so the "is it worth a scraper?" question has a number.
    "gladstone": ["gladstonego.cloud", "mytimeleisure.co.uk"],
    "golfnow": ["golfnow.co.uk", "golfnow.com"],  # explicitly out of scope, but worth logging as "seen"
}

BOOKING_LINK_RE = re.compile(r"book|tee.?time|visitor|green.?fee", re.I)
LOGIN_TITLE_RE = re.compile(r"login required|log in|sign in", re.I)

# A hub can exist for a club WITHOUT visitor booking being switched on: a
# ClubV1 hub answers HTTP 200 with "Permission Denied" on /Visitors/booking
# for those (Mid Kent, Dartford, Eltham Warren, Faversham, Bearsted). Landing
# on the platform's domain is NOT proof the sheet is open — the first version
# of this script counted five such clubs as "confirmed public".
DENIED_RE = re.compile(r"permission denied|do not have permission|access denied|not authori[sz]ed", re.I)

# A members' booking page can render real tee-sheet markup without a login
# (REGC's /memberbooking/ does), which the structural check would otherwise
# read as a confirmed public sheet. Members' paths are never visitor booking.
MEMBER_PATH_RE = re.compile(r"/memberbooking|/members?(?:/|$)", re.I)

# STRUCTURAL markers: the actual HTML/JS a platform's real booking page
# renders (the same selectors each scraper keys off). These are a strong
# same-domain signal — unlike a URL-domain match, they work for a
# white-labeled club whose booking page never leaves the club's own domain
# (theridge.co.uk, littlestonegolfclub.org.uk, ...), where there's no
# foreign hostname to match against.
PLATFORM_STRUCTURAL_MARKERS = {
    "intelligent_golf": ["teebooking-teetimes", "teetimes-slot"],
    "esp": ["espajax", "book_group.php", "book_date.php"],
    "golf_manager": ["ebookings/init.api", "golfmanager"],
    "clubv1": ["cv1hub-booking", "data-teetime"],
}

# Platforms with a fixed, near-universal booking-path convention, proven
# across every club checked this session (The Ridge, Wildernesse, Nizels,
# The Heron, Cinque Ports, Littlestone — all serve their sheet at exactly
# this path off their own domain or subdomain). Worth probing directly
# once we have ANY hint a club might be on this platform, rather than
# relying on the homepage happening to link straight to it — several real
# clubs' nav never links to the booking page directly at all.
PLATFORM_PATH_CONVENTIONS = {
    "intelligent_golf": "/visitorbooking/",
}


def _structural_hit(html: str) -> Optional[str]:
    low = html.lower()
    for plat, markers in PLATFORM_STRUCTURAL_MARKERS.items():
        if any(m in low for m in markers):
            return plat
    return None


@dataclass
class Candidate:
    name: str
    official_site: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    platform: str = "unknown"          # one of PLATFORM_MARKERS keys, or unknown/no_site/error/blocked
    # Deliberately separate from `platform`: a club can be confirmed on a
    # known platform AND still not be publicly bookable (Littlestone is
    # genuinely Intelligent Golf, but its booking page requires a login —
    # those are two different facts, and collapsing them into one field
    # loses the second one).
    access: str = "unknown"            # public | login_required | unknown
    evidence_url: str = ""
    confidence: str = "low"            # low | medium | high
    note: str = ""


def _fingerprint_url(url: str) -> Optional[str]:
    """STRONG signal: we are actually standing on the platform's own domain
    right now (redirected there, or a followed link landed us there)."""
    low = url.lower()
    for plat, markers in PLATFORM_MARKERS.items():
        if any(m in low for m in markers):
            return plat
    return None


def _find_platform_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """WEAK signal: this page's content merely LINKS to a platform domain
    somewhere (a "Powered by X" credit badge, a members-login nav item).
    Returns [(platform, absolute_url), ...] for those links so the caller can
    follow one before trusting it — never returned as evidence on its own.
    Caught concretely during this pipeline's own build: county-union pages
    link to intelligentgolf.co.uk in their OWN footer regardless of what
    platform any given club actually uses, and a club's own homepage can
    carry the same kind of decorative badge without it meaning the specific
    club's tee sheet is on that platform, or public."""
    soup = BeautifulSoup(html, "html.parser")
    hits = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        plat = _fingerprint_url(href) or _fingerprint_url(urljoin(base_url, href))
        if plat:
            hits.append((plat, urljoin(base_url, href)))
    return hits


# Hosts that are ONLY the vendor's marketing site — a club's sheet never lives
# here on ANY path (it lives on <slug>.<vendor> or the club's own domain).
# A "Powered by X" badge links to the root; www.intelligentgolf.co.uk/tee_times
# is the vendor's product page, and two council courses (Tilgate Forest,
# Rookwood) link straight to it — an earlier version "confirmed" them by
# landing there. e-s-p.com is the exception: its app genuinely lives on the
# shared host under /elitelive/, so only its other pages are marketing.
# www.chronogolf.com is NOT here: club widgets live under /club/<id>/.
VENDOR_MARKETING_HOSTS = {
    "intelligentgolf.co.uk", "www.intelligentgolf.co.uk",
    "golfmanager.com", "www.golfmanager.com",
    "clubv1.com", "www.clubv1.com", "hub.clubv1.com",
    "brsgolf.com", "www.brsgolf.com",
}


def _is_club_specific(url: str) -> bool:
    p = urlparse(url)
    host = p.netloc.lower()
    if host in VENDOR_MARKETING_HOSTS:
        return False
    if host in ("e-s-p.com", "www.e-s-p.com"):
        return "/elitelive" in p.path
    return True


# Landing on a platform's domain only counts as "visitor booking confirmed"
# if it's the VISITOR-facing part of that platform. A ClubV1 hub root with
# ?ReturnUrl=/members/... is the members' login (Kings Hill); a club's
# <slug>.intelligentgolf.co.uk homepage is its members' site. Anything else
# on the platform's domain is a hint to probe, not evidence.
VISITOR_PATH_RE = {
    "intelligent_golf": re.compile(r"/visitorbooking", re.I),
    "clubv1": re.compile(r"/Visitors/", re.I),
    "esp": re.compile(r"/elitelive/book_", re.I),
    "golf_manager": re.compile(r"/consumer/|ebookings", re.I),
    "brs": re.compile(r"visitors\.brsgolf\.com", re.I),
    "chronogolf": re.compile(r"/club/", re.I),
}


def _get_with_retry(session, url, attempts=2):
    """One retry with a short backoff — matches the scrapers' retry policy.
    Sites can 403 transiently (a WAF rule tripping on request pattern, not a
    real ban) as seen live during this pipeline's own validation run."""
    last = None
    for i in range(attempts):
        try:
            r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            return r
        except requests.RequestException as e:
            last = e
            if i < attempts - 1:
                time.sleep(3)
    raise last


def fingerprint_club(name: str, site: Optional[str], lat, lon, session: requests.Session) -> Candidate:
    cand = Candidate(name=name, official_site=site, lat=lat, lon=lon)
    if not site:
        cand.platform = "no_site"
        cand.note = "no official website resolved (not in county union directory)"
        return cand

    try:
        r = _get_with_retry(session, site)
    except requests.RequestException as e:
        cand.platform = "error"
        cand.note = f"homepage fetch failed: {e}"
        return cand

    if r.status_code != 200:
        # A non-200 (esp. 403) on the homepage means we likely got a WAF
        # challenge page, not real content — don't read platform markers out
        # of that, and don't let it silently pass as "unknown". Retry once
        # more after a longer pause; if it still fails, flag for a human/
        # browser-based look rather than guessing.
        time.sleep(5)
        try:
            r = _get_with_retry(session, site, attempts=1)
        except requests.RequestException:
            pass
        if r.status_code != 200:
            cand.confidence = "low"
            if r.status_code in (404, 410):
                # Not a block — the directory's URL for this club is stale.
                # Needs a fresh website lookup, not a browser retry.
                cand.platform = "dead_link"
                cand.note = f"directory URL returned HTTP {r.status_code} — stale link, needs a fresh website lookup"
            else:
                cand.platform = "blocked"
                cand.note = f"homepage returned HTTP {r.status_code} — likely bot-blocked; needs a browser-based check"
            return cand

    hinted_urls: list[tuple[str, str]] = []  # (platform, url) of each platform link seen — followed directly below
    weak_platforms: list[str] = []  # hinted at, not yet confirmed — ORDER MATTERS:
    # first-seen wins, deterministically, when a page hints at more than one
    # platform. A plain set() was tried first and produced a real bug: the
    # same club (Bearsted) came back as two different platforms across two
    # runs, because Python's set iteration order for strings is randomized
    # per-process. A review tool must give the same answer every run.

    def check_page(url) -> "tuple[str, str, str] | None":
        """Fetch url and classify it. Returns (kind, platform, evidence_url) where
        kind is 'login' | 'confirmed' | 'weak' | None (fetch failed/nothing)."""
        try:
            r2 = _get_with_retry(session, url, attempts=1)
        except requests.RequestException:
            return None
        if r2.status_code != 200:
            return None
        if LOGIN_TITLE_RE.search(r2.text[:2000]):
            return ("login", "", url)
        if DENIED_RE.search(r2.text[:3000]):
            return ("denied", _fingerprint_url(r2.url) or "", url)
        if MEMBER_PATH_RE.search(urlparse(r2.url).path):
            return ("denied", _fingerprint_url(r2.url) or _structural_hit(r2.text) or "", url)
        if _fingerprint_url(r2.url) == "golfnow":
            # A GolfNow listing, not the club's own booking. Excluded by policy
            # (it's already visible on GolfNow), so never "public".
            return ("denied", "golfnow", url)
        # STRONG: the page's actual markup is a real tee sheet, OR we're on
        # the platform's VISITOR-facing path — not its marketing site, not a
        # members' login/homepage that merely sits on the platform's domain.
        structural = _structural_hit(r2.text)
        if structural:
            return ("confirmed", structural, r2.url)
        by_url = _fingerprint_url(r2.url)
        if by_url:
            vp = VISITOR_PATH_RE.get(by_url)
            if _is_club_specific(r2.url) and (vp is None or vp.search(r2.url)):
                return ("confirmed", by_url, r2.url)
            # On the platform's domain but not its visitor sheet: hint only,
            # and remember the URL so the hinted-link probe can try the
            # proper visitor path from it (e.g. hub root -> /Visitors/booking).
            if by_url not in weak_platforms:
                weak_platforms.append(by_url)
            if (by_url, r2.url) not in hinted_urls:
                hinted_urls.append((by_url, r2.url))
        # WEAK: this page merely links out to a platform domain somewhere
        # (a badge/credit) — note it, don't trust it, caller may probe further.
        for plat, link in _find_platform_links(r2.text, r2.url):
            if plat not in weak_platforms:
                weak_platforms.append(plat)
            if (plat, link) not in hinted_urls:
                hinted_urls.append((plat, link))
        return ("weak", "", url)

    def _denied(plat_hint, evidence, where):
        cand.platform = plat_hint or (weak_platforms[0] if weak_platforms else "unknown")
        cand.confidence = "high"
        if plat_hint == "golfnow":
            cand.access = "out_of_scope"
            cand.note = f"GolfNow listing, not the club's own booking — excluded by policy: {evidence}"
        else:
            cand.access = "not_available"
            cand.note = (f"platform reached but not a public visitor sheet ({where}; "
                         f"members-only, or visitor booking not enabled): {evidence}")
        return cand

    def booking_ish_links(html, base_url):
        soup = BeautifulSoup(html, "html.parser")
        links = [
            a["href"] for a in soup.find_all("a", href=True)
            if BOOKING_LINK_RE.search(a.get_text(" ", strip=True)) or BOOKING_LINK_RE.search(a["href"])
        ]
        # "book" in the href itself outranks a generic "visitor info" page
        # whose link text merely mentions the word.
        strong = [l for l in links if re.search(r"book", l, re.I)]
        ordered, seen = [], set()
        for l in strong + [x for x in links if x not in strong]:
            full = urljoin(base_url, l)
            if full not in seen:
                seen.add(full)
                ordered.append(full)
        return ordered

    # Homepage itself: any platform links present are a weak hint only.
    for plat, link in _find_platform_links(r.text, r.url):
        if plat not in weak_platforms:
            weak_platforms.append(plat)
        if (plat, link) not in hinted_urls:
            hinted_urls.append((plat, link))

    to_visit = booking_ish_links(r.text, r.url)
    visited_pages: list[str] = []
    for full in to_visit[:3]:
        res = check_page(full)
        visited_pages.append(full)
        if not res:
            continue
        kind, plat, evidence = res
        if kind == "login":
            cand.access = "login_required"
            cand.platform = weak_platforms[0] if weak_platforms else "unknown"
            cand.confidence = "high"
            cand.note = f"booking link requires login: {evidence}"
            return cand
        if kind == "denied":
            return _denied(plat, evidence, "booking link")
        if kind == "confirmed":
            cand.platform, cand.evidence_url, cand.access = plat, evidence, "public"
            cand.confidence = "high"
            return cand

    # Bounded second hop from pages we actually reached but which resolved
    # nothing (e.g. a "green fees" page that itself links onward to the real
    # booking system — exactly how Littlestone is structured).
    for page_url in visited_pages[:2]:
        try:
            r3 = _get_with_retry(session, page_url, attempts=1)
        except requests.RequestException:
            continue
        if r3.status_code != 200:
            continue
        for full2 in booking_ish_links(r3.text, r3.url)[:2]:
            res = check_page(full2)
            if not res:
                continue
            kind, plat, evidence = res
            if kind == "login":
                cand.access = "login_required"
                cand.platform = weak_platforms[0] if weak_platforms else "unknown"
                cand.confidence = "high"
                cand.note = f"booking link (2 hops in) requires login: {evidence}"
                return cand
            if kind == "denied":
                return _denied(plat, evidence, "booking link, 2 hops in")
            if kind == "confirmed":
                cand.platform, cand.evidence_url, cand.access = plat, evidence, "public"
                cand.confidence = "high"
                return cand

    # Follow the actual platform links we saw before falling back to guessed
    # paths. A homepage "Members' Area" link pointing at <club>.hub.clubv1.com
    # never matches the booking-word heuristic, so it was only ever recorded
    # as a hint — fetching it lands on the platform's own domain, which IS a
    # confirmed signal. (A whole batch of ClubV1 clubs sat at "medium" for
    # exactly this reason.) Vendor marketing roots are skipped: landing on
    # www.intelligentgolf.co.uk proves nothing about this club's sheet.
    for plat, link in hinted_urls[:4]:
        if not _is_club_specific(link):
            continue
        probe = link
        if plat == "clubv1" and "hub.clubv1.com" in urlparse(link).netloc:
            # A ClubV1 hub's root is the members' login; the PUBLIC visitor
            # sheet is a fixed path off the same host (verified on Willingdon).
            # Probe that, or a members-only login page would be misread as
            # "visitor booking is gated".
            p = urlparse(link)
            probe = f"{p.scheme}://{p.netloc}/Visitors/booking"
        res = check_page(probe)
        if not res:
            continue
        kind, hit_plat, evidence = res
        if kind == "denied":
            return _denied(hit_plat or plat, evidence, "platform link")
        if kind == "confirmed":
            cand.platform, cand.evidence_url, cand.access = hit_plat, evidence, "public"
            cand.confidence = "high"
            cand.note = "confirmed by following the platform link found on the club's own site"
            return cand
        if kind == "login" and probe != link:
            # Only a VISITOR path answering with a login page means visitor
            # booking is gated. A members-area root doing so is just normal.
            cand.access = "login_required"
            cand.platform = plat
            cand.confidence = "high"
            cand.note = f"visitor booking path requires login: {evidence}"
            return cand

    # Nothing confirmed via crawling. If we have a weak hint AND that
    # platform has a known, near-universal booking-path convention, probe it
    # directly — this is exactly how Littlestone, Nizels and The Heron were
    # actually resolved this session: not by finding a link, but by trying
    # the platform's standard path once its identity was suspected.
    for plat in weak_platforms:
        path = PLATFORM_PATH_CONVENTIONS.get(plat)
        if not path:
            continue
        probe_url = urljoin(site, path)
        res = check_page(probe_url)
        if not res:
            continue
        kind, hit_plat, evidence = res
        if kind == "denied":
            return _denied(hit_plat or plat, evidence, f"{plat} convention path")
        if kind == "login":
            cand.access = "login_required"
            cand.platform = plat
            cand.confidence = "high"
            cand.note = f"{plat} convention path requires login: {evidence}"
            return cand
        if kind == "confirmed":
            cand.platform, cand.evidence_url, cand.access = hit_plat, evidence, "public"
            cand.confidence = "high"
            cand.note = f"confirmed via {plat}'s standard booking path"
            return cand

    if weak_platforms:
        cand.platform = weak_platforms[0]
        cand.confidence = "medium"
        cand.note = f"platform link(s) seen ({weak_platforms}) but no confirmed public booking page found"
        return cand

    cand.platform = "unknown"
    cand.note = f"homepage loaded ({r.url}) but no platform signal found on it or {len(to_visit)} booking-ish link(s)"
    return cand


def cmd_fingerprint(args):
    candidates = []
    for path in args.inputs:
        candidates.extend(json.loads(Path(path).read_text(encoding="utf-8")))
    log.info(f"Loaded {len(candidates)} candidate(s) from {len(args.inputs)} file(s)")

    already = set()
    if args.config and Path(args.config).exists():
        with open(args.config, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                already.add(norm_club_name(row["club_name"].split(" (")[0].strip()))
    # Acronym-named config entries can't be matched by normalisation alone:
    # "REGC (Devonshire)" vs OSM's "The Royal Eastbourne Golf Club" slipped
    # through as a new club and would have been proposed again under its
    # members' booking URL. Add an alias here whenever a config name is an
    # acronym or nickname for what OSM / the county union call the club.
    for short, long in KNOWN_ALIASES.items():
        if short in already:
            already.add(long)

    todo = [
        c for c in candidates
        if not any(norm_club_name(c["name"]) == a or norm_club_name(c["name"]) in a or a in norm_club_name(c["name"])
                   for a in already if a)
    ]
    log.info(f"{len(candidates) - len(todo)} already in {args.config}; {len(todo)} to fingerprint")

    if args.exclude:
        done = set()
        for path in args.exclude:
            with open(path, newline="", encoding="utf-8") as f:
                done |= {norm_club_name(r["name"]) for r in csv.DictReader(f)}
        todo = [c for c in todo if norm_club_name(c["name"]) not in done]
        log.info(f"--exclude: skipping {len(done)} already-reviewed club(s); {len(todo)} remain")

    if args.limit:
        todo = todo[: args.limit]
        log.info(f"--limit applied: fingerprinting {len(todo)}")

    session = requests.Session()
    results: list[Candidate] = []
    for i, c in enumerate(todo):
        cand = fingerprint_club(c["name"], c.get("official_site"), c.get("lat"), c.get("lon"), session)
        results.append(cand)
        log.info(f"[{i+1}/{len(todo)}] {c['name']:35} -> {cand.platform:16} ({cand.confidence})")
        time.sleep(REQUEST_DELAY_SECONDS)

    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()) if results else
                           ["name","official_site","lat","lon","platform","evidence_url","confidence","note"])
        w.writeheader()
        for c in results:
            w.writerow(asdict(c))
    log.info(f"Wrote {len(results)} row(s) -> {out_path}")

    from collections import Counter
    dist = Counter(c.platform for c in results)
    log.info(f"Platform distribution this batch: {dict(dist)}")


def cmd_recheck(args):
    """Re-fingerprint rows already in a review CSV, in place. For when the
    fingerprinting logic improves (as it did when 'Permission Denied' hubs
    were found being counted as public) and an existing batch needs
    correcting without re-visiting every club in it."""
    path = Path(args.review_csv)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fields = f.fieldnames if hasattr(f, "fieldnames") else None
    fields = list(rows[0].keys()) if rows else FIELDS_FALLBACK
    targets = [r for r in rows
               if (not args.platform or r["platform"] in args.platform)
               and (not args.access or r.get("access") in args.access)]
    log.info(f"rechecking {len(targets)} of {len(rows)} row(s)")
    session = requests.Session()
    changed = 0
    for r in targets:
        site = r.get("official_site") or None
        cand = fingerprint_club(r["name"], site, r.get("lat"), r.get("lon"), session)
        new = asdict(cand)
        before = (r["platform"], r.get("access"), r["confidence"])
        for k in ("platform", "access", "evidence_url", "confidence", "note"):
            r[k] = new[k]
        after = (r["platform"], r["access"], r["confidence"])
        if before != after:
            changed += 1
            log.info(f"  {r['name']:34} {before} -> {after}")
        time.sleep(REQUEST_DELAY_SECONDS)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    log.info(f"rewrote {path} — {changed} row(s) changed")


FIELDS_FALLBACK = ["name", "official_site", "lat", "lon", "platform", "access", "evidence_url", "confidence", "note"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discover golf club booking platforms (enumerate + fingerprint)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("enumerate", help="Build a candidate list for one county (OSM + union directory)")
    p1.add_argument("--county", required=True, choices=list(COUNTY_BOUNDS))
    p1.add_argument("--out", required=True)
    p1.add_argument("--refresh", action="store_true",
                    help="re-query Overpass and the county union instead of using cached osm_/union_ files")
    p1.set_defaults(func=cmd_enumerate)

    p2 = sub.add_parser("fingerprint", help="Detect each candidate's booking platform")
    p2.add_argument("inputs", nargs="+", help="One or more enumerate --out files")
    p2.add_argument("--config", default="clubs_config.csv", help="Skip clubs already configured")
    p2.add_argument("--out", default="candidates_review.csv")
    p2.add_argument("--limit", type=int, default=None, help="Only fingerprint the first N (for testing)")
    p2.add_argument("--exclude", nargs="*", default=[],
                    help="review CSVs from earlier batches — clubs in them are skipped (resume without redoing)")
    p2.set_defaults(func=cmd_fingerprint)

    p3 = sub.add_parser("recheck", help="Re-fingerprint rows of an existing review CSV in place")
    p3.add_argument("review_csv")
    p3.add_argument("--platform", nargs="*", default=[], help="only rows whose platform is one of these")
    p3.add_argument("--access", nargs="*", default=[], help="only rows whose access is one of these (e.g. public)")
    p3.set_defaults(func=cmd_recheck)

    a = ap.parse_args()
    a.func(a)
