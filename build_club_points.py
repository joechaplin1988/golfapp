"""
Write web/club_points.json — every scrapable club's coordinates, rounded.

The search page uses it for one job: when a town name matches several places,
decide which one the person meant by counting how many of our courses are
near each. That is the only signal that separates two places we both cover —
there is a Swanley in Kent and one in Gloucestershire, a Cambridge in
Cambridgeshire, Norfolk and Gloucestershire.

Three decimal places is about 100 m, far finer than a 30 km count needs, and
keeps the file a few kilobytes. Clubs with no coordinates (and the rows that
have no booking platform) are left out.

Re-run this after every batch of new clubs:
    python build_club_points.py
"""
import csv
import json
from pathlib import Path

rows = list(csv.DictReader(open("clubs_config.csv", encoding="utf-8")))
points = sorted({
    (round(float(r["latitude"]), 3), round(float(r["longitude"]), 3))
    for r in rows
    if r.get("latitude") and r.get("longitude") and (r.get("base_url") or "").strip()
})
out = Path("web/club_points.json")
out.write_text(json.dumps([list(p) for p in points], separators=(",", ":")), encoding="utf-8")
print(f"{len(rows)} config rows -> {len(points)} distinct points, {out.stat().st_size:,} bytes -> {out}")
