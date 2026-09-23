"""
Build trip_info.csv: what is at each club, and what is near it.

Testers asked for enough to plan an overnight golf trip: the facilities on
site (driving range, pro shop, bar, somewhere to stay) and the nearest pubs
and hotels. All of it comes from OpenStreetMap via Overpass, which is free
and needs no key, in exchange for a credit line on the site.

Honesty rules, because a wrong claim here is worse than no claim:
  - only NAMED places are listed, so "a pub" never appears without a pub;
  - distances are straight-line from the club's stored position, and are
    labelled as such on the site;
  - nothing is inferred. A club with no bar tagged in OSM shows no bar,
    rather than us guessing from its size or its website.

Clubs are queried in batches, one Overpass request per batch, with a pause
between: this is a shared free service and 1,000 clubs is a lot to ask of it.

Usage:
    python trip_info.py --limit 12            # a sample, printed
    python trip_info.py                       # every club -> trip_info.csv
    python trip_info.py --out trip_info.csv --batch 25
"""

import argparse
import csv
import json
import math
import re
import time

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

UA = {"User-Agent": "ServiceSynkGolfAggregator/0.1 (contact: joe@servicesynk.com; research use)"}
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]

ON_SITE_M = 700          # how far we ASK; each facility is judged tighter, below
PUB_M = 5000
STAY_M = 6000
MAX_NEARBY = 3

# What we look for, what we call it, and how close it has to be to count as the
# club's own. One radius for everything was wrong in both directions: at 700m
# Cooden Beach gained a "hotel on site" that is a separate hotel across the
# road, and Knole Park gained two cafes belonging to the National Trust park
# around it. Golf-specific things that far out are almost certainly the club's;
# a bar or a cafe that far out is somebody else's business.
#
# There is no "hotel on site" any more. A hotel is only ever honest with a
# distance beside it, and it already appears under places to stay.
ON_SITE = [
    ('nwr["golf"="driving_range"]', "driving range", 500),
    ('nwr["leisure"="golf_driving_range"]', "driving range", 500),
    ('nwr["golf"="practice"]', "practice ground", 500),
    ('nwr["shop"="golf"]', "pro shop", 400),
    ('nwr["amenity"="restaurant"]', "restaurant", 200),
    ('nwr["amenity"="bar"]', "bar", 200),
    ('nwr["amenity"="cafe"]', "cafe", 200),
    ('nwr["leisure"="spa"]', "spa", 200),
]
NEARBY = [
    ('nwr["amenity"="pub"]', "pub", PUB_M),
    ('nwr["tourism"~"^(hotel|guest_house|inn|motel)$"]', "stay", STAY_M),
]


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def ask(query):
    """Overpass is a free shared service and answers 429/504 when busy. Back off
    and keep trying both mirrors rather than losing an hour of collected work."""
    last = None
    for attempt in range(6):
        for url in OVERPASS:
            try:
                r = requests.post(url, data={"data": query}, headers=UA, timeout=240)
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    return r.json()
                last = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last = type(e).__name__
        wait = 30 * (attempt + 1)
        print(f"    Overpass busy ({last}); waiting {wait}s", flush=True)
        time.sleep(wait)
    raise RuntimeError(f"Overpass would not answer: {last}")


# Keys that could describe something on a golf club's own site. Asking for
# "anything carrying one of these keys within 700m" is ONE statement per club
# instead of one per facility, and the tags are filtered locally afterwards
# anyway — the answer is identical, at a fraction of the cost to a free
# service that was answering 504 under the per-facility version.
ON_SITE_KEYS = "^(golf|leisure|shop|amenity|tourism)$"


def batch_query(clubs):
    """One request for a batch: three statements per club, unioned."""
    parts = []
    for c in clubs:
        lat, lon = c["lat"], c["lon"]
        parts.append(f'nwr(around:{ON_SITE_M},{lat},{lon})[~"{ON_SITE_KEYS}"~"."];')
        for sel, _kind, radius in NEARBY:
            parts.append(f'{sel}(around:{radius},{lat},{lon});')
    return f'[out:json][timeout:240];({"".join(parts)});out center tags;'


def collect(clubs):
    """Returns {club index: {"on_site": [...], "pubs": [...], "stays": [...]}}"""
    data = ask(batch_query(clubs))
    out = {i: {"on_site": [], "pubs": [], "stays": []} for i in range(len(clubs))}
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        for i, c in enumerate(clubs):
            d = haversine_m(c["lat"], c["lon"], lat, lon)
            for sel, label, radius in ON_SITE:
                if d <= radius and matches_tags(sel, tags):
                    out[i]["on_site"].append((label, d))
            if name:
                if d <= PUB_M and tags.get("amenity") == "pub":
                    out[i]["pubs"].append((name, d))
                if d <= STAY_M and tags.get("tourism") in ("hotel", "guest_house", "inn", "motel"):
                    out[i]["stays"].append((name, d))
    for i in out:
        # Nearest example of each facility wins, and its distance is kept so the
        # radii above can be re-tuned from the CSV without asking Overpass for
        # 1,000 clubs all over again.
        nearest: dict = {}
        for label, d in out[i]["on_site"]:
            if label not in nearest or d < nearest[label]:
                nearest[label] = d
        out[i]["on_site"] = sorted(nearest.items())
        out[i]["pubs"] = sorted(set(out[i]["pubs"]), key=lambda x: x[1])[:MAX_NEARBY]
        out[i]["stays"] = sorted(set(out[i]["stays"]), key=lambda x: x[1])[:MAX_NEARBY]
    return out


def matches_tags(selector, tags):
    m = re.findall(r'\["([^"]+)"\s*([=~])\s*"([^"]+)"\]', selector)
    for key, op, value in m:
        got = tags.get(key)
        if got is None:
            return False
        if op == "=" and got != value:
            return False
        if op == "~" and not re.match(value, got):
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description="Collect on-site facilities and nearby pubs/stays per club")
    ap.add_argument("--config", default="clubs_config.csv")
    ap.add_argument("--out", default="trip_info.csv")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--pause", type=float, default=4.0)
    a = ap.parse_args()

    # Written as we go and re-read on start, so a stall costs one batch, not
    # the whole run.
    done = set()
    try:
        with open(a.out, newline="", encoding="utf-8") as f:
            done = {r["club"] for r in csv.DictReader(f)}
    except FileNotFoundError:
        pass

    seen, clubs = set(), []
    for r in csv.DictReader(open(a.config, newline="", encoding="utf-8")):
        name = re.sub(r"\s*\(.*\)$", "", r["club_name"]).strip()
        if not r.get("latitude") or not r["base_url"].strip() or name in seen:
            continue
        seen.add(name)
        clubs.append({"club": name, "lat": float(r["latitude"]), "lon": float(r["longitude"])})
    if a.limit:
        clubs = clubs[: a.limit]
    if done:
        before = len(clubs)
        clubs = [c for c in clubs if c["club"] not in done]
        print(f"resuming: {before - len(clubs)} club(s) already collected")
    print(f"{len(clubs)} clubs to do, {a.batch} per Overpass request")

    fresh = not done
    out = open(a.out, "w" if fresh else "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out, fieldnames=["club", "on_site", "pubs", "stays"])
    if fresh:
        writer.writeheader()

    rows = []
    for start in range(0, len(clubs), a.batch):
        chunk = clubs[start : start + a.batch]
        try:
            got = collect(chunk)
        except RuntimeError as e:
            print(f"    skipping this batch: {e}", flush=True)
            continue
        for i, c in enumerate(chunk):
            g = got[i]
            rows.append({
                "club": c["club"],
                "on_site": "; ".join(f"{label}:{d:.0f}m" for label, d in g["on_site"]),
                "pubs": "; ".join(f"{n} ({d / 1609.34:.1f} mi)" for n, d in g["pubs"]),
                "stays": "; ".join(f"{n} ({d / 1609.34:.1f} mi)" for n, d in g["stays"]),
            })
        writer.writerows(rows[-len(chunk):])
        out.flush()
        at = min(start + a.batch, len(clubs))
        with_any = sum(1 for r in rows if r["on_site"] or r["pubs"] or r["stays"])
        print(f"  {at}/{len(clubs)} clubs; {with_any} with something to show", flush=True)
        time.sleep(a.pause)

    out.close()
    print(f"wrote {len(rows)} row(s) -> {a.out}")
    if a.limit:
        for r in rows:
            print(f"\n  {r['club']}\n    on site: {r['on_site'] or '-'}\n    pubs: {r['pubs'] or '-'}\n    stays: {r['stays'] or '-'}")


if __name__ == "__main__":
    main()
