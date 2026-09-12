"""
One-off: turn the three wave-2 probe outputs into a single clean approved
file. Three jobs the probe can't do for itself:

  - drop sheets that aren't a round of golf. The probe reads a club's course
    selector verbatim, so it happily proposes FootGolf, disc golf, mini golf,
    adventure golf and pitch-and-putt sheets. Par 3 courses stay: they are a
    real round and the config already carries two (North Foreland, Chesfield
    Downs).
  - drop a "(10th Tee)" sheet when the same club also sells a "(1st Tee)"
    one. Both are real, but they are the same course listed twice, and only
    clubs flagged dedupe_courses get collapsed in search.
  - check each scraped postcode against where OSM says the club actually is.
    A postcode lifted off a club's homepage can belong to a sponsor or the
    secretary: Cirencester came back "LU2 0FY", which is Luton. Anything more
    than 15 km from the club is cleared, and any blank is filled by reverse
    geocoding OSM's coordinates.
"""
import csv, json, math, re, sys
from pathlib import Path
try:
    import truststore; truststore.inject_into_ssl()
except ImportError:
    pass
import requests

UA = {"User-Agent": "ServiceSynkGolfAggregator/0.1 (contact: joe@servicesynk.com)"}
INPUTS = sys.argv[1:-1] or [f"approved_rows_wave2{w}.csv" for w in "abc"]
OUT = sys.argv[-1] if len(sys.argv) > 1 else "approved_rows_wave2.csv"
NOT_A_ROUND = re.compile(
    r"footgolf|foot golf|disc golf|mini golf|adventure|crazy golf|"
    r"pitch\s*(and|&)\s*putt|putting green|coaching diary|driving range", re.I)
MAX_KM = 15.0


def norm(s):
    s = re.sub(r"\(.*?\)", " ", s.lower())
    s = re.sub(r"\b(golf|club|course|centre|center|links|and|the|hotel|resort)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def haversine(a, b, c, d):
    r = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# --- load ---------------------------------------------------------------
rows, bykey = [], {}
for src in INPUTS:
    for r in csv.DictReader(open(src, encoding="utf-8")):
        if r["verified"].lower() != "true":
            continue
        k = (r["platform"], r["base_url"].rstrip("/"), r["course_id"])
        # The same club reaches us under two names when two counties' sources
        # both list it — St Helens' Grange Park came through Merseyside's OSM
        # as "Grange Park Golf Club" and Lancashire's union as "Grange Park
        # Golf Club (Lancashire) Golf Club". Same sheet, so keep one, and keep
        # the tidier name rather than whichever county was read first.
        if k in bykey:
            if len(r["club_name"]) < len(bykey[k]["club_name"]):
                bykey[k]["club_name"] = r["club_name"]
            continue
        bykey[k] = r
        rows.append(r)
print(f"{len(rows)} unique verified rows")

dropped = []
keep = [r for r in rows if not NOT_A_ROUND.search(r["club_name"])]
dropped += [(r["club_name"], "not a round of golf") for r in rows if NOT_A_ROUND.search(r["club_name"])]

first_tee = {norm(r["club_name"]) for r in keep if "1st tee" in r["club_name"].lower()}
keep2 = []
for r in keep:
    if "10th tee" in r["club_name"].lower() and norm(r["club_name"]) in first_tee:
        dropped.append((r["club_name"], "same course as its 1st-tee sheet"))
    else:
        keep2.append(r)
keep = keep2

# --- club coordinates from the enumeration ------------------------------
# Only this wave's counties. Matching on name across every county file we
# have ever written put a Lincolnshire club's coordinates on a St Helens club
# of the same name — the kind of mistake that geocodes cleanly and puts a club
# 180 km from where it is.
import os
COORDS_GLOB = os.environ.get("COORDS_GLOB", "candidates_*.json")
coords = {}
for p in Path(".").glob(COORDS_GLOB):
    try:
        for c in json.loads(p.read_text(encoding="utf-8")):
            if c.get("lat") and c.get("lon"):
                coords.setdefault(norm(c["name"]), (float(c["lat"]), float(c["lon"])))
    except (json.JSONDecodeError, ValueError, KeyError):
        continue

# --- postcode sanity ----------------------------------------------------
pcs = sorted({r["postcode"] for r in keep if r["postcode"]})
loc = {}
for i in range(0, len(pcs), 100):
    resp = requests.post("https://api.postcodes.io/postcodes", json={"postcodes": pcs[i:i + 100]},
                         headers=UA, timeout=30).json()
    for item in resp.get("result", []):
        if item.get("result"):
            loc[item["query"].upper()] = (item["result"]["latitude"], item["result"]["longitude"],
                                          item["result"]["admin_district"])

bad, filled = [], []
for r in keep:
    here = coords.get(norm(r["club_name"]))
    pc = (r["postcode"] or "").upper()
    if pc and pc in loc and here:
        d = haversine(here[0], here[1], loc[pc][0], loc[pc][1])
        if d > MAX_KM:
            bad.append((r["club_name"], pc, round(d), loc[pc][2]))
            r["postcode"] = ""
    elif pc and pc not in loc:
        bad.append((r["club_name"], pc, -1, "not a real postcode"))
        r["postcode"] = ""
    if not r["postcode"] and here:
        # radius matters: the default 100 m usually finds nothing, because
        # OSM's centroid for a golf course sits in the middle of a fairway.
        rev = requests.get("https://api.postcodes.io/postcodes",
                           params={"lat": here[0], "lon": here[1], "limit": 1, "radius": 2000},
                           headers=UA, timeout=20).json().get("result")
        if rev:
            r["postcode"] = rev[0]["postcode"]
            filled.append((r["club_name"], rev[0]["postcode"], rev[0]["admin_district"]))
    if here:
        r["latitude"], r["longitude"] = f"{here[0]:.6f}", f"{here[1]:.6f}"

print(f"\ndropped {len(dropped)}:")
for n, why in dropped:
    print(f"  - {n[:52]:54s} {why}")
print(f"\nwrong postcode cleared {len(bad)}:")
for n, pc, d, dist in bad:
    print(f"  ! {n[:44]:46s} {pc:9s} {'(' + dist + ')' if d < 0 else str(d) + ' km away in ' + dist}")
print(f"\nreverse-geocoded {len(filled)} blank postcode(s):")
for n, pc, dist in filled:
    print(f"  + {n[:44]:46s} {pc:9s} {dist}")

still = [r["club_name"] for r in keep if not r["postcode"]]
print(f"\n{len(keep)} rows kept; {len(still)} still without a postcode: {still}")

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(keep[0].keys()))
    w.writeheader()
    w.writerows(keep)
print(f"wrote {OUT}")
