"""
Work through clubs where Intelligent Golf was SEEN but no visitor sheet was
ever confirmed, and say which of the four things each one actually is.

The fingerprint marks these "unknown": it found an IG marker on the club's
site but never landed on a public tee sheet. That is not the same as "no
visitor booking", and the difference can only be settled by opening the page.
Two real examples from Essex: Chelmsford's `/visitorbooking/` is its
**Competition Bookings** page, and Colchester's needs a member login. Both
look identical to a pattern-matcher and neither is a visitor sheet.

Outcomes:
  public          a real visitor tee sheet, open — worth probing for ids
  login           the sheet exists but demands a member login
  not_a_sheet     the path resolves to something else (competitions, an
                  events form, the vendor's own marketing site)
  already_live    the sheet is one we ALREADY scrape under another club name
                  (Basingstoke Pitch and Putt is Basingstoke Golf Club's
                  sheet; South Petersfield is Petersfield's)
  unreachable     nothing answered

Usage:
    python ig_check.py candidates_igcheck.json --out ig_check.csv
"""

import argparse
import csv
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
DELAY = 1.2
FIELDS = ["club", "outcome", "sheet_url", "title", "detail"]

SHEET_MARKER = "teebooking"
LOGIN_RE = re.compile(r"login required|please log ?in|sign in to book", re.I)
# A visitor path can serve a COMPETITION sheet — same markup, wrong thing.
NOT_VISITOR_TITLE_RE = re.compile(r"competition|open entry|society|event", re.I)
VENDOR_HOST_RE = re.compile(r"^(www\.)?intelligentgolf\.co\.uk$", re.I)


def get(session, url):
    try:
        r = session.get(url, headers=BROWSER, timeout=25, verify=False, allow_redirects=True)
        time.sleep(DELAY)
        return r
    except requests.RequestException:
        return None


def title_of(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else ""


def candidate_urls(session, row: dict) -> list:
    """Every plausible visitor-sheet URL for this club, best first."""
    urls = []
    ev = (row.get("evidence_url") or "").strip()
    site = (row.get("official_site") or "").strip()
    if ev and not VENDOR_HOST_RE.match(urlparse(ev).netloc):
        urls.append(ev if ev.rstrip("/").endswith("visitorbooking")
                    else urljoin(ev.rstrip("/") + "/", "visitorbooking/"))
    if site and not VENDOR_HOST_RE.match(urlparse(site).netloc):
        urls.append(urljoin(site.rstrip("/") + "/", "visitorbooking/"))
        # The club's own IG subdomain, if its site names one anywhere.
        r = get(session, site)
        if r is not None:
            m = re.search(r"https?://([a-z0-9-]+)\.intelligentgolf\.co\.uk", r.text, re.I)
            if m and m.group(1) not in ("www", "secure"):
                urls.append(f"https://{m.group(1)}.intelligentgolf.co.uk/visitorbooking/")
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def classify(session, row: dict, live_sheets: set) -> dict:
    out = {f: "" for f in FIELDS}
    out["club"] = row["name"]
    for url in candidate_urls(session, row):
        r = get(session, url)
        if r is None or r.status_code != 200:
            continue
        out["sheet_url"] = r.url
        out["title"] = title_of(r.text)
        key = r.url.split("?")[0].rstrip("/").lower()
        if key in live_sheets:
            out["outcome"] = "already_live"
            out["detail"] = "this sheet is already scraped under another club name"
            return out
        if SHEET_MARKER not in r.text:
            out["outcome"] = "not_a_sheet"
            out["detail"] = "no tee-sheet markup on the page"
            continue
        if LOGIN_RE.search(r.text):
            out["outcome"] = "login"
            out["detail"] = "tee sheet present but requires a member login"
            return out
        if NOT_VISITOR_TITLE_RE.search(out["title"]):
            out["outcome"] = "not_a_sheet"
            out["detail"] = f"tee-sheet markup, but the page is: {out['title']}"
            return out
        out["outcome"] = "public"
        out["detail"] = "public visitor tee sheet"
        return out
    if not out["outcome"]:
        out["outcome"] = "unreachable" if not out["sheet_url"] else "not_a_sheet"
        out["detail"] = out["detail"] or "no visitor sheet found at any known path"
    return out


def live_sheet_keys(path="clubs_config.csv") -> set:
    keys = set()
    try:
        for r in csv.DictReader(open(path, newline="", encoding="utf-8")):
            if r.get("platform") == "intelligent_golf" and r.get("base_url"):
                keys.add(r["base_url"].split("?")[0].rstrip("/").lower())
    except FileNotFoundError:
        pass
    return keys


def main() -> None:
    ap = argparse.ArgumentParser(description="Settle the 'IG seen, sheet unconfirmed' clubs")
    ap.add_argument("candidates")
    ap.add_argument("--out", default="ig_check.csv")
    a = ap.parse_args()

    rows = json.load(open(a.candidates, encoding="utf-8"))
    live = live_sheet_keys()
    session = requests.Session()
    out = []
    for i, row in enumerate(rows, 1):
        res = classify(session, row, live)
        out.append(res)
        log.info(f"[{i}/{len(rows)}] {row['name'][:34]:34} {res['outcome']:13} {res['detail'][:44]}")

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    from collections import Counter
    log.info(f"wrote {len(out)} row(s) -> {a.out}  {dict(Counter(r['outcome'] for r in out))}")


if __name__ == "__main__":
    main()
