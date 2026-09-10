"""
Fill course_profiles.csv — course TYPE and YARDAGE for each configured course.

The search page can say "20 tee times from £45", which only helps someone who
already knows the courses. Type ("links", "parkland") and length are what a
stranger actually chooses on. This is static data: it belongs in a
hand-checked CSV in the repo, not in the 2-hourly scrape.

This script only PROPOSES rows, by reading each club's own website. Every row
is eyeballed before it goes near the database, because a wrong yardage is
worse than a blank one.

Yardage is deliberately fuzzy: a course has a different length off every tee.
We take the largest plausible 18-hole figure on the page (the medal/white
tee), which is the number clubs quote themselves, and keep every candidate we
saw in `all_yardages` so a human can spot a scorecard we misread.

Usage:
    python enrich_courses.py --out course_profiles.csv
    python enrich_courses.py --out course_profiles.csv --only "Knole Park"
"""

import argparse
import csv
import glob
import json
import logging
import re
import time
from urllib.parse import urljoin, urlparse

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
import urllib3

urllib3.disable_warnings()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("golf")

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
DELAY = 1.0
FIELDS = ["club_name", "course_type", "yardage", "all_yardages", "source_url", "note"]

# Ordered: first match wins, so the more distinctive styles come first. A club
# described as both heathland and parkland is nearly always sold as the former.
#
# "links" needs a much stricter pattern than the rest. A bare \blinks\b matches
# the "Quick Links" in almost every site's footer, which labelled Bramley,
# Chobham, Guildford and Sheerness as links courses on the first pass. Demand
# a golf phrase instead and accept a few false negatives; a parkland course
# advertised as links is a far worse error than a blank.
# "links-style" / "links-like" means "inland course that plays a bit like
# one" — Thames Ditton & Esher is on common land and was published as links
# on the previous pass. A golfer filtering for links wants the coast.
LINKS_RE = (r"(?<!-)\b(?:championship|seaside|traditional|classic|true|original|authentic|natural|"
            r"coastal|old|magnificent)\s+links\b(?![- ]?(?:style|like|esque))"
            r"|\blinks\s+(?:course|golf|layout|land|holes?)\b(?![- ]?(?:style|like))"
            r"|\b(?:a|the)\s+links\s+(?:course|golf|experience)\b")
LINKS_STYLE_RE = re.compile(r"links[- ]?(?:style|like|esque)", re.I)
TYPES = [
    ("links", LINKS_RE),
    ("heathland", r"\bheath ?land\b"),
    ("downland", r"\bdown ?land\b"),
    ("moorland", r"\bmoor ?land\b"),
    ("clifftop", r"\bcliff ?top\b"),
    # "woodland walks" is a clubhouse-brochure phrase, so ask for a golf noun.
    ("woodland", r"\bwood ?land\s+(?:course|golf|layout|holes?)\b|\b(?:course|layout)\b[^.]{0,25}\bwood ?land\b"),
    ("meadowland", r"\bmeadow ?land\b"),
    ("parkland", r"\bpark ?land\b"),
]

# "6,432 yards", "6432 yds" — bounded so a single hole ("180 yards") and a
# phone number can't be mistaken for a course length. The second pattern
# catches the way clubs usually phrase it in prose ("measuring 6,432",
# "par 71, 6,432"), which the first pass missed on most sites.
YARDAGE_RES = [
    re.compile(r"\b(\d{1,2},?\d{3})\s*(?:yards?|yds?)\b", re.I),
    re.compile(r"\b(?:measur\w+|length|plays?|extends?|stretch\w*|total)\b[^.\d]{0,25}"
               r"(\d{1,2},?\d{3})\b", re.I),
    re.compile(r"\bpar\s*\d{2}\b[^.\d]{0,25}(\d{1,2},?\d{3})\b", re.I),
    re.compile(r"\b(\d{1,2},?\d{3})\b[^.\d]{0,15}\bpar\s*\d{2}\b", re.I),
]
YARDAGE_MIN, YARDAGE_MAX = 4000, 7600

CANDIDATE_PATHS = ["", "the-course", "course", "our-course", "golf-course", "the-golf-course",
                   "golf", "course-guide", "the-course/course-guide", "course/course-guide",
                   "scorecard", "course/scorecard", "the-course/scorecard", "course/the-course",
                   "about", "about-us", "visitors", "green-fees", "play", "club/the-course"]


def clean_text(html: str) -> str:
    html = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def official_sites() -> dict:
    """club name -> its own website, from every source the project already has.

    The review CSVs only cover clubs the discovery pipeline found. The 16 from
    the original manual survey (Knole Park, Wildernesse, West Malling, REGC…)
    predate it, and their base_url is an intelligentgolf.co.uk subdomain, so
    the first pass had no site for them at all and returned nothing. The
    county-union cache (union_*.json) and the OSM candidate files carry those.
    """
    sites: dict = {}
    for path in glob.glob("candidates_review*.csv") + glob.glob("residue*.csv"):
        try:
            for r in csv.DictReader(open(path, newline="", encoding="utf-8")):
                if r.get("official_site"):
                    sites.setdefault(r["name"], r["official_site"])
        except (OSError, KeyError):
            continue
    for path in glob.glob("union_*.json"):
        try:
            for name, url in json.loads(open(path, encoding="utf-8").read()).items():
                if url:
                    sites.setdefault(name, url)
        except (OSError, ValueError, AttributeError):
            continue
    for path in glob.glob("candidates_*.json"):
        try:
            data = json.loads(open(path, encoding="utf-8").read())
            items = data if isinstance(data, list) else data.get("candidates", [])
            for c in items:
                if isinstance(c, dict) and c.get("name"):
                    url = c.get("official_site") or c.get("website") or c.get("url")
                    if url:
                        sites.setdefault(c["name"], url)
        except (OSError, ValueError, AttributeError):
            continue
    return sites


def norm(s: str) -> str:
    s = re.sub(r"\b(golf|club|course|centre|center|links|and|the)\b", " ", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


# Booking hosts that are the PLATFORM's, not the club's — never scrape these
# for course facts, they describe the vendor.
SHARED_HOSTS = ("e-s-p.com", "brsgolf.com", "clubv1.com", "golfmanager.com",
                "gladstonego.cloud", "intelligentgolf.co.uk", "shiji")


# config name -> the name the directories use. Same problem KNOWN_ALIASES
# solves in discover_clubs.py.
NAME_ALIASES = {"regc": "royaleastbourne"}


def site_for(club_name: str, base_url: str, sites: dict) -> str:
    """The club's own website.

    A white-labelled booking URL on the club's OWN domain is checked first and
    beats the directory: we scrape that host every two hours, so we know it
    resolves, whereas a county-union link can be years stale (its entry for
    The Ridge points at a domain that no longer answers).
    """
    host = urlparse(base_url).netloc
    if host and not any(h in host for h in SHARED_HOSTS):
        return f"https://{host}"
    key = norm(club_name.split(" (")[0])
    key = NAME_ALIASES.get(key, key)
    for name, url in sites.items():
        if norm(name) == key:
            return url
    for name, url in sites.items():
        if key and (key in norm(name) or norm(name) in key):
            return url
    return ""


def profile_for(session, club_name: str, site: str) -> dict:
    row = {f: "" for f in FIELDS}
    row["club_name"] = club_name
    row["source_url"] = site
    if not site:
        row["note"] = "no club website known — fill by hand"
        return row

    seen_types = []
    yardages = []
    used_url = ""
    for path in CANDIDATE_PATHS:
        url = urljoin(site.rstrip("/") + "/", path)
        try:
            resp = session.get(url, headers=BROWSER, timeout=25, verify=False)
            time.sleep(DELAY)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        text = clean_text(resp.text)
        for label, pattern in TYPES:
            if label == "links" and LINKS_STYLE_RE.search(text):
                continue
            if re.search(pattern, text, re.I) and label not in seen_types:
                seen_types.append(label)
        for rx in YARDAGE_RES:
            for m in rx.finditer(text):
                n = int(m.group(1).replace(",", ""))
                if YARDAGE_MIN <= n <= YARDAGE_MAX:
                    yardages.append(n)
        if not used_url and (yardages or seen_types):
            used_url = resp.url
        if yardages and seen_types:
            break

    # "links" outranks everything in the TYPES order because it is the most
    # distinctive style — but it is also the word most often used loosely
    # ("links-style", "the links" as a synonym for the course). When a page
    # offers a second, more specific style as well, that one is the safer
    # answer: Kings Hill is heathland and Guildford is downland, and both were
    # published as links on the previous pass.
    if len(seen_types) > 1 and seen_types[0] == "links":
        seen_types = seen_types[1:] + ["links"]
    row["course_type"] = seen_types[0] if seen_types else ""
    row["all_yardages"] = " ".join(str(n) for n in sorted(set(yardages), reverse=True)[:6])
    row["yardage"] = str(max(yardages)) if yardages else ""
    row["source_url"] = used_url or site
    notes = []
    if len(seen_types) > 1:
        notes.append("types seen: " + "/".join(seen_types))
    if len(set(yardages)) > 1:
        notes.append("several yardages — check which tee")
    if not yardages:
        notes.append("no yardage found")
    row["note"] = "; ".join(notes)
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description="Propose course type/yardage rows for review")
    ap.add_argument("--config", default="clubs_config.csv")
    ap.add_argument("--out", default="course_profiles.csv")
    ap.add_argument("--only", nargs="*", default=[], help="club names (substring) to (re)do")
    ap.add_argument("--missing", action="store_true",
                    help="only rows --out has no entry for, and MERGE into it "
                         "(the usual case after adding a county)")
    a = ap.parse_args()

    sites = official_sites()
    with open(a.config, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("base_url")]
    if a.only:
        rows = [r for r in rows if any(o.lower() in r["club_name"].lower() for o in a.only)]
    existing: dict = {}
    if a.missing:
        try:
            with open(a.out, newline="", encoding="utf-8") as f:
                existing = {r["club_name"]: r for r in csv.DictReader(f)}
        except FileNotFoundError:
            existing = {}
        rows = [r for r in rows if r["club_name"] not in existing]
        log.info(f"{len(existing)} row(s) already in {a.out}; {len(rows)} to do")

    session = requests.Session()
    out = []
    for i, r in enumerate(rows, 1):
        site = site_for(r["club_name"], r["base_url"], sites)
        prof = profile_for(session, r["club_name"], site)
        out.append(prof)
        log.info(f"[{i}/{len(rows)}] {r['club_name'][:38]:38} "
                 f"{prof['course_type'] or '-':11} {prof['yardage'] or '-':6} {prof['note'][:42]}")

    if a.missing and existing:
        # Merge, never clobber: course_profiles.csv is hand-checked, and a
        # rerun must not throw away corrections made to rows it isn't redoing.
        merged = dict(existing)
        for r in out:
            merged[r["club_name"]] = r
        out = [merged[k] for k in sorted(merged)]
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    both = sum(1 for r in out if r["course_type"] and r["yardage"])
    log.info(f"wrote {len(out)} row(s) -> {a.out}  ({both} with both type and yardage; "
             f"EYEBALL these before loading)")


if __name__ == "__main__":
    main()
