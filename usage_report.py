"""
Read back what the usage tables recorded.

Three questions, one per table (see db/migrations/005):
  - which clubs are we sending traffic to?      click_events
  - what did people search for, and did we have anything?  search_events
  - how hard are we hitting the platforms, and is it healthy?  scrape_runs

Needs DATABASE_URL, so it runs where the scraper runs (the workflow, or a
shell with the connection string set). The anon key deliberately cannot read
these tables — it may only INSERT — so this is not something the website can
do.

Usage:
    python usage_report.py            # last 30 days
    python usage_report.py --days 7
"""

import argparse

import golf_db


def table(cur, title: str, sql: str, params: tuple, headers: tuple) -> None:
    cur.execute(sql, params)
    rows = cur.fetchall()
    print(f"\n{title}")
    if not rows:
        print("   (nothing recorded yet)")
        return
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    print("   " + "  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    for r in rows:
        print("   " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Report on clicks, searches and scrape runs")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()

    with golf_db.get_connection() as conn:
        with conn.cursor() as cur:
            table(cur, f"Traffic sent to clubs (last {a.days} days)",
                  """
                  select club_name, count(*) as clicks,
                         count(distinct tee_date) as dates,
                         round(avg(price)::numeric, 0) as avg_price
                  from click_events
                  where created_at > now() - (%s || ' days')::interval
                  group by club_name order by clicks desc limit %s
                  """, (a.days, a.limit),
                  ("club", "clicks", "dates", "avg £"))

            table(cur, f"Searches that found NOTHING (last {a.days} days)",
                  """
                  select coalesce(area, '(unknown)') as area, count(*) as searches
                  from search_events
                  where created_at > now() - (%s || ' days')::interval
                    and coalesce(result_count, 0) = 0
                  group by area order by searches desc limit %s
                  """, (a.days, a.limit),
                  ("area searched", "searches"))

            table(cur, f"Busiest searched areas (last {a.days} days)",
                  """
                  select coalesce(area, '(unknown)') as area, count(*) as searches,
                         round(avg(result_count)) as avg_results
                  from search_events
                  where created_at > now() - (%s || ' days')::interval
                  group by area order by searches desc limit %s
                  """, (a.days, a.limit),
                  ("area searched", "searches", "avg results"))

            table(cur, "Recent scrape runs",
                  """
                  select to_char(finished_at, 'YYYY-MM-DD HH24:MI') as finished,
                         days, courses, ok, empty, error, rows_written
                  from scrape_runs order by finished_at desc limit %s
                  """, (a.limit,),
                  ("finished (UTC)", "days", "sheets", "ok", "empty", "error", "rows"))


if __name__ == "__main__":
    main()
