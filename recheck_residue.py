"""
Re-check the clubs a plain fingerprint pass couldn't resolve.

`discover_clubs.py fingerprint` uses the project's honest, identifying user
agent and only looks at the pages it is pointed at. That leaves a residue —
403s, TLS errors, and "homepage loaded but no platform signal" — which is NOT
the same as "no online booking". The Kent pass recovered 16 clubs from that
residue and Surrey 5, so it is worth a second, more patient look:

  1. Re-fetch with a browser user agent and the bare domain. Most "blocked"
     and "error" rows are a WAF rejecting an unusual agent, or a certificate
     issued for a different hostname — not a real refusal.
  2. Follow the obvious booking sub-pages (/book-a-tee-time, /visitors,
     /green-fees) one hop, because plenty of sites keep the platform link off
     the homepage.
  3. Probe `<slug>.intelligentgolf.co.uk/visitorbooking/` whenever the club's
     site mentions Intelligent Golf at all. Several clubs exist ONLY there.

Writes a review CSV in the same shape as `fingerprint`, so the approved rows
flow through `probe_course_ids.py` and `apply_approved.py` unchanged.

Usage:
    python recheck_residue.py residue_essex.csv --out recheck_essex.csv
"""

import argparse
import csv
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
FIELDS = ["name", "official_site", "lat", "lon", "platform", "access", "evidence_url",
          "confidence", "note"]

PLATFORM_HOSTS = [
    ("intelligent_golf", r"intelligentgolf\.co\.uk|/visitorbooking"),
    ("esp", r"e-s-p\.com/elitelive|elitelive/book_"),
    ("golf_manager", r"golfmanager\.com"),
    ("clubv1", r"hub\.clubv1\.com"),
    ("brs", r"brsgolf\.com"),
    ("shiji", r"shiji\.aws\.prop\.cm"),
    ("gladstone", r"gladstonego\.cloud"),
    ("chronogolf", r"chronogolf\.com|lightspeedhq"),
    ("golfnow", r"golfnow\.co|teeitup"),
]
BOOKING_SUBPAGE_RE = re.compile(r"(book|tee[-_ ]?time|green[-_ ]?fee|visitor|play)", re.I)
ASSET_RE = re.compile(r"\.(css|js|png|jpe?g|svg|gif|pdf|woff2?)(\?|$)", re.I)
LOGIN_RE = re.compile(r"login required|please log ?in|sign in to book", re.I)


def get(session, url):
    try:
        r = session.get(url, headers=BROWSER, timeout=25, verify=False, allow_redirects=True)
        time.sleep(DELAY)
        return r
    except requests.RequestException:
        return None


def platform_links(html: str, base: str) -> dict:
    """platform -> first absolute URL seen for it on this page."""
    found = {}
    for href in re.findall(r'(?:href|src|action)=["\']([^"\']+)["\']', html):
        if ASSET_RE.search(href) or "facebook" in href or "linkedin" in href:
            continue
        for name, pattern in PLATFORM_HOSTS:
            if re.search(pattern, href, re.I) and name not in found:
                found[name] = urljoin(base, href)
    return found


def ig_subdomain_sheet(session, html: str):
    """A club's own Intelligent Golf subdomain, if its site links one."""
    m = re.search(r"https?://([a-z0-9-]+)\.intelligentgolf\.co\.uk", html, re.I)
    if not m or m.group(1) in ("www", "secure"):
        return None
    url = f"https://{m.group(1)}.intelligentgolf.co.uk/visitorbooking/"
    r = get(session, url)
    if r is None or r.status_code != 200 or "teebooking" not in r.text:
        return None
    return url, bool(LOGIN_RE.search(r.text))


def recheck(session, row: dict) -> dict:
    out = {f: row.get(f, "") for f in FIELDS}
    out["note"] = "rechecked with a browser user agent"
    site = (row.get("official_site") or "").strip()
    if not site:
        out["platform"], out["access"], out["confidence"] = "no_site", "unknown", "low"
        out["note"] = "still no website known"
        return out

    resp = get(session, site)
    if resp is None or resp.status_code >= 400:
        host = urlparse(site).netloc.replace("www.", "")
        resp = get(session, f"https://{host}") if host else None
    if resp is None:
        out["platform"], out["access"], out["confidence"] = "error", "unknown", "low"
        out["note"] = "unreachable even with a browser user agent"
        return out

    pages = [resp]
    subs = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', resp.text):
        if BOOKING_SUBPAGE_RE.search(href) and not ASSET_RE.search(href):
            u = urljoin(resp.url, href)
            if urlparse(u).netloc == urlparse(resp.url).netloc and u not in subs:
                subs.append(u)
    for u in subs[:5]:
        r = get(session, u)
        if r is not None and r.status_code == 200:
            pages.append(r)

    for page in pages:
        found = platform_links(page.text, page.url)
        if "golfnow" in found and len(found) == 1:
            out.update(platform="golfnow", access="out_of_scope", confidence="high",
                       evidence_url=found["golfnow"],
                       note="GolfNow/TeeItUp only — excluded by policy")
            return out
        for name, _ in PLATFORM_HOSTS:
            if name in found and name != "golfnow":
                url = found[name]
                is_visitor = re.search(r"visitorbooking|/Visitors/|book_start|book_date|"
                                       r"ebookings|casualBooking|visitors\.brsgolf|/book",
                                       url, re.I)
                out.update(platform=name, evidence_url=url,
                           access="public" if is_visitor else "unknown",
                           confidence="high" if is_visitor else "medium",
                           note="found on " + page.url)
                if is_visitor:
                    return out

    hit = ig_subdomain_sheet(session, " ".join(p.text for p in pages))
    if hit:
        url, needs_login = hit
        out.update(platform="intelligent_golf", evidence_url=url,
                   access="login_required" if needs_login else "public",
                   confidence="high",
                   note="club's own Intelligent Golf subdomain")
        return out

    if out["platform"] in ("blocked", "error", "no_site", ""):
        out["platform"], out["confidence"] = "unknown", "low"
    if not out["access"]:
        out["access"] = "unknown"
    out["note"] = f"site reachable ({resp.url}) but no visitor booking found on it or {len(subs[:5])} sub-page(s)"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Second-pass fingerprint for unresolved clubs")
    ap.add_argument("residue_csv")
    ap.add_argument("--out", default="recheck.csv")
    ap.add_argument("--skip-states", nargs="*", default=["not_available", "out_of_scope"],
                    help="access states already settled; don't waste requests on them")
    a = ap.parse_args()

    with open(a.residue_csv, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("access") not in a.skip_states]
    log.info(f"rechecking {len(rows)} club(s)")

    session = requests.Session()
    out = []
    for i, r in enumerate(rows, 1):
        res = recheck(session, r)
        out.append(res)
        log.info(f"[{i}/{len(rows)}] {r['name'][:34]:34} {r['platform']:10} -> "
                 f"{res['platform']:16} {res['access']:14} {res['evidence_url'][:44]}")

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    ready = sum(1 for r in out if r["confidence"] == "high" and r["access"] == "public")
    log.info(f"wrote {len(out)} row(s) -> {a.out}  ({ready} now look ready to probe)")


if __name__ == "__main__":
    main()
