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
PC_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[ABD-HJLNP-UW-Z]{2})\b")
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
    # The sheet's <title> names the club the way the PLATFORM knows it, which
    # is the only reliable way to spot the same club reached by two
    # hostnames. South Essex Golf Centre and The Heron Country Club are one
    # club on two domains (southessex.intelligentgolf.co.uk and
    # heroncountryclub.uk); the dedupe keys on base_url, so it saw two.
    # Putting the title in the evidence makes the mismatch obvious at review.
    sheet_title = (soup.title.get_text(strip=True) if soup.title else "")[:70]

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
            evidence=((f"{found[1]} slots on {found[0]}, href course={found[2]}"
                       + (f" | sheet says: {sheet_title}" if sheet_title else "")) if found
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


# --- BRS ---------------------------------------------------------------------

BRS_HOST = "https://visitors.brsgolf.com"
BRS_HEADERS = {**UA, "Accept": "application/json",
               "X-Requested-With": "XMLHttpRequest", "X-Booking-Source": "ui"}


def brs_rows(s, name, evidence_url):
    """Slug from the evidence URL -> courses from the API -> first date with a free slot."""
    path = urlparse(evidence_url).path.strip("/")
    slug = path.split("/")[0] if path else ""
    base = f"{BRS_HOST}/{slug}"
    s.cookies.clear()  # shared host: drop the previous club's context cookie
    if not slug or not get(s, base):
        return [row(club_name=name, platform="brs", base_url=base,
                    verified=False, evidence="club page fetch failed (no context cookie)")]
    # The API works out which club you mean from the context cookie AND the
    # Referer — without the Referer it 400s "Object reference not set".
    hdrs = {**BRS_HEADERS, "Referer": base}
    try:
        courses = s.get(f"{BRS_HOST}/api/courses/all", headers=hdrs, timeout=20).json()
        time.sleep(DELAY)
    except (requests.RequestException, ValueError):
        courses = []
    if not isinstance(courses, list) or not courses:
        return [row(club_name=name, platform="brs", base_url=base,
                    verified=False, evidence="/api/courses/all gave nothing")]
    rows = []
    for c in courses:
        cid = str(c.get("id"))
        found = ""
        for i in range(14):
            d = (date.today() + timedelta(days=i)).isoformat()
            try:
                r = s.get(f"{BRS_HOST}/api/casualBooking/teesheet?date={d}&course_id={cid}",
                          headers=hdrs, timeout=20)
                time.sleep(DELAY)
                tt = r.json()["data"]["tee_times"]
            except (requests.RequestException, ValueError, KeyError, TypeError):
                continue
            if any(sl.get("status") == "Available" for t in tt for sl in (t.get("slots") or {}).values()):
                found = d
                break
        label = name if len(courses) == 1 else f"{name} ({c.get('name')})"
        rows.append(row(club_name=label, platform="brs", base_url=base, course_id=cid,
                        verified=bool(found),
                        evidence=f"free slot on {found}" if found else "no free visitor slot in 14 days (booking may be off)"))
    return rows


# --- Shiji (hotel-resort booking sites) ------------------------------------

def shiji_rows(s, name, evidence_url):
    """Booking-site origin -> its course codes -> a free slot via the open API."""
    import shiji_scraper
    import golf_common as gc
    p = urlparse(evidence_url)
    base = f"{p.scheme}://{p.netloc}"
    r = get(s, f"{base}/golf")
    if not r:
        return [row(club_name=name, platform="shiji", base_url=base, verified=False, evidence="/golf fetch failed")]
    codes = re.findall(r'<option value="([A-Z0-9]+)"[^>]*>([^<]+)</option>', r.text)
    codes = [(v, l.strip()) for v, l in codes if v and l.strip().lower() != "please select"]
    if not codes:
        return [row(club_name=name, platform="shiji", base_url=base, verified=False, evidence="no course selector on /golf")]
    rows = []
    for code, label in codes:
        club = gc.ClubConfig(club_name=name, platform="shiji", base_url=base, course_id=code)
        found = None
        for i in range(SCAN_DAYS):
            d = (date.today() + timedelta(days=i)).isoformat()
            res, status = shiji_scraper.scrape_club(club, d, s)
            if status == "error":
                break
            if res:
                found = (d, len(res))
                break
        rows.append(row(club_name=f"{name} ({label})" if len(codes) > 1 else name, platform="shiji",
                        base_url=base, course_id=code, verified=bool(found),
                        evidence=f"{found[1]} slots on {found[0]}" if found else "no free slot in 14 days"))
    return rows


# --- Gladstone Go (council / leisure trust) ---------------------------------

def gladstone_rows(s, name, evidence_url):
    """Tenant origin -> site matched by name -> one row per golf activity group with a free slot."""
    import gladstone_scraper
    import golf_common as gc
    from discover_clubs import norm_club_name
    p = urlparse(evidence_url)
    base = f"{p.scheme}://{p.netloc}"
    hdr = {**UA, "Accept": "application/json", "X-Use-Sso": "1", "Referer": f"{base}/book"}
    try:
        s.get(f"{base}/api/samlauthentication/anonymous", headers=hdr, timeout=20)
        sites = s.get(f"{base}/api/configuration/sites?includeFacilitiesAndAmenities=true&isActive=true",
                      headers=hdr, timeout=20).json()
        groups = s.get(f"{base}/api/configuration/activity-groups", headers=hdr, timeout=20).json()
    except (requests.RequestException, ValueError) as e:
        return [row(club_name=name, platform="gladstone", base_url=base, verified=False, evidence=f"API failed: {e}")]
    key = norm_club_name(name)
    site = next((x for x in sites if norm_club_name(x.get("name", "")) == key), None) or \
           next((x for x in sites if key and key in norm_club_name(x.get("name", ""))), None)
    if not site:
        return [row(club_name=name, platform="gladstone", base_url=base, verified=False,
                    evidence="no site on this tenant matches the club name")]
    # "18 Holes" (Mytime/TM Active) or a generically-named tee-off group
    # (Impulse Leisure calls Belhus Park's "Golf Course Tee Off"). Everything
    # else on a leisure tenant — lessons, buggies, footgolf, hire — is noise.
    def is_tee_off(g):
        d = g.get("description", "")
        if re.search(r"lesson|coach|buggy|buggies|hire|footgolf|range|kidz|kids|junior", d, re.I):
            return False
        return bool(re.search(r"\b\d+\s*hole", d, re.I)
                    or re.search(r"tee[- ]?off|golf course", d, re.I))

    golf_groups = [g for g in groups if g["id"].startswith(site["id"]) and is_tee_off(g)]
    rows = []
    for g in golf_groups:
        hint = "" if re.search(r"\b\d+\s*hole", g.get("description", ""), re.I) else "18"
        ref = f"{site['id']}/{g['id']}" + (f"/{hint}" if hint else "")
        club = gc.ClubConfig(club_name=name, platform="gladstone", base_url=base, course_id=ref)
        found = None
        for i in range(SCAN_DAYS):
            d = (date.today() + timedelta(days=i)).isoformat()
            res, status = gladstone_scraper.scrape_club(club, d, s)
            if status == "error":
                break
            if res:
                found = (d, len(res))
                break
        label = re.sub(r"\(web\)", "", g["description"], flags=re.I).strip()
        rows.append(row(club_name=f"{name} ({label})", platform="gladstone", base_url=base,
                        course_id=ref, postcode=site.get("address", {}).get("postalCode", ""),
                        verified=bool(found),
                        evidence=f"{found[1]} slots on {found[0]}" if found else "no free slot in 14 days"))
    return rows


# --- Chronogolf (Lightspeed) -------------------------------------------------

CHRONO_HOST = "https://www.chronogolf.com"
# Player types that exist but are not "a visitor paying a green fee".
CHRONO_NOT_VISITOR_RE = re.compile(
    r"block|member|guest|ex-?member|junior|student|cardholder|county card|staff|twilight", re.I)


def chronogolf_rows(s, name, evidence_url):
    """club id from the widget URL -> each course -> the affiliation type that
    actually yields availability.

    The visitor player type has to be found per club, not assumed: at Bramshaw
    the type named "Visitors" gives 44 bookable slots and the one named
    "Public" gives none. So try the plausible ones and keep whichever produces
    real availability.
    """
    import chronogolf_scraper
    import golf_common as gc
    m = re.search(r"/club/(\d+)", evidence_url or "")
    base = f"{CHRONO_HOST}/club/{m.group(1)}" if m else ""
    if not base:
        return [row(club_name=name, platform="chronogolf", base_url=evidence_url or "",
                    verified=False, evidence="no /club/<id> in the evidence URL")]
    club_id = m.group(1)
    hdr = {**UA, "Accept": "application/json", "Referer": f"{base}/widget"}
    try:
        courses = s.get(f"{CHRONO_HOST}/marketplace/clubs/{club_id}/courses",
                        headers=hdr, timeout=25).json()
        time.sleep(DELAY)
        affs = s.get(f"{CHRONO_HOST}/marketplace/organizations/{club_id}/affiliation_types",
                     headers=hdr, timeout=25).json()
        time.sleep(DELAY)
    except (requests.RequestException, ValueError) as e:
        return [row(club_name=name, platform="chronogolf", base_url=base,
                    verified=False, evidence=f"marketplace API failed: {e}")]

    candidates = [a for a in affs
                  if a.get("default_role") == "public"
                  and a.get("bookable_on_marketplace")
                  and not CHRONO_NOT_VISITOR_RE.search(a.get("name", ""))]
    # Most clubs call it "Visitors" or "Public"; try those first.
    candidates.sort(key=lambda a: 0 if re.search(r"visitor|public|green ?fee", a.get("name", ""), re.I) else 1)
    if not candidates:
        return [row(club_name=name, platform="chronogolf", base_url=base, verified=False,
                    evidence="no public player type that looks like a visitor")]

    rows = []
    for c in courses:
        cid, holes = str(c.get("id")), int(c.get("holes") or 18)
        # A full scrape is four requests per day per player type; across
        # 14 days and several candidate types that is enough to earn a 429
        # from Chronogolf, which then looks exactly like "no availability".
        # Ask the cheap question instead: one single-player request per day.
        best = None
        for aff in candidates[:4]:
            for i in range(SCAN_DAYS):
                d = (date.today() + timedelta(days=i)).isoformat()
                url = chronogolf_scraper.teetimes_url(club_id, cid, aff["id"], holes, 1).format(date=d)
                try:
                    r = s.get(url, headers=hdr, timeout=25)
                    time.sleep(DELAY)
                except requests.RequestException:
                    continue
                if r.status_code == 429:
                    print(f"      rate-limited on {name} — pausing")
                    time.sleep(30)
                    continue
                try:
                    slots = r.json()
                except ValueError:
                    continue
                if not isinstance(slots, list):
                    break   # e.g. "Player type provided is not valid" — try the next type
                free = [x for x in slots if chronogolf_scraper.bookable(x)]
                if free:
                    best = (aff, d, len(free))
                    break
            if best:
                break
        label = c.get("name") or ""
        rows.append(row(
            club_name=f"{name} ({label})" if label and len(courses) > 1 else name,
            platform="chronogolf", base_url=base,
            course_id=f"{cid}/{best[0]['id']}" if best else f"{cid}/{candidates[0]['id']}",
            verified=bool(best),
            evidence=(f"{best[2]} slots on {best[1]} as '{best[0]['name']}' ({holes} holes)" if best
                      else f"no availability in {SCAN_DAYS} days for any visitor player type"),
        ))
    return rows


HANDLERS = {"intelligent_golf": ig_rows, "esp": esp_rows, "golf_manager": gm_rows, "clubv1": clubv1_rows,
            "brs": brs_rows, "shiji": shiji_rows, "gladstone": gladstone_rows,
            "chronogolf": chronogolf_rows}


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
