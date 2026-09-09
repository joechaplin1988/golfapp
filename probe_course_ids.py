"""
probe_course_ids.py — discovery step 3: turn APPROVED review rows into
proposed clubs_config.csv rows by finding each platform's ids.

Reads a review CSV (from `discover_clubs.py fingerprint`) and, for the named
clubs (or every confidence=high / access=public row with --all), does the
same id discovery the scrapers were built on:

  intelligent_golf : read the sheet's course selector (multi-course clubs get
                     one row per course), then scan the next 14 days for a
                     live slot and read course= off its real booking link.
                     course_id is often NOT 1 (241, 144, 855, 1732 seen) and
                     the page's own hidden input is frequently empty — the
                     href on a real slot is the only authoritative source.
  esp              : clubid= from the club's own e-s-p.com/elitelive link,
                     then confirm book_start.php reaches a chooser page.
  golf_manager     : the <slug>.golfmanager.com host; confirm init.api answers.
  clubv1           : the hub host; read courseId= off /Visitors/booking.

Plus a postcode scraped off the club's own homepage — EYEBALL these; the
first postcode on a page can be a sponsor's or the secretary's.

Writes approved_rows.csv with a `verified` flag meaning "a live slot / a
working endpoint was actually seen". Never touches clubs_config.csv itself:
appending is deliberately a separate, looked-at step, because course_id is
exactly where silent mistakes hide.

Usage:
    python probe_course_ids.py candidates_review.csv --names "Sundridge Park" "West Kent"
    python probe_course_ids.py candidates_review.csv --all
"""

import argparse
import csv
import re
import sys
import time
from datetime import date, timedelta
from urllib.parse import urlparse

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "ServiceSynkGolfAggregator/0.1 (contact: joe@servicesynk.com; research use)"}
PC_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b")
DELAY = 1.5
SCAN_DAYS = 14

FIELDS = ["club_name", "platform", "base_url", "course_id", "postcode",
          "latitude", "longitude", "verified", "evidence"]


def get(s, url):
    try:
        r = s.get(url, headers=UA, timeout=20, allow_redirects=True)
        time.sleep(DELAY)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


def postcode_from(s, url):
    r = get(s, url) if url else None
    if not r:
        return ""
    t = re.sub(r"<(script|style)[\s\S]*?</\1>", "", r.text, flags=re.I)
    m = PC_RE.search(t)
    return f"{m.group(1)} {m.group(2)}" if m else ""


def row(**kw):
    d = {f: "" for f in FIELDS}
    d.update(kw)
    return d


# --- Intelligent Golf --------------------------------------------------------

def ig_rows(s, name, sheet_url):
    base = sheet_url.split("?")[0].rstrip("/")
    r = get(s, base + "/")
    if not r:
        return [row(club_name=name, platform="intelligent_golf", base_url=base,
                    verified=False, evidence="sheet fetch failed")]
    soup = BeautifulSoup(r.text, "html.parser")

    options = []
    for lab in soup.select("label.btn"):
        i = lab.select_one("input[name=course]")
        if i and i.get("value"):
            options.append((i["value"], lab.get_text(strip=True)))
    for o in soup.select("select[name=course] option"):
        if o.get("value"):
            options.append((o["value"], o.get_text(strip=True)))
    seen = set()
    options = [(v, l) for v, l in options if not (v in seen or seen.add(v))]
    courses = options or [("", "")]

    rows = []
    for cid, label in courses:
        found = None
        for i in range(SCAN_DAYS):
            d = (date.today() + timedelta(days=i)).strftime("%d-%m-%Y")
            rr = get(s, f"{base}/?date={d}" + (f"&course={cid}" if cid else ""))
            if not rr:
                continue
            slots = BeautifulSoup(rr.text, "html.parser").select('div[class*="teetimes-slot"]')
            if slots:
                a = slots[0].find("a", href=True)
                m = re.search(r"[?&]course=(\d+)", a["href"]) if a else None
                found = (d, len(slots), m.group(1) if m else cid)
                break
        label_clean = re.sub(r"\s*course\s*$", "", label, flags=re.I).strip()
        rows.append(row(
            club_name=f"{name} ({label_clean})" if label_clean and len(courses) > 1 else name,
            platform="intelligent_golf", base_url=base,
            course_id=found[2] if found else (cid or ""),
            verified=bool(found),
            evidence=(f"{found[1]} slots on {found[0]}, href course={found[2]}" if found
                      else f"no availability in {SCAN_DAYS} days for course={cid or '(default)'} — id unconfirmed"),
        ))
    return rows


# --- ESP ---------------------------------------------------------------------

ESP_BASE = "https://www.e-s-p.com/elitelive"


def esp_rows(s, name, site):
    r = get(s, site) if site else None
    ids = sorted(set(re.findall(r"e-s-p\.com/elitelive/[^\"'\s]*?clubid=(\d+)", r.text))) if r else []
    if not ids:
        return [row(club_name=name, platform="esp", base_url=ESP_BASE,
                    verified=False, evidence="no clubid= link found on the club's homepage")]
    rows = []
    for cid in ids:
        rr = get(s, f"{ESP_BASE}/book_start.php?clubid={cid}&")
        ok = bool(rr) and any(k in (rr.url + rr.text[:3000])
                              for k in ("book_group.php", "book_date.php", "book_widedaterange.php"))
        rows.append(row(club_name=name, platform="esp", base_url=ESP_BASE, course_id=cid,
                        verified=ok, evidence=f"book_start.php?clubid={cid} -> {rr.url if rr else 'fetch failed'}"))
    return rows


# --- Golf Manager ------------------------------------------------------------

def gm_rows(s, name, evidence_url):
    p = urlparse(evidence_url)
    base = f"{p.scheme}://{p.netloc}"
    rr = get(s, f"{base}/ebookings/init.api?start={date.today().isoformat()}T00:00:00")
    ok = bool(rr) and '"availability"' in rr.text
    return [row(club_name=name, platform="golf_manager", base_url=base,
                verified=ok, evidence=f"init.api {'answered' if ok else 'did not answer'}")]


# --- ClubV1 ------------------------------------------------------------------

def clubv1_rows(s, name, evidence_url):
    p = urlparse(evidence_url)
    base = f"{p.scheme}://{p.netloc}"
    rr = get(s, f"{base}/Visitors/booking")
    if not rr:
        return [row(club_name=name, platform="clubv1", base_url=base,
                    verified=False, evidence="/Visitors/booking fetch failed")]
    # A hub can exist with visitor booking switched OFF — it answers 200 with
    # "Permission Denied". Hard no: nothing to scrape, do not add.
    if re.search(r"permission denied|do not have permission", rr.text, re.I):
        return [row(club_name=name, platform="clubv1", base_url=base, verified=False,
                    evidence="hub exists but /Visitors/booking says Permission Denied — visitor booking not enabled; do not add")]
    ids = sorted(set(re.findall(r"courseId=(\d+)", rr.text)))
    if not ids:
        return [row(club_name=name, platform="clubv1", base_url=base, verified=False,
                    evidence="tee sheet reached but no courseId in its links")]
    rows = []
    for cid in ids:
        seen = None
        # Every other day, with extra pacing: ClubV1 shows a "rapid refresh
        # detected" warning at ~1s between hits on the same hub.
        for i in range(0, SCAN_DAYS, 2):
            d = (date.today() + timedelta(days=i)).isoformat()
            time.sleep(2)
            r2 = get(s, f"{base}/Visitors/TeeSheet?date={d}&courseId={cid}")
            if not r2:
                continue
            n = len(BeautifulSoup(r2.text, "html.parser").select("div.tee.available"))
            if n:
                seen = (d, n)
                break
        rows.append(row(
            club_name=name, platform="clubv1", base_url=base, course_id=cid,
            # The id is read off the public sheet's own links — authoritative —
            # so the row is safe to add even if no slot is open this fortnight.
            verified=True,
            evidence=(f"public sheet, courseId {cid}; {seen[1]} slots on {seen[0]}" if seen
                      else f"public sheet, courseId {cid}; no availability in {SCAN_DAYS} days (scraper will report empty)"),
        ))
    return rows


HANDLERS = {"intelligent_golf": ig_rows, "esp": esp_rows, "golf_manager": gm_rows, "clubv1": clubv1_rows}


def main():
    ap = argparse.ArgumentParser(description="Find platform ids for approved review rows")
    ap.add_argument("review_csv")
    ap.add_argument("--names", nargs="*", default=[], help="club names (case-insensitive substring)")
    ap.add_argument("--all", action="store_true", help="every confidence=high access=public row")
    ap.add_argument("--out", default="approved_rows.csv")
    a = ap.parse_args()

    with open(a.review_csv, newline="", encoding="utf-8") as f:
        review = list(csv.DictReader(f))
    if a.all:
        chosen = [r for r in review if r["confidence"] == "high" and r["access"] == "public"]
    else:
        chosen = [r for r in review if any(n.lower() in r["name"].lower() for n in a.names)]
    if not chosen:
        sys.exit("no matching rows")

    s = requests.Session()
    out = []
    for r in chosen:
        h = HANDLERS.get(r["platform"])
        if not h:
            print(f"  {r['name']:32} {r['platform']:16} SKIP — no id handler for this platform (no scraper yet)")
            continue
        arg = r["official_site"] if r["platform"] == "esp" else r["evidence_url"]
        rows = h(s, r["name"], arg)
        pc = postcode_from(s, r["official_site"])
        for x in rows:
            x["postcode"] = pc
            out.append(x)
            flag = "OK " if x["verified"] else "?? "
            print(f"  {flag} {x['club_name']:34} {x['platform']:16} course_id={x['course_id'] or '-':6} pc={pc or '-':9} {x['evidence'][:60]}")

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    unverified = sum(1 for x in out if not x["verified"])
    print(f"\nwrote {len(out)} proposed row(s) -> {a.out}  ({unverified} unverified — do not add those blind)")


if __name__ == "__main__":
    main()
