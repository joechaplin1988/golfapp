# Golf Tee Time Scrapers — Usage Notes

Status: **seven platforms live** (Intelligent Golf, ESP, Golf Manager,
ClubV1, BRS, Shiji, Gladstone) — **129 clubs / 157 sheets per scheduled run (2026-09-10)**, the
whole of Kent & Sussex and Surrey fingerprinted, refreshed by GitHub Actions into Supabase (next 3 days every 2h, 7 days twice daily), searchable
at golfbookingapp.netlify.app. Every club is geocoded for radius search. A
discovery pipeline (below) finds new clubs and their platforms. See "Verified runs".

## Architecture

Each platform has its own scraper; they all share `golf_common.py` and emit
the **same record shape**, so the aggregator can merge them without caring
which platform a club runs:

| Script | Platform | Data source | Clubs configured |
|---|---|---|---|
| `intelligent_golf_scraper.py` | Intelligent Golf | server HTML | 22 sheets / 17 clubs |
| `esp_scraper.py` | ESP / EliteLive | session flow → HTML fragment | 6 |
| `golf_manager_scraper.py` | Golf Manager | clean JSON API | 2 |
| `clubv1_scraper.py` | ClubV1 | server HTML | 1 |
| `brs_scraper.py` | BRS Golf | JSON API (club-context cookie + Referer) | 13 |
| `shiji_scraper.py` | Shiji (hotel resorts) | open JSON API found via the booking page | 4 sheets / 2 clubs |
| `gladstone_scraper.py` | Gladstone Go (council / leisure trusts) | JSON API, anonymous session + `X-Use-Sso` header; **no prices** | 9 sheets / 4 clubs |
| `geocode_clubs.py` | — | Postcodes.io | fills lat/long for all |
| `discover_clubs.py` | — | OSM + county union + each club's site | finds clubs + their platform → review CSV |
| `probe_course_ids.py` | — | live tee sheets | approved review rows → platform ids |
| `apply_approved.py` | — | — | appends verified rows to the config |
| `enrich_courses.py` | — | each club's own site | proposes course type + yardage → `course_profiles.csv` |
| `apply_migrations.py` | — | `db/migrations/*.sql` | applies pending schema changes (run by the workflow) |
| `run_pipeline.py` | all | — | one command: syncs config → DB, scrapes a date window → DB |

`golf_common.py` owns the shared contract: the `TeeTimeResult` shape, config
loading (filtered by the new `platform` column), the polite-request policy
(user agent, 2s pacing, retry-once-then-skip) and the truststore TLS fix.
Each scraper only implements its own fetch + parse. **Add a second copy of
any of that shared logic and the scrapers will drift — extend `golf_common`
instead.**

Run one platform at a time; each reads the one shared config and picks out
its own rows:
```
python intelligent_golf_scraper.py clubs_config.csv --date 2026-09-12
python esp_scraper.py            clubs_config.csv --date 2026-09-12
python golf_manager_scraper.py   clubs_config.csv --date 2026-09-12
python clubv1_scraper.py         clubs_config.csv --date 2026-09-12
```
Each writes its own `tee_times_<platform>.json` (override with `--output`).

### The one cross-platform trap: how prices are stored
`prices_by_players` is always keyed by party size with the **total** for that
size as the value, and the keys are the party sizes actually bookable. But
the platforms report prices differently, and the scrapers normalise them:

- **ClubV1** already gives a per-party total (`ball-1..4`) — stored as-is.
- **Intelligent Golf** gives an explicit per-party-size price list — stored
  as-is. (Frequently non-linear: four-balls are often discounted.)
- **ESP** and **Golf Manager** give a single **per-player green fee**, with
  no group discount (verified on both). Only there is the total computed as
  `n × fee`. This multiply is safe *because it was verified for those two
  platforms* — never apply it to ClubV1 or Intelligent Golf, whose group
  prices really do vary.

So anything downstream must read the captured values and keys; it must never
assume 1–4 players or re-derive a total by multiplying.

## Setup
```
pip install requests beautifulsoup4
```

### If you get an SSL "certificate verify failed" error
Also install:
```
pip install truststore
```
This was needed on Joe's Windows machine. Cause: **AVG Antivirus does HTTPS
scanning** — it intercepts TLS and re-signs every certificate with its own
"AVG Web/Mail Shield Root". That root is installed in the Windows certificate
store (so Chrome/Edge are happy) but is not in `certifi`'s bundle, which is
what `requests` checks against by default — so Python rejects every HTTPS
request while browsers work fine. `truststore` makes Python verify against
the Windows store instead.

The scraper imports it inside a `try/except ImportError`, so a clean server
(the future n8n host, CI) that doesn't have it simply falls back to `certifi`
as normal. Nothing to configure either way. Do **not** "fix" this with
`verify=False` — that disables certificate checking altogether.

## The config file (`clubs_config.csv`)

One row per bookable sheet. Columns:

| Column | Meaning |
|---|---|
| `club_name` | Display name. For multi-course clubs, one row per course, named e.g. `West Malling (Spitfire)` |
| `platform` | `intelligent_golf` / `esp` / `golf_manager` / `clubv1` — decides which scraper claims the row |
| `base_url` | Platform-specific entry (see below) |
| `course_id` | Platform-specific id (see below); blank where the platform doesn't need one |
| `postcode` | UK postcode; drives geocoding |
| `latitude`,`longitude` | Filled in by `geocode_clubs.py` — leave blank |

What `base_url` / `course_id` mean per platform:

- **intelligent_golf** — `base_url` = the club's `.../visitorbooking` path;
  `course_id` = the numeric course (often not 1 — see below).
- **esp** — `base_url` = `https://www.e-s-p.com/elitelive` (shared host);
  `course_id` = the club's numeric ESP **clubid** (in its booking link).
- **golf_manager** — `base_url` = `https://<slug>.golfmanager.com`;
  `course_id` = optional `idResource` to pin one tee (blank = main tee).
- **clubv1** — `base_url` = `https://<slug>.hub.clubv1.com`;
  `course_id` = the numeric `courseId` (in the tee sheet's date links).

Rows with no `base_url` are skipped (logged), so it's safe to leave clubs
half-filled while you work through them.

## Geocoding — `geocode_clubs.py`

Turns each row's postcode into latitude/longitude via **Postcodes.io** (free,
no key, UK-only). Clubs don't move, so this is a one-off backfill plus one
lookup per newly added club:
```
python geocode_clubs.py clubs_config.csv          # fill blanks (safe to re-run)
python geocode_clubs.py clubs_config.csv --check  # look up & print, don't write
python geocode_clubs.py clubs_config.csv --force  # redo every row
```
It leaves already-geocoded rows alone and prints the admin district for each
postcode — **eyeball that column**. A postcode copied off a club's site can
be valid but wrong (a sponsor's, the secretary's home) and will still geocode
cleanly; the district is the cheap sanity check. A postcode maps to the
clubhouse, ~100m accurate — fine for radius search, not for directions.

Distance itself isn't stored — compute it at query time from the two
coordinate pairs (Haversine, or PostGIS — which the schema uses).

## Discovering new clubs

Kent & Sussex have **~160 real golf clubs** (OpenStreetMap); the original
hand survey covered 34. Three scripts take a county from "which clubs
exist" to config rows, with a **human review gate** in the middle — nothing
goes live from discovery without being looked at.

```
# 1. enumerate: club list (OSM Overpass) + official websites (county golf
#    union directory: kentgolf.org / sussexgolf.org). Both cached in
#    osm_*.json / union_*.json — the public Overpass mirrors are flaky and
#    the data changes on a timescale of years; --refresh re-queries.
python discover_clubs.py enumerate --county kent   --out candidates_kent.json
python discover_clubs.py enumerate --county sussex --out candidates_sussex.json

# 2. fingerprint: visit each club's OWN site, identify platform + whether the
#    booking page is actually public. Writes a review CSV, never the config.
#    --limit N for a batch; --exclude earlier.csv to resume without redoing.
python discover_clubs.py fingerprint candidates_kent.json candidates_sussex.json \
    --config clubs_config.csv --out candidates_review.csv --limit 40

# 3. YOU review candidates_review.csv (platform / access / confidence /
#    evidence). Then find the platform ids for the ones you approve — a
#    live-slot scan, because course_id is often NOT 1 (577, 578, 227, 2
#    found in the first batch) and two-course clubs need a row each:
python probe_course_ids.py candidates_review.csv --names "Mid Kent" "Dartford"

# 4. append only rows that were verified against a live slot, geocode, push.
#    The scheduled run syncs the CSV into the DB itself — no manual DB step.
python apply_approved.py
python geocode_clubs.py clubs_config.csv
git add clubs_config.csv && git commit -m "add clubs" && git push
```

`platform` values: our seven scrapers, plus `chronogolf` /
`gladstone` (seen, no scraper yet — counted so "which scraper next?" has
real numbers), and `no_site` / `unknown` / `blocked` (403, check in a real
browser) / `dead_link` (404, stale directory URL) / `error`. `access` is
separate from platform on purpose: a club can be confirmed on Intelligent
Golf *and* have a login-walled booking page (`login_required` — Littlestone,
Chislehurst), or have visitor booking switched off entirely — a ClubV1 hub
answers HTTP 200 with "Permission Denied" (`not_available`; five of the
seven ClubV1 clubs in the first batch). Only `access = public` is bookable.

Hard-won rules baked into the fingerprinting — each one was a real false
positive during the build:
- The county union pages carry a "Created by intelligentgolf" CMS credit in
  their own footer. It says nothing about any club. Only markers on the
  club's own site count.
- A "Powered by X" badge on a club's homepage isn't confirmation either. Only
  landing on the platform's domain, or seeing its actual tee-sheet markup,
  is. Vendor marketing roots (www.intelligentgolf.co.uk) are never followed
  as evidence.
- Once a platform is *hinted*, probe its known path directly
  (`/visitorbooking/` for Intelligent Golf, `/Visitors/booking` on a ClubV1
  hub) — several real clubs never link to their booking page from the nav.
- Results must be deterministic: a `set()` of hinted platforms once gave the
  same club two different answers on two runs.
- Being on the platform's *domain* isn't confirmation either. A ClubV1 hub
  root with `?ReturnUrl=/members/…` is the members' login (Kings Hill);
  `www.intelligentgolf.co.uk/tee_times` is the vendor's product page (two
  council courses link straight to it); a hub can exist with visitor booking
  switched off. Only the visitor-facing path (`/visitorbooking`,
  `/Visitors/`, `/elitelive/book_`, `visitors.brsgolf.com`…) or real
  tee-sheet markup confirms — and `probe_course_ids.py` demands a real sheet
  before anything can reach the config regardless.

First real batch (40 Kent clubs): **12 ready-to-add** (an earlier count of
17 included five ClubV1 hubs whose visitor booking is switched off — the
`not_available` state exists because of them). ClubV1 was 7 of the 40 — the
original survey had it at 1 — but only one of those seven (Sheerness)
actually has visitor booking open. Council
courses run by leisure trusts show up as **Gladstone** (MyTime Active),
**Chronogolf** (Everyone Active) and Better — worth a scraper decision once
the full county count is in.

## Loading into the database (Supabase)

The DB layer: `db/schema.sql` (the schema), `golf_db.py` (shared access),
`load_config_to_db.py` (config → clubs/courses), `load_tee_times.py` (scraper
output → tee_times). See `db/schema.sql`'s header for the data model.

**Connection** comes from the `DATABASE_URL` env var — never hardcoded.
Get it from Supabase: Project Settings → Database → Connection string → URI.
```
$env:DATABASE_URL='postgresql://postgres:<pw>@<host>:5432/postgres'   # PowerShell
export DATABASE_URL='postgresql://postgres:<pw>@<host>:5432/postgres' # bash
```
`pip install "psycopg[binary]"` for the driver.

**One-time:** paste `db/schema.sql` into the Supabase SQL editor and run it
(needs the `postgis` extension, which Supabase provides).

**Then the recurring pipeline** (this is what n8n will run on a cron):
```
# 1. config → clubs/courses (idempotent). run_pipeline.py now does this
#    itself at the start of every run, so it's only needed by hand for a
#    one-off load; a club pushed to the CSV goes live on the next run.
python load_config_to_db.py clubs_config.csv        # --dry-run to preview

# 2. scrape each platform for a date (writes tee_times_<p>.json + run_<p>.json)
python intelligent_golf_scraper.py clubs_config.csv --date 2026-09-12
python esp_scraper.py            clubs_config.csv --date 2026-09-12
python golf_manager_scraper.py   clubs_config.csv --date 2026-09-12
python clubv1_scraper.py         clubs_config.csv --date 2026-09-12

# 3. load that output into tee_times (refresh per course/date)
python load_tee_times.py                            # --dry-run to preview
```
Every scraper writes a **run manifest** (`run_<platform>.json`) alongside its
results — the per-course status (`ok` / `empty` / `error`) that step 3 needs
to clear now-booked slots on a genuine empty day but leave a sheet untouched
when its scrape errored. Both loaders take `--dry-run` (no DB, no
`DATABASE_URL` needed) to preview exactly what they'd write.

**Reading it back:** the app calls the `search_tee_times(...)` RPC (defined in
the schema) — radius + date + players + price, nearest-then-cheapest.

For automation, `run_pipeline.py` does the whole recurring job in one command
(scrape a date window straight into Supabase, no intermediate JSON):
```
python run_pipeline.py --days 3          # today..today+2, all platforms -> DB
python run_pipeline.py --days 1 --no-db  # scrape + summarise only (no DATABASE_URL needed)
```
It exits non-zero only if *every* course errored (site-wide block, bad
DATABASE_URL), so a scheduler can alert on real failure without noise from one
club being down.

## Automating the pipeline

The recurring job is "run `run_pipeline.py` on a schedule against Supabase."
Pick the runner that matches your setup:

### GitHub Actions — recommended (and required if you're on n8n Cloud)
`.github/workflows/scrape.yml` is ready: it's both the scheduler and the
Python host, so there's no server to run. Setup:

1. Put this project in a GitHub repo (`git init`, commit, push — `.gitignore`
   is included and keeps the scraper outputs/secrets out).
2. Repo → **Settings → Secrets and variables → Actions → New repository
   secret**, name **`GOLF_APP`** (the workflow feeds it into the
   `DATABASE_URL` env var the code reads). Value = the libpq keyword string
   for Supabase's **IPv4 session pooler** — NOT the direct host. GitHub's
   runners are IPv4-only and Supabase's direct `db.<ref>.supabase.co` host
   resolves to IPv6, so a direct string fails with "Network is unreachable".
   Get the pooler details from Supabase → **Connect → Session pooler**; it
   looks like:
   ```
   host=aws-1-<region>.pooler.supabase.com port=5432 user=postgres.<ref> password=<pw> dbname=postgres sslmode=require
   ```
   Note the pooler needs the project ref *in the username* (`postgres.<ref>`).
   Keyword form avoids URL-encoding special characters in the password.
3. It runs every 2 hours by default (fits the free tier on a private repo).
   For 30-min freshness, make the repo public (Actions minutes are then
   unlimited and there are no secrets in the code) and edit the `cron` line.
   There's also a **Run workflow** button for manual runs.

Verified live 2026-09-09: a run from GitHub Actions through the session
pooler wrote ~1,880 tee-time rows to Supabase.

n8n Cloud can still own **alerting** if you want: add an n8n Webhook workflow
and uncomment the "Notify n8n on failure" step in the workflow.

### Self-hosted n8n (only) — `n8n/golf_pipeline.workflow.json`
An importable workflow: 30-min Schedule → Execute Command
(`run_pipeline.py --days 3`) → IF-fails → alert node. **This needs self-hosted
n8n** — the Execute Command node is disabled on n8n Cloud, which is why
GitHub Actions is the path above. If you self-host later: import it, fix the
path in the Execute Command node, set `DATABASE_URL` in n8n's process env, and
wire the alert node.

### Other options
A cheap always-on host (Fly.io / Render / a small VPS) running the same
command on cron, or exposing a tiny HTTP endpoint that n8n Cloud's Schedule →
HTTP Request node triggers — use this if you specifically want n8n Cloud
orchestrating. More moving parts than GitHub Actions for the same result.

## The search page (`web/index.html`)

Static page on Netlify (golfbookingapp.netlify.app): postcode → Postcodes.io
→ `search_tee_times` RPC → rows with a Book link to the club's own site. The
anon key is injected at deploy from the `SUPABASE_ANON_KEY` env var into
`web/config.js` (see `netlify.toml`); it is never committed.

PostgREST caps any response at **1,000 rows**, which a busy search exceeds
(Sevenoaks, 20 mi, 2 players, tomorrow: 1,017). Exactly 1,000 results is the
tell. The page therefore loads every page up front (`?limit=1000&offset=`
on the RPC URL with `Prefer: count=exact`, reading the true total from the
`Content-Range` header) and then does all filtering locally. Note a `Range`
header does *not* limit RPC calls — only the query parameters do.

Results are **grouped one card per course** (name, distance, "N tee times
from HH:MM", "from £X"), expanding to the individual times with Book links —
so a 20-mile search is ~25 cards, not 1,000 rows. A filter bar appears after
the first search: "Play between" (05:00–21:00, half-hour steps, default
06:00–20:00), holes, max total price and sort (nearest / cheapest / earliest).
Filters apply instantly to the loaded rows; only postcode/date/players/radius
go to the database.

## Finding a new Intelligent Golf club's config

**Start from the club's own website, not from a guessed subdomain.**

Guessing `https://<slug>.intelligentgolf.co.uk/visitorbooking/` from the club
name is a fast first try and worked for 13 of 16 — but it produced a **false
negative every time it failed**, and the failures looked like "this club
isn't on Intelligent Golf" when in fact all three were. The reliable method:

1. Open the club's own website
2. Follow its "Book a tee time" / "Green fees" link — that's the real host
3. Confirm the page title reads `Visitor's Teetime Booking at <club>` and
   the page links to `intelligentgolf.co.uk` (the "Powered by" footer credit)

Hosts seen in practice, none of which the slug guess would have found:
`members.royalcinqueports.com`, `www.nizelsgolfclub.com`,
`www.heroncountryclub.uk`, `www.theridge.co.uk`.

Two further traps worth knowing:
- **A club's marketing domain may not be its booking domain.** Nizels'
  booking sits on `nizelsgolfclub.com` while `nizels.co.uk` is an unrelated
  React app with no booking in the HTML at all.
- **A subdomain that exists may be the members' portal, not the visitor
  sheet.** `littlestone.intelligentgolf.co.uk` and
  `royalstgeorges.intelligentgolf.co.uk` both resolve and load, but neither
  is a public visitor tee sheet.

There's no wildcard DNS on `intelligentgolf.co.uk`, so a slug that resolves
is always a real tenant — a DNS lookup is a free, zero-traffic way to test a
guess. Just don't read a non-resolving slug as "not on this platform".

### Getting course_id right — read this, it's the easy one to get wrong
**`course_id` is NOT always 1.** Of 16 sheets configured, six use something
else: Canterbury 241, Piltdown 144, Haywards Heath 855, West Malling
129/131, REGC 98/99, Mannings Heath 80. Filling them all in as `1` would
have produced silently wrong or empty results, with no error to notice.

**Do not trust the `course` input on the page** — several clubs render it
empty even though the sheet uses a real ID. The authoritative source is a
live booking link:

1. Open `https://<slug>.intelligentgolf.co.uk/visitorbooking/?date=DD-MM-YYYY`
   on a date that actually has availability (weekday afternoons are a good
   bet; today is often already booked out)
2. Read `course=` out of any slot's booking link
3. That value is the `course_id`

### Multi-course clubs need one row each
Four of the clubs run more than one course, each with its own separate tee
sheet and its own inventory. The config is one row per *course*, not per
club — give each its own name so they stay distinguishable in the output:

```
West Malling (Spitfire),https://westmalling.intelligentgolf.co.uk/visitorbooking,129,,,
West Malling (Hurricane),https://westmalling.intelligentgolf.co.uk/visitorbooking,131,,,
```

Verify they're genuinely different sheets before adding both — West
Malling's two courses happened to return the same *number* of slots (55),
which looks like a duplicate until you compare the actual times.

Leave `postcode`/`latitude`/`longitude` blank for now — a separate step will
fill these in via the Postcodes.io API once we build the geocoding pass.

**Use the canonical hostname, including `www.` if the site redirects to it.**
This bit us on the first run: `theridge.co.uk` 301-redirects to
`www.theridge.co.uk`, and the bare host serves a certificate valid only for
`intelligentgolf.co.uk`, so requesting it fails verification before the
redirect can even be followed. Load the page, let it settle, and copy the URL
from the address bar rather than typing the domain from memory.

Rows with no `base_url` or `course_id` are skipped automatically (logged as a
warning) — safe to leave partially filled while you work through the list.

## Running it
```
python intelligent_golf_scraper.py clubs_config.csv --date 2026-09-12
```
Writes results to `tee_times_output.json` by default (`--output` to change).

## Resolved: 9 vs 18 holes
Previously flagged as an open question. **Answered — and there is nothing to
build.** The "9 holes / 18 holes" toggle on the page is a *client-side*
filter over radio inputs named `maxholes`; the server returns identical HTML
whether or not it's passed. A single request already gets the full
inventory — there is no separate 9-hole sheet to fetch.

The `holes` field in the output means **the longest round bookable at that
slot**:
- `"18"` — normal case
- `"9"` — slot restricted to 9 holes (late twilight slots without daylight
  for a full round). These carry `&holes=9` on their own booking link and are
  labelled "9 holes only" on the page.

Every slot permits 9 holes, so don't read `holes: "18"` as "9 not available" —
read it as "up to 18 available".

Related: `group` in the URL is **which tee** (1 = 1st, 2 = 10th), not a player
grouping. The Ridge returns 0 slots for `group=2`; omitting it returns the
full sheet, which is what the scraper does. Re-check on any club that
genuinely runs a two-tee sheet.

## Known gaps — resolve before relying on this for real
- **Booking requires a club account**: the deep link we emit lands correctly
  on the club's booking flow for that exact slot, but the user then hits a
  register/login wall. Fine for the click-through model, but it's real
  friction worth knowing about before writing any "book in 2 clicks" copy.
  The *tee sheet itself* (times + prices) is fully public, no login — which
  is what keeps the scraping side clean.
- **5 clubs still unconfigured** and deliberately left blank — see "Club
  coverage" below for which and why. They're skipped automatically.
- **Not yet wired into n8n**: this is a standalone script for now. Next step
  is an n8n workflow that runs this on a cron schedule and writes results
  into the real database instead of a local JSON file.

## Reading the logs
The scraper distinguishes three different kinds of "no results", so that a
routine empty day doesn't look like a breakage once this runs unattended.
This matters: before, both logged the same warning, and real failures would
have hidden inside ordinary empty Saturdays.

| Log level | Meaning | Action |
|---|---|---|
| `INFO  ... Found N available tee time(s)` | Working normally | None |
| `INFO  ... No availability (club's page says so explicitly)` | The club's own page shows a "no tee times" notice. Normal — a fully booked day looks exactly like this | None |
| `WARNING ... no slots AND no no-availability message` | Recognised the tee sheet, but it has neither slots nor a notice | Look at the page — markup may have shifted |
| `ERROR ... Did not recognise this page` | Not an Intelligent Golf tee sheet at all — wrong URL, redirect to a login wall, error page, or the club changed platform | Check `base_url`/`course_id` |

The two "empty" cases are told apart by the presence of a
`.no-teetimes-message` element, **not** by its text. That's deliberate: the
wording differs per club. The Ridge says "Sorry, there are no teetimes
available on Saturday 19th September", Wildernesse says "We're sorry but all
of the available tee times on this day have been booked." The class name is
stable across both; the sentence is not.

## Verified runs — 2026-09-08
```
python intelligent_golf_scraper.py clubs_config.csv --date 2026-09-12
```
```
[INFO] Loaded 1 club(s) with complete config out of the config file
[INFO] [The Ridge] Found 26 available tee time(s) for 2026-09-12
[INFO] Wrote 26 tee time record(s) to tee_times_output.json
```
Cross-checked against the live page for Sat 12 September: 26 slots from
12:20 to 17:16, in three price bands (£43 / £35 / £28 per player), with the
last two slots marked "9 holes only" — all matching the scraped output
exactly.

Sample record:
```json
{
  "club_name": "The Ridge",
  "date": "2026-09-12",
  "time": "12:20",
  "holes": "18",
  "prices_by_players": { "1": 43.0, "2": 86.0, "3": 129.0, "4": 172.0 },
  "booking_url": "https://www.theridge.co.uk/visitorbooking/?date=12-09-2026&course=1&group=1&book=12:20:00",
  "checked_at": "2026-09-08T18:37:37.272211Z"
}
```

### Second club — Wildernesse (subdomain install)
```
python intelligent_golf_scraper.py clubs_config.csv --date 2026-09-09
```
```
[INFO] Loaded 2 club(s) with complete config out of the config file
[INFO] [The Ridge] Found 30 available tee time(s) for 2026-09-09
[INFO] [Wildernesse] Found 4 available tee time(s) for 2026-09-09
[INFO] Wrote 34 tee time record(s) to two_club_test.json
```
Wildernesse runs on `wildernesse.intelligentgolf.co.uk` with completely
different branding, and needed **zero** code or config special-casing — the
slot markup is identical to The Ridge's. This is the result that supports
the "one scraper covers all 17 Intelligent Golf clubs" plan.

Cross-checked live for Wed 9 September: 16:30 / 16:40 / 16:50 / 17:00 at
One Ball £125.00, Two Ball £250.00, Three Ball £375.00, Four Ball £480.00 —
matching the scraped output exactly.

**Don't compute prices from the 1-player rate.** Wildernesse's four-ball is
£480, not 4 x £125 = £500 — there's a group discount. The Ridge happens to
price linearly, so testing on The Ridge alone would have made a
"price x players" shortcut look correct. Always use the per-player-count
values the scraper captures in `prices_by_players`.

Also worth noting: Wildernesse shows only an "18 holes" option, no 9-hole
toggle at all, and the scraper handles that correctly (everything defaults
to `holes: "18"`). So the per-slot holes detection degrades cleanly on clubs
that don't offer 9-hole rounds.

## Club coverage

**Configured and confirmed returning live data (97 sheets / 79 clubs)** —
the 16 clubs below from the original survey, plus 54 found by the discovery
pipeline (2026-09-09). Survey-era re-check (clubs from the original manual
survey re-fingerprinted with the residue tricks): Crowborough Beacon (IG on
its own domain) and Royal Cinque Ports (IG subdomain — visitor slots are
rare, first seen 6 days out) added; Rye and Wrotham Heath still show no
visitor slot in 14 days (IG sheets exist; re-probe occasionally); London
Beach, Lottbridge and Tunbridge Wells have no online visitor booking. Residue pass (clubs the plain-requests fingerprint
couldn't resolve, re-checked with a browser user agent, the in-app browser
and web search — 16 clubs / 17 sheets): Intelligent Golf — Cuckfield,
Tilgate Forest, Rookwood, Slinfold (Main + Academy), Nevill, Horsham (Oaks;
the Firs course isn't bookable online); BRS — Cherry Lodge, Leeds Castle,
St Augustine's (1st tee; the 9th-tee sheet is empty), Broome Park (1st tee);
ESP — Sedlescombe, Darenth Valley, Cottesmore; ClubV1 — Ashford, Tenterden,
Bexleyheath. Not added: Wrotham Heath (IG sheet exists but no visitor slot
in 14 days — like Rye, re-probe later). BRS batch (one scraper, nine clubs): Lewes,
Lindfield, Lydd, Hythe Imperial, Peacehaven, Pyecombe, Seaford Head, Walmer
& Kingsdown, Westgate & Birchington — Highwoods has a BRS hub but visitor
booking switched off (0 slots on every date, both tees), so it is not added. Batch 1: Sundridge Park (East + West), Hever Castle
(Championship + Princes), Royal Blackheath, West Kent, Pedham Place,
Chelsfield Lakes. Batch 2: Chestfield, Weald of Kent, Langley Park, West
Sussex, Seaford, Mid Sussex, Ham Manor, Stonelees (Executive, Heights,
Exec-9/Heights-9), North Foreland (main, Par-3 Northcliffe), Etchinghill,
Kings Hill, Bognor Regis, Sheerness. Batch 3: Ifield, Royal Ashdown Forest
(Old + West), Holtye, Selsey, Singing Hills (6 combinations of 3 nines),
Dyke, Littlehampton, Hill Barn. Original survey clubs:
The Ridge, Wildernesse, Knole Park, Lamberhurst, Canterbury, Cooden Beach,
Crowborough, Piltdown, Haywards Heath, Cinque Ports, Nizels, West Malling
(Spitfire + Hurricane), REGC (Devonshire + Hartington 9), Mannings Heath
(Waterfall + Kingfisher), Copthorne, The Heron (Heron-Hawk + Hawk-Vixen +
Vixen-Heron).

**Configured but not yet seen with availability:**
- *Copthorne (Back 9), course 4* — the course exists in the club's own
  selector and the sheet loads, but showed no availability across 6 days
  scanned. Config is a best guess; confirm the `course_id` against a live
  booking link the first time it does have slots.

**Deliberately left blank (skipped automatically) — 2 clubs:**
| Club | Why |
|---|---|
| Littlestone | Visitor booking is **behind a login wall** — confirmed on both its `intelligentgolf.co.uk` subdomain *and* its own domain, so this isn't a wrong-host problem. Out of scope per the project's stance on not scraping behind authentication |
| Royal St Georges | Its own site has no online booking at all and shows no Intelligent Golf markers. The `royalstgeorges.intelligentgolf.co.uk` host exists but never showed availability across 40 days. An Open Championship venue — visitor golf is arranged, not openly bookable. Genuinely out of scope, not a config problem |

**Correction to an earlier note in this file:** Cinque Ports, Nizels and The
Heron were briefly written off here as "probably misclassified in the
platform survey". That was wrong — **all three are Intelligent Golf**, and
all three are now live. The survey was right; the subdomain-guessing method
used to look them up was what failed. See the discovery section above for
the method that actually works.

## Verified runs — 2026-09-08

### Full batch — 16 sheets, 521 tee times
```
python intelligent_golf_scraper.py clubs_config.csv --date 2026-09-09 --output batch_run.json
```
All 16 sheets returned data, plus one clean `No availability` for Copthorne
(Back 9). 521 records total. Validated automatically across the whole set:
zero records missing prices, zero non-positive prices, zero malformed
booking URLs, zero duplicate (club, time) pairs, zero wrong dates, and every
record's URL `course=` matching its configured `course_id`.

Spot-checked Copthorne against its live page: 27 slots 07:00–18:52 at One
Ball £75.00 / Two £150.00 / Three £225.00 / Four Ball £260.00 — exact match.

### Never compute prices from the 1-player rate
Confirmed at scale: **6 of 16 sheets** price a four-ball below 4x the single
rate. Copthorne is £260 rather than £300, Wildernesse £480 rather than £500,
and Mannings Heath (Waterfall) and Copthorne discount on *every single slot*.
Use the captured `prices_by_players` values.

### The retry earned itself immediately
The first full batch run lost West Malling (Hurricane) to a
`RemoteDisconnected` — 55 tee times that were definitely there, silently
dropped. That's what prompted the retry-once-then-skip now in the scraper;
the identical run afterwards returned all 16 sheets. Retries are limited to
plausibly-transient failures (connection drops, timeouts, 5xx); a 4xx fails
immediately rather than repeating a request we already know is wrong.

## `prices_by_players` also tells you the bookable party sizes

The keys are not always `1,2,3,4`. Which player counts a slot offers *is*
the club's party-size rule for that slot, and the scraper captures it without
needing to know the rule:

- **The Heron has a 2-player minimum** — every slot offers only `2,3,4`, no
  single tier at all. A naive "find me a tee time for 1 player" search that
  ignores these keys would wrongly offer The Heron to solo golfers.
- **Cinque Ports varies by weekday.** The club states "Three & Fourball only
  Monday & Thursday; Single & Twoball Tuesday & Wednesday" — and on a
  Wednesday its slots came back with exactly `1,2`. The rule falls out of the
  data for free.
- **Capacity-limited slots**: a slot near the end of a busy sheet may be
  `bookable:2`, showing a "2x / Maximum of 2 players" badge, and yields only
  `1,2`. Crowborough and Haywards Heath both had a few.

So the player-count filter should test membership of `prices_by_players`,
not assume 1-4 and multiply.

## Watch out: 27-hole clubs can double-count

The Heron is 27 holes sold as three 18-hole combinations (Heron-Hawk,
Hawk-Vixen, Vixen-Heron), each its own `course_id` and its own tee sheet.
Because the combinations share nines, **the same physical tee time can appear
on two sheets**: Heron-Hawk and Vixen-Heron shared 11 identical
(time, price-set) slots out of 86 Heron rows in one run — roughly 13%.

Only one of those can actually be booked. Summing rows across course
combinations therefore overstates real availability. Before this feeds a
user-facing count, de-duplicate on (club, date, time) for clubs where the
courses share holes. Not yet proven that the shared slots are the same
bookable slot — it's the obvious reading given the shared nines, but worth
confirming with the club before relying on either behaviour.

Mannings Heath and West Malling are genuinely separate courses (no shared
holes) and showed no such overlap, so this is a 27-hole-specific issue
rather than a general multi-course one.

### Full run — 21 sheets, 628 tee times
```
python intelligent_golf_scraper.py clubs_config.csv --date 2026-09-09
```
21 of 22 configured sheets returned data; the 22nd (Copthorne Back 9) logged
a clean `No availability`. Validated across all 628 records: zero missing or
non-positive prices, zero malformed booking URLs, zero duplicate
(sheet, time) pairs, zero wrong dates, zero malformed times, and every
record's URL `course=` matching its configured `course_id`.

## Verified runs — 2026-09-09 (four platforms)

All four scrapers run against live data for 2026-09-12:

| Platform | Clubs | Tee times |
|---|---|---|
| Intelligent Golf | 22 sheets | 524 |
| ESP | 6 | 193 |
| Golf Manager | 2 | 153 |
| ClubV1 | 1 | 39 |
| **Total** | **28 clubs** | **909** |

Validated across all 909 records: zero missing/non-positive prices, zero
malformed times, zero non-http or empty booking URLs, `holes` always 9 or 18,
`platform` and `checked_at` always set, all price keys numeric.

Live spot-checks:
- **Golf Manager** (Redlibbets 06:24): our £60 matched the live API's £60
  per-player, and the slot's `min=max=1` (one space left) correctly produced
  a `{"1"}`-only record.
- **ClubV1** (Willingdon 14:12): the booking deep link resolved to a real
  "Add Booking — Sep 12 14:12" page, and ball-1..4 totals (£30/60/90/120)
  matched.
- **ESP** (Westerham): flat £75 green fee → £75/150/225/300 for 1–4.

Two platform-specific notes worth keeping:
- **ESP Prince's** uses a different entry (`book_widedaterange.php`, a calendar
  chooser with the activity pre-selected) instead of the usual group chooser.
  The scraper now treats that as a valid date page and skips the group step.
  Prince's is a 27-hole links sold as three 18-hole combinations, like The
  Heron — the same cross-course de-duplication caveat applies.
- **Golf Manager** publishes real per-slot party limits (`min`/`max`), so its
  records legitimately vary between `{"1"}`, `{"1","2"}` and `{"1".."4"}`.
  That's remaining-capacity data, not a bug — the player-count filter should
  use it.

### Course character on the search page (2026-09-10)
"20 tee times from £45" only helps someone who already knows the courses.
Joe's call: show **course type and yardage**, in the **expanded panel**, not
on the summary card — the card is at its limit and this is "help me choose",
not "help me scan". Par, a pay-and-play flag and Google ratings were
considered and held back; ratings are the most valuable of the three and the
most complex (Places terms restrict caching, require attribution, and bill
per call), so they're a separate decision once there are users.

The data is static, so it does not belong in the 2-hourly scrape:
`enrich_courses.py` proposes rows from each club's own website into
`course_profiles.csv`, that file is **eyeballed**, and `load_config_to_db`
syncs it into `courses.course_type` / `courses.yardage` on every run. Blank
cells never overwrite a stored value (`coalesce` in `upsert_course`), so a
half-filled file is safe to commit. Migration `002` adds the columns and
rebuilds `search_tee_times` to return them (Postgres won't let CREATE OR
REPLACE change a function's OUT columns — it has to be dropped first).

Yardage is deliberately approximate: every course plays a different length
off each tee. We take the medal/white figure, which is what clubs quote, and
`enrich_courses.py` keeps every candidate it saw in `all_yardages` so a
misread scorecard is visible at review time.

### Parked courses, and Cloudflare vs the CI runner (2026-09-10)
`clubs_config.csv` now has an optional `scrape_enabled` column; `false`
parks a row (kept, with its reason, but not scraped). First use: the seven
**Mytime Active** sheets (High Elms 18/9, Orpington Cray 18/9 + Ruxley 9,
Bromley 18/9). Their tenant sits behind Cloudflare, which answers **403 to
the GitHub runner** while the identical request — same code, same polite
user agent — returns 200 from a home connection. Both our own user agent and
a browser one work from home, so this is not about how we identify
ourselves; it is the runner's address or its TLS fingerprint. Either is an
anti-bot control, and working around it (proxies, TLS spoofing) is
circumvention, so we don't. Parked rather than deleted: 49 identical errors
a run would bury every real failure. Poult Wood is on TM Active's separate
tenant and is unaffected — it scrapes fine (713 rows).

To un-park: set `scrape_enabled` back to blank. Worth a note to Mytime
Active asking for access if those courses matter.

### Schema migrations, and the constraint that ate two batches (2026-09-10)
`courses.platform` had a CHECK listing every allowed platform name. It
duplicated `golf_common.PLATFORMS`, drifted (it said `concept_shiji`, never
had `gladstone`), and so the config→DB sync raised
`violates check constraint "courses_platform_check"`. Two consequences, both
worse than the constraint was ever worth:
1. `load_config_to_db.load()` wrapped the whole sync in ONE transaction, so
   one rejected club rolled back every other new club — a whole Surrey batch
   scraped clean and then had no course rows to write to (49 "No course row"
   errors, 0 rows stored). It now commits **per club** and names what it skipped.
2. Fixing the constraint needed a psql session, which is exactly the reason
   the shared-nines de-duplication had been stuck as a TODO for a day.

So schema changes now ship as commits: `db/migrations/*.sql` +
`apply_migrations.py` (records applied files in `schema_migrations`, one
transaction each, exits non-zero on failure), run by the workflow before the
scrape, where `DATABASE_URL` already lives. `001` replaces the platform list
with a format check — the scrapers own that vocabulary.

### Surrey residue — 5 more clubs, and unpriced BRS slots (2026-09-10)
Same treatment as the Kent residue (browser UA, `<slug>.intelligentgolf.co.uk/visitorbooking/`
probes, booking-subpage crawl): **Chipstead, The Drift** (IG),
**Addington Palace** (ClubV1), **Silvermere** (BRS), **Cranleigh** (ESP) —
`8 ok, 2 empty, 0 error; 221 tee times`.

Silvermere exposed a real hole: it had 45 open slots and BRS returned
`green_fee1_ball: null` for every one, so the parser dropped them all and the
club vanished. Unpriced BRS slots are now **kept with null prices**, matching
the Gladstone policy — a club with published availability should never be
invisible because it doesn't publish a price. (Intelligent Golf's separate
"£0.00, no per-player lines" case is still skipped; that's a handful of
9-hole combos and £0.00 may mean something different.)

Out: Oaks Sports Centre (BRS hub, no visitor slot in 14 days), Betchworth
Park (ClubV1 "Permission Denied"), Dulwich & Sydenham (IG login), and the
login-walled IG set (Camberley Heath, Cuddington, Purley Downs, Puttenham,
Royal Wimbledon, West Hill, Woking, Worplesdon, Royal Mid-Surrey, Coombe
Hill, Farnham). No public online visitor booking found: Wentworth, Queenwood,
The Wisley, St George's Hill, Walton Heath, Sunningdale, Foxhills,
Roehampton, Effingham Park, Hersham. One-offs left: Sandown Park
(yourgolfbooking.com), Bletchingley (TeeItUp — GolfNow's white-label, needs
a policy call).

### Surrey batch 1 — 39 clubs / 42 sheets (2026-09-09)
`discover_clubs.py enumerate --county surrey` (121 clubs; the Surrey union
directory at surreygolf.org uses the same CMS as Kent/Sussex) →
`fingerprint` (115 rows: IG 39, BRS 11, ESP 6, ClubV1 5, GM 2; 43
ready) → `probe_course_ids.py --all` (52 proposed rows, 43 verified) →
Joe approved all verified → `apply_approved` + geocode → test
`run_pipeline.py --config <42 rows> --no-db --days 2`: `73 ok, 8 empty, 3
error; 3208 tee-time row(s)`. The 3 errors were North Downs and Pyrford
(Golf Manager) timing out at 20s; both answer in ~25s, so the shared request
timeout is now 45s. Fixes made by hand before applying: Reigate Heath's site
carries a Luton postcode (web agency) — set RH2 8QR; "Richmond Park Golf
Course" had matched The Richmond Golf Club's ClubV1 hub (a different, public
course) — dropped; a few blank postcodes filled from the clubs' contact pages.
Unverified and left out: Hindhead, Hankley Common (IG sheets, no visitor
slot in 14 days — private-club pattern), The Addington (ESP, no clubid on
site), Coombe Wood's 8-hole/practice sheets and the 10th-tee sheets at
Clandon, Surbiton and Lingfield Park (empty). Surrey residue (login-walled,
unknown, no_site) still to do.

### Shiji + Gladstone — the last two Kent/Sussex platform families (2026-09-09)
Recon → build in one afternoon; both verified with
`run_pipeline.py --config <13 new rows> --no-db --days 3` → `Done: 26 ok, 13
empty, 0 error; 1908 tee-time row(s)`.

**Shiji** (Dale Hill Woosnam + Old, East Sussex National East + West): the
booking page embeds `window.__API_ENDPOINT__` and the API course id; the sheet
is `GET {api}/course/{id}/availability/{date}?players=n&holes=18` — no auth.
`prices.guest` is pence per player and identical for 1–4 players (Dale Hill
Saturday £45 → £45/90/135/180), so this joins ESP and Golf Manager as a
verified multiply case. We query each party size because the API only returns
slots with room for that many. Tudor Park (Marriott) runs the same app but its
booking host is unreachable (certificate for another domain, 403 on http) —
left out.

**Gladstone Go** (High Elms 18/9, Orpington Cray 18/9 + Ruxley 9, Bromley
18/9 on Mytime Active's tenant; Poult Wood 18/9 on TM Active's): every call
401s until you hit `/api/samlauthentication/anonymous` AND send
`X-Use-Sso: 1`. Then `/api/configuration/sites` (site ids, postcodes — the
Mytime tenant also lists six Birmingham courses and Dibden in Hampshire),
`/api/configuration/activity-groups` (18/9-hole groups per site) and
`/api/availability/V2/sessions?…siteIds=&activityGroupIds=&dateFrom=&dateTo=`
(one activity per tee time, UTC times, spaces left). **No price anywhere** —
Gladstone only prices a slot when it is leased into a basket, which holds it
against real customers, so we don't. Rows carry bookable sizes with `null`
prices; the page shows "price on club site" and a max-price filter excludes
them (Joe's decision: include them, 2026-09-09). Config `course_id` is
`SITE/GROUP`, one row per group because 18- and 9-hole groups share times.

### Pipeline now threaded per platform; 7-day window (2026-09-09)
`run_pipeline.py` scrapes each platform in its own thread (same per-club
pacing, own session; DB writes stay on the main thread), so a run takes about
as long as the slowest platform: the 17-sheet residue config went from ~65s
serial to 26s. The workflow has two crons — `0 */2 * * *` with `--days 3`
and `30 5,17 * * *` with `--days 7` — so the search page has a week of data
and the next three days stay fresh. Manual runs default to 7 days.

### Residue pass — 17 sheets, 633 tee times over 2 days (2026-09-09)
`run_pipeline.py --config <only the new rows> --no-db --days 2` → `Done: 16
ok, 18 empty, 0 error; 633 tee-time row(s)` (the empties are same-day
sheets). What the re-check taught us, worth keeping:
- Most "blocked" (403) and "error" (TLS) sites were just rejecting the
  scraper's user agent or had a certificate for a different host — a browser
  user agent and the bare domain got in (Cherry Lodge, Cuckfield,
  St Augustine's, Sene Valley, Bexleyheath, Tenterden all surfaced this way).
- A club's IG subdomain (`<slug>.intelligentgolf.co.uk/visitorbooking/`)
  is worth probing whenever its site links the vendor at all — Tilgate,
  Rookwood, Cuckfield and Horsham only exist there.
- Two more platform families appear in the county: council courses on
  Gladstone (MyTime Active: Bromley, High Elms, Barnehurst, Cobtree,
  Orpington ×2; TM Active: Poult Wood ×2) and a hotel-group tee booking app
  at `book.<hotel>.co.uk/golf` (Dale Hill, Tudor Park, East Sussex
  National). Neither has a scraper yet. One-offs: Rustington (Fusemetrix),
  Tonbridge Golf Centre (BookingHound), Lullingstone (Chronogolf).
- Confirmed not bookable online: Shortlands (members'-guest only), Whitstable
  & Seasalter, Upchurch River Valley (ESP members' login only), Sene Valley
  (ClubV1 hub, visitor booking off), Gillingham and Cowdray (IG subdomain,
  no visitor sheet), Rochester & Cobham and Worthing (IG, login required),
  Horam Park, Villa, Bay View, Dewlands Manor, Fawkham Valley, Burgess Hill
  (turn up and play), Great Chart, Hilden Park, Manston, Boars Head (site
  down), Gravesend Golf Centre, Kingsnorth/Homelands (site dead), Shooters
  Hill (domain parked), Kent & Surrey (site returns Database Error),
  Ashdown Park, Cottesmore's Griffin course and Effingham Park (GolfNow only).

### BRS Golf — 9 clubs, 340 tee times for 2026-09-10 (2026-09-09)
`python brs_scraper.py clubs_config.csv --date 2026-09-10` → `brs: 9 ok, 0
empty, 0 error`, 340 records (316 × 18 holes, 24 × 9 holes — Peacehaven's
early slots are 9-hole only). Through the pipeline for a 2-day window:
`Done: 17 ok, 1 empty, 0 error; 418 tee-time row(s)` (the empty is Pyecombe
same-day, which its own page confirms: "0 of 0 tee times"). Spot-checks:
Pyecombe 13:45 £40/80/120/150 for 1–4 — the 4-ball discount proves BRS
prices are per-party TOTALS (stored as-is, never multiplied); Lydd 07:00
£38 matched the sheet. The deep link `visitors.brsgolf.com/<slug>#/course/1`
opens the club's live sheet.

First scheduled run with BRS in (GitHub Actions, 2026-09-09): `Upserted 61
club(s) and 78 course(s)` then `Done: 131 ok, 25 empty, 0 error; 3243
tee-time row(s) written` — all nine BRS courses `ok`, 403 BRS rows in the
database for the 2-day window.

What the first recon got wrong: BRS is **not** Cloudflare-challenged for
plain requests. The API just needs the club page loaded first on the same
session (context cookie) plus the app's own headers and a `Referer` of the
club page — without the cookie it 500s "Could not get tee sheet", without
the Referer it 400s "Object reference not set". The host is shared, so the
scraper clears cookies before each club.

### County-wide — 69 sheets / 52 clubs, 2,955 tee times (2026-09-09, GitHub Actions)
After the three discovery batches: `Done: 117 ok, 19 empty, 2 error; 2955
tee-time row(s) written`, every newly approved club scraping real slots on
its first scheduled run. The 2 "errors" are one Intelligent Golf same-day
empty state (sheet present, no slots, no no-availability message) that the
three-way classifier reports as "markup may have changed" — a mislabel, not
a failure; the same sheets parsed normally for the next day.
