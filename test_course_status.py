"""
Course status: what we keep from a club's notice board, and what we drop.

Every notice in here is real — collected from a live club page, not invented —
because the filter's whole job is to tell the state of the course from the dog
policy, and only real clubs write like real clubs. Add the awkward ones here
when they turn up; the regexes in golf_common are easy to break by accident.

    python test_course_status.py
"""
import sys

sys.path.insert(0, ".")
import golf_common as gc
import brs_scraper as brs
import intelligent_golf_scraper as ig
from bs4 import BeautifulSoup

fails = []


def eq(got, want, what):
    if got != want:
        fails.append(f"{what}\n     got:  {got!r}\n     want: {want!r}")


C = gc.clean_status_note
# The club's own name in front of its own notice is noise on our card.
eq(C("● Effingham.: Good morning. Greens 9, 10, 15, 14 are temporary due to drainage works.",
     "Effingham Golf Club"),
   "Greens 9, 10, 15, 14 are temporary due to drainage works.", "club prefix")
eq(C("● Cooden Beach Golf Course: COURSE OPEN. Preferred lies.", "Cooden Beach"),
   "COURSE OPEN. Preferred lies.", "club prefix, longer form")
# Someone else's name before a colon is part of what they said — keep it.
eq(C("Vixen course: the greens are temporary.", "The Heron"),
   "Vixen course: the greens are temporary.", "non-club prefix kept")
# Empty boxes and bare headings are not a status.
for junk in ("No updates available", "Course Status", "course status:", "N/A", "TBC",
             "Nothing to report.", "", "   ", "●", "Open"):
    eq(C(junk, "Any Golf Club"), "", f"junk {junk!r}")
# A real one survives whatever the theme wraps it in.
eq(C("  Course   Status:  ●  Course open. Buggies on.  ", "Northumberland"),
   "Course open. Buggies on.", "label and bullet stripped")
essay = "The course is open and " + "the greens are running well " * 40
eq(len(C(essay, "X")) <= gc.STATUS_MAX_CHARS + 1, True, "long note truncated")
eq(C(essay, "X").endswith("…"), True, "truncation marked")

# --- Intelligent Golf page shapes -------------------------------------------
themed = """<html><body>
 <div class="status-section"><h3>Course Status</h3>
   <p><span>●</span> Lamberhurst: Course is OPEN. Trolleys permitted.</p></div>
 <div class="teebooking-teetimes">slots here</div></body></html>"""
eq(ig.course_status(BeautifulSoup(themed, "html.parser")),   # raw; cleaning happens on record
   "● Lamberhurst: Course is OPEN. Trolleys permitted.", "themed block")

commented = """<html><body><!-- <div class="courseStatus"><h3>Course Status</h3>
   <p>The course is OPEN - YELLOW flag rules apply.</p></div> -->
   <div class="teebooking-teetimes">slots</div></body></html>"""
eq(ig.course_status(BeautifulSoup(commented, "html.parser")), "", "commented-out block ignored")

bullet_only = """<html><body><div class="header-bg">Wed 23 20C
   <div><p>● Anyclub: Course open, preferred lies.</p></div></div></body></html>"""
eq(ig.course_status(BeautifulSoup(bullet_only, "html.parser")),
   "● Anyclub: Course open, preferred lies.", "bullet fallback takes the inner line")

none_at_all = '<html><body><ul class="nav"><li><a href="#coursestatus">flag</a></li></ul></body></html>'
eq(ig.course_status(BeautifulSoup(none_at_all, "html.parser")), "", "tab link is not a status")

# The tee sheet must be untouched by reading the status out of the same soup.
soup = BeautifulSoup(themed, "html.parser")
ig.course_status(soup)
eq(soup.select_one(".status-section h3").get_text(), "Course Status", "soup not mutated")

# --- BRS dated messages ------------------------------------------------------
data = {"messages": [
    {"start_date": "2026-08-13", "end_date": "2026-09-30", "message": "Smoking prohibition in place."},
    {"start_date": "2026-10-01", "end_date": "2026-10-31", "message": "Winter greens from October."},
    {"start_date": "", "end_date": "", "message": "Check in with the pro shop before play"},
]}
eq(brs.course_messages(data, "2026-09-24"),
   "Smoking prohibition in place. Check in with the pro shop before play", "in-range only")
eq(brs.course_messages(data, "2026-10-05"),
   "Winter greens from October. Check in with the pro shop before play", "later date")
eq(brs.course_messages({}, "2026-09-24"), "", "no messages key")
eq(brs.course_messages({"messages": [{"message": "   "}]}, "2026-09-24"), "", "blank message")

# --- the recording path ------------------------------------------------------
club = gc.ClubConfig(club_name="Test Golf Club", platform="brs",
                     base_url="https://visitors.brsgolf.com/test", course_id="1")
gc.record_status_note(club, "No updates available")
eq(gc.status_note_for(club), "", "junk never stored")
gc.record_status_note(club, "● Test Golf Club: Greens 4 and 7 temporary.")
eq(gc.status_note_for(club), "Greens 4 and 7 temporary.", "stored clean")


# --- what survives the course-only filter ------------------------------------

def check(raw, club, want, why):
    got = gc.clean_status_note(raw, club)
    if got != want:
        fails.append(f"{why}\n     got:  {got!r}\n     want: {want!r}")


# --- kept: the state of the course ------------------------------------------
check("● Effingham.: Good morning. Greens 9, 10, 15, 14 are temporary due to drainage works.",
      "Effingham Golf Club",
      "Greens 9, 10, 15, 14 are temporary due to drainage works.",
      "temporary greens kept, 'Good morning' dropped")

check("● Lamberhurst: Course is OPEN. Trolleys and Buggies permitted.", "Lamberhurst",
      "Course is OPEN. Trolleys and Buggies permitted.", "open + equipment")

check("Course open. Please be aware some tees may be on mats or markers may not be where "
      "you are used to due to ongoing project and renovation works.", "Shirley Park Golf Club",
      "Course open. Please be aware some tees may be on mats or markers may not be where "
      "you are used to due to ongoing project and renovation works.", "Shirley Park, both sentences")

check("Course open. Preferred lies on all closely mown areas. Competitions qualifying.",
      "Dorking Golf Club",
      "Course open. Preferred lies on all closely mown areas. Competitions qualifying.",
      "Dorking, all three")

check("Course open. Preferred Lies are in operation for general and competition play. "
      "Updated: 22nd Aug 2026", "Kingswood Golf & Country Club",
      "Course open. Preferred Lies are in operation for general and competition play. "
      "Updated: 22nd Aug 2026", "club's own updated date rides along")

check("Course Open • No Restrictions - Preferred Lies in play • Halfway House Open "
      "Daily (Except Tuesdays) Updated: 15th Sep 2026", "Nevill Golf Club",
      "Course Open. No Restrictions. Preferred Lies in play. Updated: 15th Sep 2026",
      "bullets split; halfway house dropped, club's date kept")

check("● Cooden Beach Golf Course: AUGUST 2026 COURSE OPEN. HCBR Green Flag. Course is "
      "fully open. Preferred lies in play on holes 1 and 18 due to leather jacket damage. "
      "Week beginning 3rd August is course maintenance week.", "Cooden Beach",
      "AUGUST 2026 COURSE OPEN. Course is fully open. Preferred lies in play on holes 1 and 18 "
      "due to leather jacket damage. Week beginning 3rd August is course maintenance week.",
      "Cooden Beach, flag line dropped")

# --- dropped: house rules, housekeeping, marketing ---------------------------
check("IMPORTANT INFORMATION: Please be advised that non-golfers are strictly NOT ALLOWED "
      "on the course. This includes walkers and observers.", "Surrey Downs Golf Club",
      "", "who may walk on the course is a rule, not a status")

check("Under NO circumstances is anyone allowed to stand or ride on the back of a buggy. "
      "Our Diner serves bacon baps, sandwiches and hot drinks.", "Clandon Golf",
      "", "buggy behaviour rule + the menu")

check("PLEASE BE ADVISED THAT NON GOLFERS ARE STRICTLY NOT PERMITTED ON THE COURSE IE. "
      "WALKERS, OBSERVERS, CHILDREN AND DOGS", "Silvermere Golf Club",
      "", "same rule, shouted")

check("Smoking prohibition in place until further notice. See Course - Course Status / "
      "Opening hours on website for more information.", "Pyecombe Golf Course",
      "", "smoking rule and a pointer to another page")

check("Please note the times the car park closes in the evening. Times are on the post by "
      "the gate and on the notice boards.", "Seaford Head Golf Club",
      "", "car park hours")

check("Please Check in with Pro Shop before play", "North Shore Golf Club",
      "", "arrival instruction")

check("Hawk - Vixen only - Electric Trolleys, Push Trolleys and Buggies permitted - Driving "
      "Range Open - Clubhouse and Padel courts open as usual. Updated: 22nd Sep 2026",
      "The Heron",
      "Electric Trolleys, Push Trolleys and Buggies permitted. Updated: 22nd Sep 2026",
      "dash-separated: equipment kept, clubhouse and padel courts dropped")

check("Course open no restrictions. Preferred lies. Front 9 closed on Monday & back 9 closed "
      "on Tuesday for over-seeding. All 18 holes open from 1pm, though there may still be some "
      "limited disruption. Tee levelling works continue Monday-Friday. Updated: 19th Sep 2026",
      "Chobham Golf Club",
      "Course open no restrictions. Preferred lies. Front 9 closed on Monday & back 9 closed "
      "on Tuesday for over-seeding. All 18 holes open from 1pm, though there may still be some "
      "limited disruption. Tee levelling works continue Monday-Friday. Updated: 19th Sep 2026",
      "nine written as a digit — the whole point of the feature")

check("Course open. No restrictions. Please repair pitchmarks and replace divots. Thank you. "
      "Updated: 23rd Sep 2026", "Tyrrells Wood Golf Course",
      "Course open. No restrictions. Updated: 23rd Sep 2026", "etiquette dropped")

# A club that never uses a full stop runs its status and its dog policy together.
check("Senior rate available if two or more players Monday to Friday please call: 01932 963943 "
      "today the golf course is : OPEN -IMPORTANT: ONLY GOLFERS ARE ALLOWED ON THE COURSE DOGS "
      "ARE NOT ALLOWED - NO REFUND WILL BE GIVEN under 16 years old must be accompanied by an adult",
      "Abbey Moor Golf Club",
      "Senior rate available if two or more players Monday to Friday please call: 01932 963943 "
      "today the golf course is : OPEN.",
      "run-on: the dash splits the rules off the status")

check("Bookings made with less than four players for any one tee time may be paired up at the "
      "club's discretion. Visitors can book 10 days in advance Bar: Opens at 9:00am Monday - "
      "Thursday. Bean to cup coffee machine available all day.", "Hurtmore Golf Club",
      "", "booking policy and the coffee machine")

# --- the empty and the absurd ------------------------------------------------
for junk in ("No updates available", "Course Status", "N/A", "", "●",
             "Our Diner serves bacon baps.", "Welcome to the club!"):
    check(junk, "Any Golf Club", "", f"junk {junk!r}")

print("\n".join(f"FAIL {f}" for f in fails) or "all checks passed")
sys.exit(1 if fails else 0)
