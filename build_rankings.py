"""
Write web/rankings.json from course_rankings.csv, for the "Top 100" / "Top 200"
stamps on the search page.

course_rankings.csv is Golf Monthly's UK & Ireland Top 100 (ranked) and Next
100 (unranked, shown as "Top 200"), 2025/26 edition. config_club_name is the
hand-checked match to a clubs_config.csv row, blank when we don't list the
course. Matching is done by a person, not by name similarity: Formby Golf Club
is ranked and Formby Ladies Golf Club, which we list, is a different club.

Keys are "club|course" the way the search results name them: the club is the
config name without its bracketed part, and the course is the bracketed part,
or the club name again for a single-course club.

Re-run after editing course_rankings.csv or renaming a club:
    python build_rankings.py
"""
import csv
import json
import re
from pathlib import Path

config = {r["club_name"] for r in csv.DictReader(open("clubs_config.csv", encoding="utf-8"))}
out = {}
for r in csv.DictReader(open("course_rankings.csv", encoding="utf-8")):
    name = r["config_club_name"].strip()
    if not name:
        continue
    if name not in config:
        raise SystemExit(f"course_rankings.csv names {name!r}, which is not in clubs_config.csv")
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", name)
    club, course = (m.group(1), m.group(2)) if m else (name, name)
    out[f"{club}|{course}"] = {"tier": r["tier"], "rank": int(r["rank"]) if r["rank"] else None}
path = Path("web/rankings.json")
path.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
print(f"{len(out)} ranked courses -> {path} ({path.stat().st_size:,} bytes)")
