"""
Find the official website for clubs the discovery pipeline never got one for.

coverage.csv's biggest "unresolved" bucket is "no working website found":
OpenStreetMap had no website tag for the club and no county union directory
listed it, so the fingerprint step had nothing to look at. Those clubs were
never judged; they were skipped.

Most golf clubs live at a predictable address — <name>golfclub.co.uk,
<name>gc.co.uk, <name>golf.com — so this guesses a short list of domains per
club and accepts one only if the page proves it is THIS club:

  - it must mention golf, and the club's distinctive name word;
  - it must not be a parked / for-sale domain or a directory site;
  - "high" confidence needs a UK postcode on the page that geocodes to
    within 15 km of where OSM puts the club. That is the check that stops
    Bradford Golf Club's site being accepted for a Bradford in Devon.
    Pages with no postcode but the name in the <title> are "medium".

It does NOT use a search engine. Guessing and checking a few domains per club
is polite (one DNS lookup for most guesses, a single page fetch for the few
that resolve), needs no API key, and scraping search results would breach
their terms.

Output is the enumerate format, so the hits go straight into the existing
pipeline:
    python find_websites.py --counties surrey london --out candidates_websites.json
    python discover_clubs.py fingerprint candidates_websites.json --out candidates_review_<county>.csv
"""

import argparse
import concurrent.futures as cf
import csv
import glob
import json
import math
import re
import socket
from pathlib import Path

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

UA = {"User-Agent": "ServiceSynkGolfAggregator/0.1 (contact: joe@servicesynk.com; research use)"}
socket.setdefaulttimeout(6)
MAX_KM = 15.0

# Not a club a visitor books a round at. Skipped with the reason recorded, so
# they can be marked out of scope rather than looked for again.
NOT_A_CLUB = re.compile(
    r"simulator|indoor|driving range|golf range|\brange\b|pitch\s*(and|&)?\s*putt|"
    r"footgolf|foot golf|mini golf|crazy golf|adventure golf|putting|"
    r"\bschool\b|\bcollege\b|hospital|holiday park|caravan", re.I)

GENERIC = {"the", "golf", "club", "course", "centre", "center", "complex", "leisure",
           "and", "country", "estate", "gc", "links", "gold"}

# Hosts that are about golf clubs but are not the club.
NOT_THE_CLUB = ("golfnow", "facebook.", "hole19", "golfshake", "ncg.co.uk", "top100golf",
                "wikipedia", "tripadvisor", "yell.com", "englandgolf", "golfpass", "teeitup",
                "brsgolf.com", "intelligentgolf.co.uk", "clubv1.com", "e-s-p.com",
                "golfmanager.com", "chronogolf.com", "instagram.", "twitter.", "linkedin.")

PARKED = re.compile(
    r"domain (is|may be) for sale|buy this domain|this domain has expired|parked free|"
    r"hugedomains|sedo\.com|dan\.com|domain name is registered|coming soon to|"
    r"account suspended|default web site page|welcome to nginx", re.I)

PC_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[ABD-HJLNP-UW-Z]{2})\b")


def haversine(a, b, c, d):
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def norm(s):
    s = re.sub(r"\b(golf|club|course|centre|center|links|and|the)\b", " ", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


def core_words(name):
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and ").replace("'", ""))
    return [w for w in words if w not in GENERIC]


def guesses(name):
    """Most likely first, so the first verified hit is usually the right one."""
    core = core_words(name)
    if not core:
        return []
    bases = ["".join(core)]
    if len(core) > 1:
        bases.append("-".join(core))
    out = []
    for base in bases:
        for suffix in ("golfclub", "gc", "golf", "golfcourse", ""):
            for tld in (".co.uk", ".com", ".org.uk", ".uk"):
                label = f"{base}{suffix}" if suffix else base
                if suffix and "-" in base:
                    label = f"{base}-{suffix}"
                if len(label) > 3:
                    out.append(label + tld)
    return list(dict.fromkeys(out))


def resolves(host):
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def page_text(html):
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    body = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.I)
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    return (title.group(1).strip() if title else ""), body


def postcodes_near(postcodes, lat, lon):
    """Nearest distance (km) of any real postcode on the page to the club."""
    if not postcodes or lat is None:
        return None
    try:
        res = requests.post("https://api.postcodes.io/postcodes", json={"postcodes": postcodes[:10]},
                            headers=UA, timeout=20).json().get("result", [])
    except (requests.RequestException, ValueError):
        return None
    dists = [haversine(lat, lon, x["result"]["latitude"], x["result"]["longitude"])
             for x in res if x.get("result")]
    return min(dists) if dists else None


def check_site(domain, name, lat, lon, session):
    distinctive = max(core_words(name), key=len, default="")
    for url in (f"https://www.{domain}", f"https://{domain}"):
        host = url.split("//", 1)[1]
        if not resolves(host):
            continue
        try:
            r = session.get(url, headers=UA, timeout=12, allow_redirects=True)
        except requests.RequestException:
            continue
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            continue
        final = r.url.lower()
        if any(h in final for h in NOT_THE_CLUB):
            return None
        title, body = page_text(r.text)
        low = (title + " " + body).lower()
        if PARKED.search(low) or "golf" not in low:
            return None
        if distinctive and distinctive not in low.replace(" ", "") and distinctive not in low:
            return None
        pcs = list(dict.fromkeys(f"{a} {b}" for a, b in PC_RE.findall(body.upper())))
        near = postcodes_near(pcs, lat, lon)
        if near is not None and near <= MAX_KM:
            return {"site": r.url, "confidence": "high",
                    "evidence": f"postcode on page {near:.1f} km from the club; title: {title[:60]}"}
        if near is not None:
            # A real postcode, but somewhere else: another club of the same name.
            return None
        if distinctive and distinctive in title.lower() and "golf" in title.lower():
            return {"site": r.url, "confidence": "medium",
                    "evidence": f"no postcode on page; club name in title: {title[:60]}"}
    return None


def find_for(club, session):
    name = club["name"]
    if NOT_A_CLUB.search(name):
        return {**club, "result": "skipped", "reason": "not a club a visitor books a round at"}
    for domain in guesses(name):
        hit = check_site(domain, name, club.get("lat"), club.get("lon"), session)
        if hit:
            return {**club, "result": "found", **hit}
    return {**club, "result": "not_found", "reason": "no guessed domain verified"}


def load_clubs(counties):
    """Unresolved no-website clubs for the given coverage labels, with the
    coordinates from THAT county's own candidate file only — a name match
    across every county file put a Lincolnshire club's position on a St
    Helens club once."""
    stems = {Path(p).stem.replace("candidates_", "").replace("_", ""): p
             for p in glob.glob("candidates_*.json")}
    rows = [r for r in csv.DictReader(open("coverage.csv", encoding="utf-8"))
            if r["status"] == "unresolved" and "no working website" in r["reason"]
            and r["county"] in counties]
    coords_by_label = {}
    for label in counties:
        coords = {}
        for part in label.split("/"):
            path = stems.get(part)
            if not path:
                continue
            for c in json.loads(Path(path).read_text(encoding="utf-8")):
                if isinstance(c, dict) and c.get("lat") is not None:
                    coords.setdefault(norm(c["name"]), (c["lat"], c["lon"]))
        coords_by_label[label] = coords
    clubs = []
    for r in rows:
        lat, lon = coords_by_label.get(r["county"], {}).get(norm(r["club"]), (None, None))
        clubs.append({"name": r["club"], "county": r["county"], "lat": lat, "lon": lon})
    return clubs


def main():
    ap = argparse.ArgumentParser(description="Find official websites for unresolved clubs")
    ap.add_argument("--counties", nargs="+", required=True, help="coverage.csv county labels")
    ap.add_argument("--out", required=True, help="enumerate-format JSON of clubs whose site was found")
    ap.add_argument("--report", default=None, help="CSV of every club tried and the outcome")
    ap.add_argument("--workers", type=int, default=10)
    a = ap.parse_args()

    clubs = load_clubs(set(a.counties))
    print(f"{len(clubs)} unresolved no-website club(s) in {', '.join(a.counties)}")
    session = requests.Session()
    results = []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as pool:
        for i, res in enumerate(pool.map(lambda c: find_for(c, session), clubs), 1):
            results.append(res)
            tag = {"found": "OK ", "skipped": "-- ", "not_found": "   "}[res["result"]]
            detail = res.get("site") or res.get("reason", "")
            print(f"[{i}/{len(clubs)}] {tag} {res['name'][:40]:42s} {res.get('confidence', ''):6s} {detail[:60]}",
                  flush=True)

    found = [r for r in results if r["result"] == "found"]
    Path(a.out).write_text(json.dumps(
        [{"name": r["name"], "lat": r["lat"], "lon": r["lon"], "source": "website_pass",
          "osm_site": "", "official_site": r["site"]} for r in found], indent=1), encoding="utf-8")
    report = a.report or Path(a.out).with_suffix(".csv")
    with open(report, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "county", "result", "confidence", "site", "evidence", "reason"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    counts = {k: sum(1 for r in results if r["result"] == k) for k in ("found", "skipped", "not_found")}
    high = sum(1 for r in found if r["confidence"] == "high")
    print(f"\n{counts}; {high} of the found are high confidence -> {a.out}, report {report}")


if __name__ == "__main__":
    main()
