"""
apply_approved.py — discovery step 4: append VERIFIED rows from
approved_rows.csv (probe_course_ids.py output) to clubs_config.csv.

Only rows with verified=True are taken — an unverified row means no live
slot was ever seen, so its course_id is a guess, and a guessed course_id
fails silently (empty sheet forever, or someone else's course). Duplicates
against the existing config are skipped on (platform, base_url, course_id).

A row with a blank postcode is still appended (scraping doesn't need one)
but flagged loudly, because radius search does: fix it, then run
geocode_clubs.py. Postcodes.io is the real validator — a syntactically
plausible junk postcode (the regex once produced "KS3S 7BZ") only gets
caught there.

After this:
    python geocode_clubs.py clubs_config.csv
    git commit + push
The scheduled run_pipeline.py syncs the new rows into the DB on its next
run, so nothing needs a local DATABASE_URL.

Usage:
    python apply_approved.py --dry-run          # show what would be appended
    python apply_approved.py                    # append
"""

import argparse
import csv
import sys

CONFIG_FIELDS = ["club_name", "platform", "base_url", "course_id", "postcode", "latitude", "longitude"]


def key(r):
    return (r["platform"], r["base_url"].rstrip("/"), r["course_id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--approved", default="approved_rows.csv")
    ap.add_argument("--config", default="clubs_config.csv")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    with open(a.approved, newline="", encoding="utf-8") as f:
        approved = list(csv.DictReader(f))
    with open(a.config, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or CONFIG_FIELDS
        existing = list(reader)
    have = {key(r) for r in existing if r.get("base_url")}

    to_add, skipped = [], []
    for r in approved:
        if str(r.get("verified", "")).lower() != "true":
            skipped.append((r["club_name"], "unverified — no live slot seen"))
            continue
        if key(r) in have:
            skipped.append((r["club_name"], "already in config"))
            continue
        if "memberbooking" in r["base_url"].lower():
            # Defence in depth: a members' booking URL is never a visitor
            # sheet, whatever upstream said (REGC nearly re-entered this way).
            skipped.append((r["club_name"], "members' booking URL — never a visitor sheet"))
            continue
        to_add.append({f: r.get(f, "") for f in fields})
        have.add(key(r))

    for r in to_add:
        flag = "   <-- NO POSTCODE: fix before geocoding" if not r["postcode"] else ""
        print(f"  + {r['club_name']:36} {r['platform']:16} course_id={r['course_id'] or '-':6} pc={r['postcode'] or '-'}{flag}")
    for name, why in skipped:
        print(f"  - {name:36} skipped: {why}")

    if a.dry_run:
        print(f"\n(dry run) would append {len(to_add)} row(s) to {a.config}")
        return
    if not to_add:
        print("\nnothing to append")
        return

    # Append after the last row that has a base_url, keeping the deliberately
    # blank out-of-scope rows (Littlestone, Royal St Georges) at the bottom.
    filled = [r for r in existing if r.get("base_url")]
    blank = [r for r in existing if not r.get("base_url")]
    with open(a.config, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(filled + to_add + blank)
    print(f"\nappended {len(to_add)} row(s) to {a.config} — now run: python geocode_clubs.py {a.config}")
    if any(not r["postcode"] for r in to_add):
        print("some rows have no postcode: they'll scrape fine but won't appear in radius search until fixed + geocoded")
        sys.exit(2)


if __name__ == "__main__":
    main()
