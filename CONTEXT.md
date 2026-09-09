# Golf Tee Time Aggregator — Project Brief for Claude Code

## The goal
Build a golf tee time aggregator app for Kent/Sussex (eventually UK-wide).
GolfNow only shows courses that are its own platform members — this app
scrapes clubs' own visitor booking pages directly to surface everything
GolfNow misses. User searches by postcode radius, date, players, price;
results link out to each club's own real booking page. We do NOT process
bookings or payments ourselves — pure aggregator/search layer, click-through
only.

## Current status: first scraper BUILT AND VERIFIED (2026-09-08)
Three files should be sitting alongside this brief in the project folder:
- `intelligent_golf_scraper.py` — the scraper itself
- `clubs_config.csv` — per-club config, The Ridge pre-filled as the test case
- `README.md` — usage instructions

**DONE** — verified against two clubs on 2026-09-08, covering both install
types: The Ridge (white-labeled on the club's own domain) and Wildernesse
(on an intelligentgolf.co.uk subdomain), with no per-club code. Full
details, log-reading guide and sample records are in README.md.

Four issues fixed getting there, only one of which was in the scraper:
1. **AVG Antivirus HTTPS scanning** broke all Python TLS (re-signs certs
   with a root that's in the Windows store but not certifi's bundle).
   Fixed with `truststore`, optional-imported so a clean n8n host is
   unaffected. Python itself was never broken.
2. **Wrong hostname** — theridge.co.uk redirects to www., and the bare host
   serves a cert valid only for intelligentgolf.co.uk. Always copy the
   settled URL from the address bar.
3. **Real bug**: booking URLs were built with the "?" stripped, so every
   click-through link was dead. Since click-through IS the product, this
   would have been bad to ship.
4. `datetime.utcnow()` deprecation cleared.

**Club config now done** (2026-09-08): 21 tee sheets across 16 clubs
configured and confirmed live, 628 tee times in one run. Only 2 clubs
remain unconfigured, both genuinely out of scope: Littlestone (visitor
booking behind a login wall, confirmed on both its subdomain and its own
domain) and Royal St Georges (no online visitor booking at all — an Open
venue where visitor golf is arranged). Full breakdown in README.md.

**The platform survey was accurate.** An intermediate note in this file
claimed Cinque Ports / Nizels / The Heron looked misclassified; that was
wrong and has been retracted. All three are Intelligent Golf and all three
are now live. The failure was the lookup method — guessing
`<club>.intelligentgolf.co.uk` — which silently produces false negatives.
Their real hosts are members.royalcinqueports.com, www.nizelsgolfclub.com
and www.heroncountryclub.uk. No wider re-audit of the survey is warranted.

**Geography note**: The Heron is in Brentwood, Essex — outside the stated
Kent/Sussex scope. It's configured and working; keep or drop as a scope
call, and it's a fine early test case for the eventual UK-wide expansion.

Things learned that affect any future platform scraper:
- **course_id is not always 1** — 6 of 16 sheets use another value
  (241, 144, 855, 129/131, 98/99, 80). The page's own `course` input is
  often empty and cannot be trusted; read it from a live booking link.
- **Four-ball prices are frequently discounted** — 6 of 16 sheets price a
  four-ball below 4x the single rate. Never derive prices by multiplying.
- **Party-size rules come free in the data** — which player counts a slot
  offers IS the club's rule (The Heron is 2-player minimum; Cinque Ports
  varies by weekday). Filter on the captured keys, don't assume 1-4.
- **27-hole clubs can double-count** — The Heron sells three 18-hole
  combinations that share nines, and ~13% of its slots appear on two
  sheets. De-duplicate on (club, date, time) before showing counts.
- **Find clubs via their own website's booking link**, not by guessing
  subdomains — guessing gave a false negative on 3 of 3 misses.

**Scrapers 2-4 built & verified** (2026-09-09): ESP, Golf Manager and ClubV1
scrapers now join Intelligent Golf. Shared logic (record shape, config load,
retry, TLS fix, pacing) extracted into golf_common.py; each scraper is just
its own fetch+parse. Config gained a `platform` column. All four run off the
one clubs_config.csv, each picking its own rows. Combined live run:
**909 tee times / 28 clubs** — IG 524, ESP 193, GolfManager 153, ClubV1 39.
Geocoding also done: every club has lat/long via geocode_clubs.py (Postcodes.io).

Platform access patterns confirmed (details in each scraper's docstring):
- ESP: 3-step session flow (book_start -> 18-hole group -> ajax_gettime
  fragment). Prince's is a variant (book_widedaterange), now handled.
- Golf Manager: no-auth JSON at /ebookings/init.api?start=DATE. Easiest.
- ClubV1: server HTML at /Visitors/TeeSheet?date=&courseId=, real deep links.
- Price models differ: ClubV1 + IG give per-party totals (keep as-is); ESP +
  GolfManager give a per-player green fee (multiply by party size — verified
  safe for those two only).

**DB layer built** (2026-09-09). Geocoding done (all clubs have lat/long).
`db/schema.sql` = Postgres/PostGIS schema for Supabase: clubs -> courses ->
tee_times, RLS (public read, service-role write), and a
`search_tee_times(lat,lng,radius,date,players,max_price,holes)` RPC that IS
the radius/price product query. Shared `golf_db.py` + two loaders:
`load_config_to_db.py` (config CSV -> clubs/courses; splits multi-course rows,
flags The Heron's shared-nines dedupe) and `load_tee_times.py` (scraper output
-> tee_times via replace-on-refresh). Scrapers now emit a run manifest
(`run_<platform>.json`, per-course ok/empty/error) so the refresh clears empty
sheets but never wipes an errored one; scraper run() moved into
`golf_common.run_platform` (anti-drift). All transform/plan logic verified
offline via --dry-run; the SQL is UNRUN (no local PostGIS) so its first run in
Supabase is the real test. Full runbook in README ("Loading into the database").

**DB is LIVE and proven end-to-end** (2026-09-09). Supabase project up
(Postgres 17.6 + PostGIS; project ref kept out of the repo). schema.sql applied
(one fix vs the draft: search function ORDER BY must use column positions,
not output-column names). Config loaded (25 clubs / 31 courses, all geocoded).
run_pipeline.py scraped a 2-day window straight into Supabase — 1910
tee_times rows, 54 ok / 8 empty / 0 error. search_tee_times RPC returns real
results (326 four-balls within 20km of Sevenoaks on 10 Sep, nearest-cheapest).
RLS verified: anon can read + call the RPC, all writes blocked.
ACTION FOR JOE: the DB password was shared in chat — rotate it in Supabase
(Database -> Reset password) and update DATABASE_URL wherever it's set.

**Scheduling**: Joe is on n8n CLOUD, which can't run the Python (no Execute
Command node) — so n8n Cloud can't be the runner. Primary path is now
**GitHub Actions cron** (`.github/workflows/scrape.yml`): it's both scheduler
and Python host, free, runs run_pipeline.py --days 2, DATABASE_URL as a repo
secret. Default cadence every 2h (fits free tier on a private repo; go public
+ 30-min for fresher). requirements.txt + .gitignore added. The self-hosted
n8n workflow (n8n/golf_pipeline.workflow.json) is kept but only works on
self-hosted n8n. n8n Cloud can still do failure alerting via a webhook
(commented step in the Actions file). Full options in README ("Automating the
pipeline"). NOT YET DONE by Joe: push repo to GitHub, set DATABASE_URL secret.

**Next candidates** (not started, Joe to choose):
- Turn on scheduling: push repo to GitHub, add DATABASE_URL secret, enable
  `.github/workflows/scrape.yml` (Actions cron). This is the go-live for the
  automated refresh loop.
- The user-facing search: a thin page/endpoint calling the search_tee_times
  RPC via the Supabase anon key (radius + date + players + price). This is
  what turns the working backend into something a person can use.
- Deferred platforms: BRS (Cloudflare cf_clearance hop), Concept/Shiji
  (React + payments), Chronogolf (reCAPTCHA — go gentle). Harder tier.

## Platform survey — 34 clubs checked across Kent/Sussex, 7 distinct platforms found

| Platform | Clubs found | Data format | Auth | Difficulty |
|---|---|---|---|---|
| **Intelligent Golf** | 17 (half of all clubs surveyed) | Server-rendered HTML | None | Easiest — building this first |
| ESP (e-s-p.com / EliteLive) | 6 | HTML fragment, 2-step (date click → AJAX time fetch) | Anonymous session cookie, no login | Easy |
| BRS Golf (owned by GolfNow itself since 2013) | 3 | Clean JSON | Cloudflare `cf_clearance` challenge | Medium — needs a browser hop to get cookies, then can hit JSON API directly |
| Concept Spa & Golf / Shiji | 3 | Likely JSON, React frontend | Unconfirmed | Unknown — has own payment layer, needs dedicated recon before building |
| Golf Manager | 2 | Clean JSON | None apparent (only marketing cookies) | Easy |
| Chronogolf (Lightspeed) | 1 in sample, but likely covers hundreds of UK clubs | Clean REST JSON | None for browsing, but reCAPTCHA present — be conservative on request volume/enumeration | Easy per-club, but treat carefully at scale |
| ClubV1 (Club Systems Intl) | 1 | Server-rendered HTML | None | Easiest — same tier as Intelligent Golf |
| Native/bespoke (one-off club builds) | 1 (Cranham) | — | — | Low priority — doesn't scale across clubs |
| GolfNow-only | (excluded from count) | — | — | Deliberately out of scope — no gap to fill there |

**Build priority decided**: Intelligent Golf first (50% coverage alone),
then ESP (→ 68% combined), then Golf Manager + ClubV1 as cheap bonus adds
(→ 76% combined). BRS, Concept/Shiji, Chronogolf deferred until the easy
platforms are proven and shipped.

## Confirmed technical details — Intelligent Golf (this scraper)
- URL pattern: `{base_url}?date=DD-MM-YYYY&course={course_id}` — confirmed
  working by directly editing the URL and loading it (no need to click
  through the UI)
- Data is fully server-rendered in the initial HTML — no JS/API call
  needed, confirmed via "View Page Source" + searching for a visible time
- HTML structure (confirmed from The Ridge):
  ```html
  <div class="teetimes-slot bookable:4">
    <a href="?date=09-09-2026&course=1&group=1&book=11:24:00">
    <fieldset><legend><b>11:24</b> Teetime Prices:</legend>
      <div class="priceLine">
        <label class="player">
          <span><input type="radio" name="numslots" value="1"/>
          <span class="players">1 Player</span></span>
          <span class="price">£38.00</span>
        </label>
      </div>
      <!-- repeated priceLine divs for 2, 3, 4 players -->
    </fieldset>
  </div>
  ```
- Same platform appears both on its own subdomain
  (`<club>.intelligentgolf.co.uk`, e.g. Wildernesse) and fully white-labeled
  on the club's own domain with just a footer credit
  ("Powered by intelligentgolf", e.g. The Ridge). Same backend either way —
  one scraper, one config table, URL/course_id differ per club.
- **RESOLVED — 9 vs 18 holes**: there is no server-side parameter. The
  page's toggle is a *client-side* filter over radio inputs named
  `maxholes`; the server returns identical HTML either way, so one request
  already retrieves the full inventory. `holes=9` appears only on an
  individual slot's booking link, marking slots restricted to 9 holes
  (late twilight ones). The scraper reads holes per-slot from that href,
  meaning "longest round bookable at this slot". `HOLES_PARAM` stays None.
- **Also learned**: `group` in the URL is which TEE (1 = 1st, 2 = 10th),
  not a player grouping. And per-player pricing is NOT always linear —
  Wildernesse's four-ball is £480 vs 4 x £125 — so never compute prices
  from the 1-player rate, use the captured per-count values.
- **Booking requires a club account**: the emitted deep link lands on the
  right slot in the club's flow, but the user then hits a register/login
  wall. The tee sheet itself (times + prices) is fully public, no login,
  which is what keeps the scraping side clean.

## Architecture decisions made so far
- **No live scraping on user search** — pre-fetch on a schedule (n8n cron,
  e.g. every 15-30 min for near-term dates) into a database; user searches
  hit the cached database, not the clubs, for speed (sub-second response)
- **Google Sheets is fine for the current recon/config phase but won't
  scale to the live user-facing app** — plan to move to Postgres via
  Supabase (pairs well with Joe's existing Netlify hosting) once building
  the real backend
- **n8n orchestrates, Python/Node does the actual scraping** — n8n's HTTP
  node can't handle real HTML parsing well; scrapers are standalone
  scripts n8n calls out to and collects results from
- **Postcodes.io** (free, no API key) for postcode → lat/long, used both
  for club geocoding and user radius search
- **Model**: pure aggregator/click-through, never handles payment or
  booking directly — user always completes the booking on the club's own
  site. This keeps legal/liability exposure low and avoids Apple/Google
  in-app-purchase rules entirely unless a future in-app "Pro" tier is added
- **BRS specifically** needs a two-tier approach: a real/headless browser
  hop to pass Cloudflare and capture a valid `cf_clearance` cookie, then
  reuse that cookie for fast direct JSON API calls until it expires

## Scraping ethics/legal stance agreed
- Scraping publicly visible visitor tee-time pages (no login required) —
  not the same risk profile as scraping GolfNow itself (much bigger,
  better-resourced, actively defends its data) or scraping behind a login
  wall (not attempting — treated as out of scope for clubs that gate
  visitor booking behind an account)
- Polite scraping: honest user-agent string, request pacing/delays, no
  aggressive concurrent hammering, especially on Chronogolf given its
  reCAPTCHA and larger scale
- GolfNow-only clubs are deliberately excluded — no product reason to
  scrape them (already visible on GolfNow) and highest legal/technical
  risk of any target

## Joe's working preferences (apply throughout)
- Wants direct recommendations, not just neutral option lists
- Wants step-by-step guidance with confirmation before moving forward —
  don't jump ahead multiple steps at once
- Wants reusable internal documentation built alongside every system —
  but only once we've confirmed something actually works, not before
- Runs ServiceSynk (SMS automation/web design for tradespeople) as his
  main business — this golf project is a separate side venture, same
  general tech comfort level (n8n, Netlify, scripting) but different
  domain

## Longer-term ideas discussed (not being built yet)
- Community reviews/ratings as a moat — acknowledged as a cold-start
  problem, sequence after real user traffic exists, not before
- Real moat candidates identified: the aggregation work itself (hard to
  replicate), breadth of coverage, accumulated historical price/demand
  data over time, eventual direct club relationships from sending traffic
- Discovery automation (auto-finding clubs + auto-classifying their
  platform) is a planned future build, explicitly deferred until the
  first few scrapers are proven — not to be built prematurely
- Web app first; mobile/PWA later; App Store in-app-purchase rules only
  matter if a future in-app "Pro" tier gets added, not for the core
  click-through model
