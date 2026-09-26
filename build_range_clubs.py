"""web/range_clubs.json — the clubs with a driving range or practice ground on
site, for the search filter. A few kB, so the filter can be applied without
pulling the whole 200kB trip file into every search."""
import csv, json, re
from pathlib import Path

config = {re.sub(r"\s*\(.*\)$", "", r["club_name"]).strip()
          for r in csv.DictReader(open("clubs_config.csv", encoding="utf-8"))
          if r["base_url"].strip()}

PRACTICE = ("driving range", "practice ground")
clubs = sorted({
    r["club"].strip() for r in csv.DictReader(open("trip_info.csv", encoding="utf-8"))
    if r["club"].strip() in config
    and any(item.strip().rsplit(":", 1)[0].strip() in PRACTICE
            for item in (r["on_site"] or "").split(";") if item.strip())
})
p = Path("web/range_clubs.json")
p.write_text(json.dumps(clubs, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"{len(clubs)} clubs with somewhere to warm up -> {p} ({p.stat().st_size:,} bytes)")
