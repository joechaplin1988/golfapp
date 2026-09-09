"""
Geocode clubs_config.csv — turn each club's postcode into latitude/longitude.

Uses Postcodes.io (free, no API key, UK-only). Golf clubs don't move, so
this is a one-off backfill plus one lookup per newly added club: rows that
already have latitude/longitude are left alone, so it's safe to re-run.

Usage:
    python geocode_clubs.py clubs_config.csv            # fill in blanks
    python geocode_clubs.py clubs_config.csv --force    # redo everything
    python geocode_clubs.py clubs_config.csv --check    # report only, no write

Each geocoded row is printed with the district Postcodes.io reports for it —
eyeball that column. A postcode scraped off a club's website can be the
wrong one (a sponsor's, the secretary's home) and still geocode perfectly
well, just to the wrong place. The district is the cheap sanity check.
"""

import argparse
import csv
import logging
import sys
from pathlib import Path

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("geocode_clubs")

POSTCODES_IO_BULK = "https://api.postcodes.io/postcodes"
BULK_LIMIT = 100  # Postcodes.io caps a bulk lookup at 100 postcodes
USER_AGENT = "ServiceSynkGolfAggregator/0.1 (contact: joe@servicesynk.com)"
TIMEOUT = 20


def normalise(postcode: str) -> str:
    """'tn15  0je' -> 'TN15 0JE'. Postcodes.io is forgiving, but the config isn't."""
    compact = postcode.replace(" ", "").upper()
    return f"{compact[:-3]} {compact[-3:]}" if len(compact) > 3 else compact


def lookup(postcodes: list[str]) -> dict[str, dict | None]:
    """Bulk-geocode. Returns {postcode: result-or-None}. None = Postcodes.io doesn't know it."""
    out: dict[str, dict | None] = {}
    for i in range(0, len(postcodes), BULK_LIMIT):
        chunk = postcodes[i : i + BULK_LIMIT]
        resp = requests.post(
            POSTCODES_IO_BULK,
            json={"postcodes": chunk},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        for item in resp.json()["result"]:
            out[normalise(item["query"])] = item["result"]
    return out


def run(config_path: str, force: bool, check_only: bool) -> int:
    path = Path(config_path)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    todo = [
        r for r in rows
        if r.get("postcode", "").strip()
        and (force or not (r.get("latitude") and r.get("longitude")))
    ]
    skipped_no_postcode = [r["club_name"] for r in rows if not r.get("postcode", "").strip()]

    if skipped_no_postcode:
        log.warning(f"No postcode, skipping: {', '.join(skipped_no_postcode)}")
    if not todo:
        log.info("Nothing to geocode — every row with a postcode already has coordinates")
        return 0

    unique = sorted({normalise(r["postcode"]) for r in todo})
    log.info(f"Geocoding {len(unique)} distinct postcode(s) across {len(todo)} row(s)")
    results = lookup(unique)

    failures = 0
    print(f"\n{'club':30} {'postcode':9} {'lat':>9} {'long':>9}  district / parish")
    for r in todo:
        pc = normalise(r["postcode"])
        res = results.get(pc)
        if not res:
            failures += 1
            print(f"{r['club_name']:30} {pc:9} {'--':>9} {'--':>9}  NOT FOUND by Postcodes.io")
            continue
        r["postcode"] = pc
        r["latitude"] = f"{res['latitude']:.6f}"
        r["longitude"] = f"{res['longitude']:.6f}"
        where = res.get("admin_district") or ""
        parish = res.get("parish") or ""
        print(f"{r['club_name']:30} {pc:9} {res['latitude']:9.5f} {res['longitude']:9.5f}  {where} / {parish}")
    print()

    if check_only:
        log.info("--check: not writing")
    else:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        log.info(f"Wrote coordinates for {len(todo) - failures} row(s) to {path}")

    if failures:
        log.error(f"{failures} postcode(s) not found — fix them in the config and re-run")
    return 1 if failures else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Geocode club postcodes via Postcodes.io")
    p.add_argument("config", help="Path to clubs config CSV")
    p.add_argument("--force", action="store_true", help="Re-geocode rows that already have coordinates")
    p.add_argument("--check", action="store_true", help="Look up and print, but don't write the CSV")
    a = p.parse_args()
    sys.exit(run(a.config, a.force, a.check))
