"""
Write web/trip_info.json from trip_info.csv, for the trip-planning lines on a
club's card: what is on site, the nearest pubs, and the nearest places to stay.

trip_info.csv comes from trip_info.py (OpenStreetMap via Overpass) and is the
hand-checkable source; this only reshapes it for the browser. Clubs with
nothing worth showing are left out, which keeps the file small and means the
page shows nothing rather than an empty heading.

The page fetches this only when someone expands a club, so an ordinary search
never downloads it.

Re-run after trip_info.py, or after renaming a club:
    python build_trip_info.py
"""
import csv
import json
import re
from pathlib import Path

config = {re.sub(r"\s*\(.*\)$", "", r["club_name"]).strip()
          for r in csv.DictReader(open("clubs_config.csv", encoding="utf-8"))
          if r["base_url"].strip()}

out, skipped, untimed = {}, 0, 0
for r in csv.DictReader(open("trip_info.csv", encoding="utf-8")):
    club = r["club"].strip()
    if club not in config:
        skipped += 1
        continue
    entry = {}
    if r["on_site"]:
        # trip_info.csv keeps how far each facility is ("bar:19m") so the
        # what-counts-as-on-site radii can be re-tuned without collecting all
        # 1,000 clubs again. The page shows the facility, not the metres.
        #
        # An entry with no distance was collected before those radii existed,
        # when everything within 700m counted — which gave Cooden Beach a
        # "hotel on site" that is a hotel across the road. Saying a club has
        # something it doesn't is worse than saying nothing, so untimed
        # entries are left out until that club is collected again.
        on_site = [x.strip() for x in r["on_site"].split(";") if x.strip()]
        timed = [x.rsplit(":", 1)[0].strip() for x in on_site if re.search(r":\d+m$", x)]
        untimed += len(on_site) - len(timed)
        if timed:
            entry["on_site"] = timed
    for key in ("pubs", "stays"):
        if r[key]:
            entry[key] = [x.strip() for x in r[key].split(";") if x.strip()][:3]
    if entry:
        out[club] = entry

path = Path("web/trip_info.json")
path.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
print(f"{len(out)} clubs with trip info -> {path} ({path.stat().st_size:,} bytes)"
      + (f"; {skipped} row(s) not in the club list" if skipped else "")
      + (f"; {untimed} on-site entr(y/ies) left out, collected before per-facility "
         f"radii — re-run trip_info.py for those clubs" if untimed else ""))
