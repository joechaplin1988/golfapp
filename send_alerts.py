"""
Email tee-time alerts: for every confirmed alert, find tee times in the next
seven days that match it and haven't been sent before, and email them.

Runs in GitHub Actions after each scrape (.github/workflows/alerts.yml), where
DATABASE_URL lives. Alerts are created and confirmed by the Netlify functions
in netlify/functions; see db/migrations/006_alerts.sql for the tables and the
privacy rules they follow.

Matching reuses search_tee_times, the same function the website searches with,
so an alert finds exactly what the person would see if they searched again.

Rules:
  - a tee time is only ever emailed once per alert (alert_sent);
  - at most one email per alert every COOLDOWN_HOURS, carrying everything new;
  - only tee times at least an hour away, in UK time;
  - the first email after confirming lists what is available right now.

Sending needs RESEND_API_KEY and ALERTS_FROM. Without them, or with --dry-run,
it prints the emails it would send and changes nothing.

Usage:
    python send_alerts.py                  # send (or dry-run if not configured)
    python send_alerts.py --dry-run
    python send_alerts.py --test-alert '{"place":"Sevenoaks","lat":51.27,"lng":0.19,"radius_km":16,"players":2,"days":[6,7]}'
"""

import argparse
import html
import json
import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo
    UK = ZoneInfo("Europe/London")
except Exception:          # Windows without the tzdata package
    UK = None

import requests

import golf_common as gc
import golf_db

log = gc.log

COOLDOWN_HOURS = 3
LOOKAHEAD_DAYS = 7
MIN_LEAD = timedelta(hours=1)
MAX_LINES = 12          # club-and-date lines listed in one email
MAX_TIMES_PER_LINE = 6
SITE_URL = os.environ.get("SITE_URL", "https://golfbookingapp.netlify.app").rstrip("/")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def now_uk() -> datetime:
    return datetime.now(UK) if UK else datetime.now()


def describe(a: dict) -> str:
    days = a.get("days") or []
    day_text = "any day" if not days or len(days) == 7 else ", ".join(DAY_NAMES[d - 1] for d in sorted(days))
    miles = round(float(a["radius_km"]) * 0.621371)
    price = f", up to £{float(a['max_price']):.0f}" if a.get("max_price") is not None else ""
    holes = f", {a['holes']} holes" if a.get("holes") else ""
    return (f"{a['players']} player{'' if a['players'] == 1 else 's'} within {miles} miles of "
            f"{a.get('place') or 'your search'}, {day_text}, "
            f"{str(a['time_from'])[:5]}-{str(a['time_to'])[:5]}{holes}{price}")


def as_time(v) -> time:
    return v if isinstance(v, time) else time.fromisoformat(str(v)[:5])


def slot_key(row: dict) -> tuple:
    # A club that sells one physical course as several sheets returns a
    # different course name for the same slot from run to run, so it keys on
    # the club alone, as search does.
    course = "" if row.get("one_course") else (row.get("course_name") or "")
    return (row["club_id"], course, row["tee_date"], row["tee_time"])


def matches(cur, a: dict) -> list[dict]:
    today = now_uk().date()
    earliest = now_uk().replace(tzinfo=None) + MIN_LEAD
    t_from, t_to = as_time(a["time_from"]), as_time(a["time_to"])
    days = set(a.get("days") or [])
    found = []
    for k in range(LOOKAHEAD_DAYS):
        d = today + timedelta(days=k)
        if days and d.isoweekday() not in days:
            continue
        cur.execute("select * from search_tee_times(%s::float8, %s::float8, %s::float8, %s::date, "
                    "%s::int, %s::numeric, %s::int)",
                    (a["lat"], a["lng"], float(a["radius_km"]), d, a["players"],
                     a.get("max_price"), a.get("holes")))
        for row in cur.fetchall():
            tt = as_time(row["tee_time"])
            if not (t_from <= tt <= t_to):
                continue
            if datetime.combine(row["tee_date"], tt) < earliest:
                continue
            found.append(row)
    return found


def build_email(a: dict, rows: list[dict]) -> tuple[str, str, str]:
    lines = defaultdict(list)
    for r in rows:
        name = r["club_name"] + ("" if r.get("one_course") or r["course_name"] in (None, "", r["club_name"])
                                 else f" ({r['course_name']})")
        lines[(r["tee_date"], name)].append(r)
    ordered = sorted(lines.items(), key=lambda kv: (kv[0][0], min(x["distance_km"] for x in kv[1])))
    shown, hidden = ordered[:MAX_LINES], ordered[MAX_LINES:]

    unsubscribe = f"{SITE_URL}/.netlify/functions/alert-unsubscribe?token={a['token']}" if a.get("token") else SITE_URL
    count = len(rows)
    subject = f"{count} new tee time{'' if count == 1 else 's'} near {a.get('place') or 'you'}"

    text_parts, html_rows = [], []
    for (d, name), slots in shown:
        slots.sort(key=lambda x: as_time(x["tee_time"]))
        miles = min(x["distance_km"] for x in slots) * 0.621371
        prices = [float(x["price"]) for x in slots if x.get("price") is not None]
        price = f"from £{min(prices):.0f} for {a['players']}" if prices else "price on club site"
        times = ", ".join(str(x["tee_time"])[:5] for x in slots[:MAX_TIMES_PER_LINE])
        more = f" +{len(slots) - MAX_TIMES_PER_LINE} more" if len(slots) > MAX_TIMES_PER_LINE else ""
        when = f"{DAY_NAMES[d.isoweekday() - 1]} {d.day} {d.strftime('%b')}"
        link = slots[0].get("booking_url") or SITE_URL
        text_parts.append(f"{when}  {name}, {miles:.1f} mi\n  {times}{more}  ({price})\n  Book: {link}")
        html_rows.append(
            f"<tr><td style='padding:10px 0;border-bottom:1px solid #e2e6e0'>"
            f"<div style='color:#5c6b60;font-size:13px'>{html.escape(when)} · {miles:.1f} mi</div>"
            f"<div style='font-weight:600'>{html.escape(name)}</div>"
            f"<div>{html.escape(times + more)} <span style='color:#5c6b60'>({html.escape(price)})</span></div>"
            f"<div><a href='{html.escape(link)}' style='color:#1f6b3b'>Book on the club's site</a></div></td></tr>")
    extra = sum(len(s) for _, s in hidden)
    tail = f"\n...and {extra} more tee time{'' if extra == 1 else 's'} on {SITE_URL}" if hidden else ""

    summary = describe(a)
    text = (f"New tee times for your alert: {summary}\n\n" + "\n\n".join(text_parts) + tail +
            f"\n\nTimes change quickly, so check on the club's site before you set off.\n"
            f"Stop this alert: {unsubscribe}\n")
    body = (f"<div style='font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:560px;color:#1c2620'>"
            f"<p>New tee times for your alert:<br><strong>{html.escape(summary)}</strong></p>"
            f"<table style='width:100%;border-collapse:collapse'>{''.join(html_rows)}</table>"
            + (f"<p>...and {extra} more on <a href='{SITE_URL}' style='color:#1f6b3b'>Tee Times Near You</a>.</p>" if hidden else "")
            + f"<p style='color:#5c6b60;font-size:13px'>Times change quickly, so check on the club's site before you set off. "
            f"This alert ends by itself 90 days after you set it up. "
            f"<a href='{html.escape(unsubscribe)}' style='color:#5c6b60'>Stop this alert</a></p></div>")
    return subject, text, body


def send(to: str, subject: str, text: str, body: str, unsubscribe: str) -> None:
    res = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={"from": os.environ["ALERTS_FROM"], "to": [to], "subject": subject, "text": text, "html": body,
              "headers": {"List-Unsubscribe": f"<{unsubscribe}>",
                          "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}},
        timeout=30,
    )
    res.raise_for_status()


def main() -> None:
    ap = argparse.ArgumentParser(description="Email tee-time alerts")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-alert", help="JSON for a made-up alert; printed, never sent or stored")
    a = ap.parse_args()

    configured = bool(os.environ.get("RESEND_API_KEY") and os.environ.get("ALERTS_FROM"))
    dry = a.dry_run or a.test_alert or not configured
    if not configured and not a.dry_run and not a.test_alert:
        log.warning("RESEND_API_KEY / ALERTS_FROM not set: dry run, nothing will be sent")

    from psycopg.rows import dict_row
    with golf_db.get_connection() as conn:
        conn.row_factory = dict_row
        with conn.cursor() as cur:
            if a.test_alert:
                alerts = [{"id": None, "token": None, "email": "test@example.com", "time_from": "06:00",
                           "time_to": "20:00", "days": [], "max_price": None, "holes": None,
                           **json.loads(a.test_alert)}]
            else:
                if not dry:
                    cur.execute("delete from alerts where confirmed_at is null and created_at < now() - interval '48 hours'")
                    stale = cur.rowcount
                    cur.execute("delete from alerts where expires_at < now()")
                    if stale or cur.rowcount:
                        log.info(f"Removed {stale} unconfirmed and {cur.rowcount} expired alert(s)")
                    conn.commit()
                cur.execute(f"""
                    select * from alerts
                    where confirmed_at is not null and expires_at > now()
                      and (last_sent_at is null or last_sent_at < now() - interval '{COOLDOWN_HOURS} hours')
                """)
                alerts = cur.fetchall()
            log.info(f"{len(alerts)} alert(s) due a check{' (dry run)' if dry else ''}")

            sent = 0
            for alert in alerts:
                rows = matches(cur, alert)
                if alert["id"]:
                    cur.execute("select club_id, course_name, tee_date, tee_time from alert_sent where alert_id = %s",
                                (alert["id"],))
                    already = {(r["club_id"], r["course_name"], r["tee_date"], r["tee_time"]) for r in cur.fetchall()}
                    rows = [r for r in rows if slot_key(r) not in already]
                if not rows:
                    continue
                subject, text, body = build_email(alert, rows)
                unsubscribe = f"{SITE_URL}/.netlify/functions/alert-unsubscribe?token={alert['token']}"
                if dry:
                    print(f"\n--- would email {alert['email']} ---\nSubject: {subject}\n\n{text}")
                    continue
                try:
                    send(alert["email"], subject, text, body, unsubscribe)
                except requests.RequestException as e:
                    log.error(f"alert {alert['id']}: send failed, will retry next run: {e}")
                    continue
                # Recorded only after the email went, so a failed send is retried.
                cur.executemany(
                    "insert into alert_sent (alert_id, club_id, course_name, tee_date, tee_time) "
                    "values (%s, %s, %s, %s, %s) on conflict do nothing",
                    [(alert["id"], *slot_key(r)) for r in rows])
                cur.execute("update alerts set last_sent_at = now() where id = %s", (alert["id"],))
                conn.commit()
                sent += 1
            if not dry:
                log.info(f"Sent {sent} alert email(s)")


if __name__ == "__main__":
    main()
