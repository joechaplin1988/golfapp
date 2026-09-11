"""
Build coverage.csv — ONE reviewable list of every club we know about.

Until now the answer to "which clubs are in, which are out, and why?" was
spread across clubs_config.csv, seven per-county review CSVs, the residue and
recheck files, and the approved_rows intermediates — several of which are
gitignored, so they weren't even in the repo. This merges all of it into a
single file, one row per club, with a status and a plain-English reason.

Status values, most-included first:

  live                 scraped every run and searchable
  parked               in the config with scrape_enabled=false; kept, with the
                       reason, but skipped (their host blocks our CI runner)
  no_visitor_booking   a real club with no public online visitor booking:
                       platform reached but visitor booking is off, or the
                       booking page needs a member login
  out_of_scope         GolfNow / TeeItUp only — excluded by policy
  no_scraper           on a platform we don't scrape (one-off systems)
  unresolved           we couldn't tell; the honest "not looked at properly"
                       bucket, and where any future recovery will come from

Usage:
    python coverage_report.py                 # writes coverage.csv + a summary
    python coverage_report.py --out foo.csv
"""

import argparse
import csv
import glob
import logging
import os
import re
from collections import Counter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("golf")

FIELDS = ["club", "status", "county", "platform", "sheets", "postcode", "reason", "evidence_url"]

SCRAPED_PLATFORMS = ("intelligent_golf", "esp", "golf_manager", "clubv1", "brs",
                     "shiji", "gladstone", "chronogolf")

# Why a club is parked. Keyed on the club name as it appears in the config.
PARK_REASONS = {
    "High Elms Golf Course": "Mytime Active's Cloudflare blocks the CI runner (works from a normal connection)",
    "Orpington Golf Centre": "Mytime Active's Cloudflare blocks the CI runner (works from a normal connection)",
    "Bromley Golf Centre": "Mytime Active's Cloudflare blocks the CI runner (works from a normal connection)",
    "Belhus Park Golf Club": "Impulse Leisure's Cloudflare blocks the CI runner (works from a normal connection)",
    "Lickey Hills Golf Course": "Mytime Active's Cloudflare blocks the CI runner (works from a normal connection)",
}


def norm(s: str) -> str:
    s = re.sub(r"\b(golf|club|course|centre|center|links|and|the)\b", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def base_name(config_name: str) -> str:
    """'Hever Castle (Princes)' -> 'Hever Castle'."""
    return re.sub(r"\s*\(.*\)\s*$", "", config_name or "").strip()


def county_of(path: str) -> str:
    m = re.search(r"candidates_review_?([a-z]*)\.csv$", os.path.basename(path))
    name = (m.group(1) if m else "") or ""
    return {"": "kent/sussex", "2": "kent/sussex", "3": "kent/sussex",
            "batch1": "kent/sussex", "recovered": "kent/sussex/surrey"}.get(name, name)


def load_config() -> dict:
    """normalised club name -> live/parked row."""
    out = {}
    try:
        rows = list(csv.DictReader(open("clubs_config.csv", newline="", encoding="utf-8")))
    except FileNotFoundError:
        return out
    for r in rows:
        if not (r.get("base_url") or "").strip():
            continue
        club = base_name(r["club_name"])
        enabled = str(r.get("scrape_enabled", "") or "").strip().lower() not in {"false", "0", "no"}
        e = out.setdefault(norm(club), {
            "club": club, "platform": r["platform"], "sheets": 0,
            "postcode": r.get("postcode", ""), "enabled": enabled,
        })
        e["sheets"] += 1
        e["enabled"] = e["enabled"] and enabled
    return out


def load_reviews() -> dict:
    """normalised club name -> the most informative fingerprint row we have.

    A club can appear in several files (fingerprint, then residue recheck).
    Later, more specific answers win: a recheck that found a platform beats
    the original "unknown".
    """
    best = {}
    order = glob.glob("candidates_review*.csv") + glob.glob("recheck_*.csv")
    for path in order:
        try:
            rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
        except (OSError, KeyError):
            continue
        for r in rows:
            key = norm(r.get("name", ""))
            if not key:
                continue
            cand = {
                "club": r.get("name", ""), "county": county_of(path),
                "platform": r.get("platform", ""), "access": r.get("access", ""),
                "note": r.get("note", ""), "evidence_url": r.get("evidence_url", ""),
                "confidence": r.get("confidence", ""),
            }
            prev = best.get(key)
            if prev is None or _informativeness(cand) > _informativeness(prev):
                cand["county"] = cand["county"] or (prev or {}).get("county", "")
                best[key] = cand
    return best


def _informativeness(r: dict) -> int:
    if r["platform"] in SCRAPED_PLATFORMS and r["access"] == "public":
        return 4
    if r["platform"] in SCRAPED_PLATFORMS:
        return 3
    if r["access"] in ("not_available", "login_required", "out_of_scope"):
        return 2
    if r["platform"] not in ("", "unknown", "no_site", "error", "blocked", "dead_link"):
        return 1
    return 0


def classify(review: dict) -> tuple[str, str]:
    p, a = review["platform"], review["access"]
    if a == "out_of_scope" or p == "golfnow":
        return "out_of_scope", "GolfNow/TeeItUp only — excluded by policy"
    if a == "login_required":
        return "no_visitor_booking", "booking page requires a member login"
    if a == "not_available":
        return "no_visitor_booking", f"on {p}, but visitor booking is switched off"
    if p in SCRAPED_PLATFORMS:
        return "unresolved", f"{p} seen but no confirmed public visitor sheet — worth another look"
    if p in ("no_site", "dead_link"):
        return "unresolved", "no working website found"
    if p in ("blocked", "error"):
        return "unresolved", "site refused or failed to load"
    if p and p != "unknown":
        return "no_scraper", f"uses {p}, which we don't scrape"
    return "unresolved", review["note"][:110] or "no platform signal found on the site"


def main() -> None:
    ap = argparse.ArgumentParser(description="One reviewable list of every club we know about")
    ap.add_argument("--out", default="coverage.csv")
    a = ap.parse_args()

    config, reviews = load_config(), load_reviews()
    rows = []

    for key, c in config.items():
        rv = reviews.get(key, {})
        parked = not c["enabled"]
        rows.append({
            "club": c["club"],
            "status": "parked" if parked else "live",
            "county": rv.get("county", ""),
            "platform": c["platform"],
            "sheets": c["sheets"],
            "postcode": c["postcode"],
            "reason": PARK_REASONS.get(c["club"], "") if parked else "",
            "evidence_url": rv.get("evidence_url", ""),
        })

    for key, rv in reviews.items():
        if key in config:
            continue
        status, reason = classify(rv)
        rows.append({
            "club": rv["club"], "status": status, "county": rv["county"],
            "platform": rv["platform"], "sheets": 0, "postcode": "",
            "reason": reason, "evidence_url": rv["evidence_url"],
        })

    order = {"live": 0, "parked": 1, "no_visitor_booking": 2, "no_scraper": 3,
             "out_of_scope": 4, "unresolved": 5}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["county"], r["club"].lower()))

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    counts = Counter(r["status"] for r in rows)
    log.info(f"wrote {len(rows)} club(s) -> {a.out}")
    for s in sorted(counts, key=lambda x: order.get(x, 9)):
        log.info(f"  {s:20} {counts[s]:4}")
    log.info(f"  {'sheets scraped':20} {sum(r['sheets'] for r in rows if r['status'] == 'live'):4}")


if __name__ == "__main__":
    main()
