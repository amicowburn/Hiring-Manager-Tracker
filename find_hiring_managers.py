"""
Hiring Manager Tracker
======================

Finds the graduate, early-careers and marketing recruiters at every company
on the MMSS Company_Outreach_Tracker sheet, ranks them for MMSS, and keeps
them in a "Hiring Managers" tab of that same sheet for partnerships to work
from.

Where the people come from
--------------------------
HarvestAPI's `linkedin-company-employees` actor on Apify (switched to on
7 Oct 2026, after two test searches with `memo23/linkedin-people-search`
returned general staff: that actor runs loose web searches and reads
logged-out pages, which hide most titles and employers). HarvestAPI uses
LinkedIn's own people search, filtered by the company's LinkedIn page
(the tracker's LINKEDIN PAGE column), Melbourne/Sydney, LinkedIn's Human
Resources function and job titles. No LinkedIn login of ours is involved.

Free-plan pricing (checked 7 Oct 2026): US$0.003 per profile and US$0.02
per run, and one run covers up to BATCH_SIZE companies. 10 profiles for all
58 companies is about US$1.80 for the first stage.

Only work fields are kept: name, current job title, headline, company,
location and profile URL. Photos and the rest are discarded as soon as
they arrive and never reach the sheet. No emails are requested.

What a run does
---------------
1. Reads the companies from the tracker tab (TRACKER_TAB). The header row is
   found by looking for a COMPANY cell, not by row number. Companies with no
   LINKEDIN PAGE yet are skipped and named in the log.
2. Works out which companies are due: never searched, or last searched
   successfully more than RESEARCH_INTERVAL_MONTHS ago. Like the alumni
   script, due-ness is derived from the Search Log, never stored in a
   column someone could delete.
3. In batches of BATCH_SIZE companies, while the month's budget allows:
   - Stage 1 asks LinkedIn for early-careers titles only (EARLY_TITLES).
   - Stage 2 runs only for companies with no Strong match yet, asking for
     general talent-acquisition titles (FALLBACK_TITLES).
   Each run is logged at its expected cost BEFORE it starts and corrected
   after, so a crash never leaves a spend unrecorded. Then each company's
   best one or two are merged into the Hiring Managers tab and the company
   gets its own Search Log row (cost 0; the cost sits on the run row).

Merging never touches the human columns: SHORTLIST and NOTES are only ever
written by people. A person already in the tab gets their title, headline,
location, match, reason and LAST SEEN refreshed; new people are appended.

Ranking (agreed 7 Oct 2026: quality over quantity)
--------------------------------------------------
Only the one or two best people per company are written (CONTACTS_PER_COMPANY):
- Strong match: confirmed at the company, based in Melbourne or Sydney
  (CITIES), and runs early-careers hiring, ideally for a marketing, sales,
  communications or consulting team. Team-specific beats general, and
  Melbourne beats Sydney.
- Possible match: a near miss worth checking by hand (employer or location
  hidden, graduate recruiting only for audit/tech/finance, or a target-team
  recruiter who isn't early-careers). Written only when a company has no
  Strong match, and then just the best one.
- Not suitable: never written. That covers graduates and interns
  themselves, "talent" managers who look after creators, people who have
  left, and anyone outside Melbourne and Sydney.
The Search Log's KEPT column names who was written and why the rest weren't.
Run with LOG_LEVEL=DEBUG to see every candidate and their reason.

Usage
-----
    python find_hiring_managers.py --check-keys         # free: Apify key + sheet access
    python find_hiring_managers.py --dry-run            # free: what would run, and the cost
    python find_hiring_managers.py                       # the real run
    python find_hiring_managers.py --company "Canva,Deloitte"   # just these, due or not
    python find_hiring_managers.py --export-shortlist shortlist.csv [--company Canva]
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Iterable

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("hiring_manager_tracker")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    # Strip whitespace: the alumni project lost a month to a key pasted with
    # a trailing space.
    return (os.environ.get(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _list(name: str, default: str) -> list:
    return [x.strip() for x in _env(name, default).split(";") if x.strip()]


DEFAULT_EARLY_TITLES = "Early Careers;Graduate;Campus;Emerging Talent;Talent Programs;Intern;University"
DEFAULT_FALLBACK_TITLES = "Talent Acquisition;Recruitment;Recruiter;Talent Attraction"
# SEARCH AS = Marketing team: where marketing, not HR, runs student and graduate hiring (e.g. Red Bull).
DEFAULT_MARKETING_TITLES = "Student Marketing;Brand Manager;Marketing Manager;Head of Marketing;Marketing Director"
# SEARCH AS = Leadership: small firms with no recruiter, where a founder, director or head of people hires.
DEFAULT_LEADERSHIP_TITLES = ("Founder;Co-Founder;Managing Director;General Manager;Head of People;People & Culture;"
                             "Director")


@dataclass
class Config:
    sheet_id: str = field(default_factory=lambda: _env("GOOGLE_SHEET_ID"))
    credentials_file: str = field(default_factory=lambda: _env("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json"))
    tracker_tab: str = field(default_factory=lambda: _env("TRACKER_TAB", "Sheet10"))
    results_tab: str = field(default_factory=lambda: _env("RESULTS_TAB", "Hiring Managers"))
    log_tab: str = field(default_factory=lambda: _env("LOG_TAB", "Search Log"))
    apify_token: str = field(default_factory=lambda: _env("APIFY_TOKEN"))
    monthly_budget_usd: float = field(default_factory=lambda: _env_float("APIFY_MONTHLY_BUDGET_USD", 4.50))
    # Apify free plan (chosen 7 Oct 2026). HarvestAPI then allows only
    # FREE_RUNS_PER_MONTH runs and 25 profiles per run, so searches are shaped
    # to fit: PROFILES_PER_COMPANY x companies-per-run <= 25.
    free_plan: bool = field(default_factory=lambda: _env("FREE_PLAN", "true").lower() not in ("false", "0", "no"))
    free_runs_per_month: int = field(default_factory=lambda: _env_int("FREE_RUNS_PER_MONTH", 10))
    profiles_per_company: int = field(default_factory=lambda: _env_int("PROFILES_PER_COMPANY", 3))
    research_interval_months: int = field(default_factory=lambda: _env_int("RESEARCH_INTERVAL_MONTHS", 3))
    # Only people based in these cities can be a Strong match; the first is preferred.
    cities: list = field(default_factory=lambda: [c.strip() for c in _env("CITIES", "Melbourne;Sydney").split(";") if c.strip()])
    # Stage 1 job titles: early-careers hiring. LinkedIn matches any of them.
    early_titles: list = field(default_factory=lambda: _list("EARLY_TITLES", DEFAULT_EARLY_TITLES))
    # Stage 2, only for companies stage 1 found no Strong match at. Blank: no stage 2.
    fallback_titles: list = field(default_factory=lambda: _list("FALLBACK_TITLES", DEFAULT_FALLBACK_TITLES))
    marketing_titles: list = field(default_factory=lambda: _list("MARKETING_TITLES", DEFAULT_MARKETING_TITLES))
    leadership_titles: list = field(default_factory=lambda: _list("LEADERSHIP_TITLES", DEFAULT_LEADERSHIP_TITLES))
    # Companies per Apify run. On the free plan it is capped so a run stays within 25 profiles.
    batch_size: int = field(default_factory=lambda: _env_int("BATCH_SIZE", 20))

    @property
    def companies_per_run(self) -> int:
        if self.free_plan:
            return max(1, min(self.batch_size, ApifyClient.FREE_ITEMS_PER_RUN // max(1, self.profiles_per_company)))
        return self.batch_size
    # How many people per company are written to the sheet (best first).
    contacts_per_company: int = field(default_factory=lambda: _env_int("CONTACTS_PER_COMPANY", 2))
    run_timeout_secs: int = field(default_factory=lambda: _env_int("RUN_TIMEOUT_SECS", 900))


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------
#
# Quality over quantity (agreed 7 Oct 2026): partnerships wants the one or
# two people at each company who hire early-career staff into the teams
# marketing students go into, based in Melbourne or Sydney, not every
# recruiter on LinkedIn. So the rules are strict, and only the best one or
# two are written to the sheet (pick_best).

STRONG, POSSIBLE, UNSUITABLE = "Strong match", "Possible match", "Not suitable"

# Teams that hire marketing students. A recruiter whose title or headline
# names one of these ranks above a general early-careers recruiter.
TEAMS = {
    "marketing": ["marketing", "brand", "digital", "media", "advertising", "growth", "creative"],
    "sales": ["sales", "commercial", "business development", "account management", "category", "trade marketing"],
    "communications": ["communications", "comms", "corporate affairs", "public relations", "pr", "publications",
                       "publishing", "editorial", "content"],
    # Not "consultant": "Talent Acquisition Consultant" is a recruiter's job title, not a consulting team.
    "consulting": ["consulting", "advisory", "strategy", "transformation", "people consulting", "customer",
                   "insights", "research"],
}
ANY_TEAM = [t for terms in TEAMS.values() for t in terms]

# The tracker's INDUSTRY column -> the team that company's hires most often
# sit in. Adds a point; never a filter.
INDUSTRY_TEAM = {
    "consulting": "consulting",
    "market research": "consulting",
    "marketing research": "consulting",
    "public relations": "communications",
    "advertising agency": "marketing",
    "production agency": "marketing",
    "media": "marketing",
    "fmcg": "marketing",
}

# Someone who runs graduate/intern/campus hiring. Deliberately phrases, not
# single words: "graduate" alone describes graduates themselves.
EARLY_PROGRAM = [
    "early careers", "early career", "early talent", "emerging talent", "future talent", "campus",
    "university relations", "university recruitment", "university recruiting", "graduate program",
    "graduate programs", "graduate programme", "graduate attraction", "student attraction", "talent programs",
    "talent program", "intern program", "internship program", "vacation program", "entry level recruitment",
]
EARLY_WORDS = ["graduate", "graduates", "grad", "grads", "intern", "interns", "internship", "internships",
               "student", "students", "university", "entry level", "cadet", "trainee", "vacation"]
# Someone who recruits. Bare "talent" is left out on purpose: at TikTok, Nine
# or an agency a "Talent Manager" looks after creators, not hiring.
RECRUIT = [
    "recruit", "recruiter", "recruiters", "recruiting", "recruitment", "talent acquisition", "talent attraction",
    "talent aquisition", "talent acqusition",  # misspellings seen on real titles (Deloitte, 7 Oct 2026)
    "talent partner", "talent advisor", "talent sourcing", "talent sourcer", "sourcer", "resourcing",
    "people & culture", "people and culture", "p&c", "human resources", "hr", "employer brand",
    "people partner", "people experience",
]
# Recruiting proper, for the general-recruiter fallback: not HR business partners or P&C generalists.
GENERAL_RECRUIT = ["recruit", "recruiter", "recruiters", "recruiting", "recruitment", "talent acquisition",
                   "talent attraction", "talent partner", "talent advisor", "talent aquisition", "talent acqusition",
                   "sourcer", "resourcing"]

# Running a graduate program's learning and development is not hiring.
LEARNING = ["learning", "l&d", "learning and development", "capability", "academy"]
UNRELATED = {
    "audit and tax": ["audit", "assurance", "tax"],
    "technology": ["engineering", "engineer", "software", "technology", "tech", "it", "developer", "cyber", "data"],
    "retail operations": ["retail operations", "store", "stores", "frontline", "warehouse", "supply chain",
                          "logistics", "distribution", "hourly", "volume"],
    "finance and risk": ["finance", "financial", "risk", "actuarial", "accounting", "banking", "markets"],
    "legal": ["legal", "law", "lawyer"],
    "clinical": ["clinical", "nursing", "medical"],
    "executive search": ["executive search", "executive recruitment", "leadership hiring"],
}
CITY_ALIASES = {
    "melbourne": ["melbourne", "vic", "victoria"],
    "sydney": ["sydney", "nsw", "new south wales"],
}
AUSTRALIA = re.compile(
    r"\b(australia|nsw|sydney|brisbane|perth|adelaide|qld|canberra|act|hobart|tasmania|darwin|gold coast"
    r"|new south wales|queensland|western australia|south australia|victoria|vic|melbourne|geelong)\b"
)

# Company names people write differently from the tracker (keys are norm()ed).
COMPANY_ALIASES = {
    "commbank": ["commonwealth bank", "cba", "commonwealth bank of australia"],
    "p&g": ["procter & gamble", "procter and gamble"],
    "ey": ["ernst & young", "ernst and young", "ey oceania"],
    "anz": ["australia and new zealand banking group"],
    "accenture": ["accenture song", "fiftyfive5"],
    "mars": ["mars wrigley", "effem", "mars incorporated", "mars petcare", "mars food"],
    "coles group": ["coles", "coles supermarkets"],
    "tiktok": ["bytedance"],
    "nine": ["nine entertainment", "nine network", "nine entertainment co"],
    "thinkerbell agency": ["thinkerbell"],
    "kraft heinz": ["the kraft heinz company"],
    "loreal": ["loreal groupe", "loreal australia", "loreal anz"],
    "nestle": ["nestle australia", "nestle oceania"],
    "bosch": ["robert bosch", "bosch australia"],
    # VidaCorp renamed itself Axia Beauty (DBG Health's beauty division), 2026.
    "vidacorp": ["axia beauty", "axia beauty group", "axia"],
    "axia beauty": ["vidacorp", "axia beauty group", "axia"],
    "mondelez": ["mondelez international", "mondelez australia"],
    "kpmg": ["kpmg australia"],
    "deloitte": ["deloitte australia"],
    "news corp australia": ["news corp", "news corp australia"],
    "oohmedia": ["ooh media", "oohmedia"],
}


def norm(s: object) -> str:
    text = unicodedata.normalize("NFD", str(s or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[’'!]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _words(s: object) -> str:
    """Space-padded word string, so _has matches whole words and phrases only."""
    return " " + re.sub(r"[^a-z0-9&]+", " ", norm(s)).strip() + " "


def _has(hay: str, term: str) -> bool:
    return " " + re.sub(r"[^a-z0-9&]+", " ", norm(term)).strip() + " " in hay


def _hits(hay: str, terms: Iterable[str]) -> list:
    return [t for t in terms if _has(hay, t)]


def company_names(query: str) -> list:
    n = norm(query)
    if not n:
        return []
    out = [n]
    for key, aliases in COMPANY_ALIASES.items():
        if key == n or n in aliases:
            out += [key, *aliases]
    return list(dict.fromkeys(out))


def company_matches(person_company: str | None, query: str) -> bool:
    names = company_names(query)
    if not names:
        return True
    cc = _words(person_company)
    return any(_has(cc, n) or norm(person_company) == n for n in names)


def location_info(loc: str | None, cities: list) -> tuple:
    """(kind, city, text): kind is target / aus / overseas / unknown; city is the matched target city."""
    words = _words(loc)
    first = str(loc or "").split(",")[0].strip()
    if not norm(loc):
        return "unknown", "", "Location not shown"
    for city in cities:
        if any(_has(words, a) for a in CITY_ALIASES.get(norm(city), [norm(city)])):
            return "target", city, f"Based in {city}"
    places = " or ".join(cities)
    if AUSTRALIA.search(norm(loc)):
        return "aus", "", f"Based in {first}, not {places}"
    return "overseas", "", f"Based overseas ({first}), not {places}"


HEADLINE_EMPLOYER = re.compile(r"(?:\bat|@)\s+([^|·,–—(]+)", re.I)


def headline_employer(text: str | None) -> str | None:
    """'Campus Recruiter at Google' -> 'Google'. LinkedIn often hides the employer field but not the headline."""
    m = HEADLINE_EMPLOYER.search(text or "")
    name = m.group(1).strip(" .-") if m else ""
    return name if re.search(r"[A-Za-z]{2}", name) else None


def _early_phrase(found: list) -> str:
    s = " ".join(found)
    parts = []
    if "grad" in s:
        parts.append("graduates")
    if re.search(r"intern|vacation|cadet|trainee", s):
        parts.append("interns")
    if re.search(r"campus|universit|student", s):
        parts.append("on campus")
    if not parts:
        parts.append("early-career talent")
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


@dataclass
class Assessment:
    match: str
    score: int
    reason: str


def assess(person: "Person", company: str, cities: list, industry_team: str = "") -> Assessment:
    """
    Strong match: confirmed at `company`, based in one of `cities`, and runs
    early-careers hiring, either for a marketing/sales/communications/
    consulting team or generally (not only for audit, tech, finance etc.).

    Possible match: a near miss worth a human look: employer or location not
    shown publicly, an early-careers recruiter who only names unrelated
    teams, or a general recruiter for one of the target teams.

    Not suitable: everything else, including graduates and interns
    themselves, people who have left, and anyone outside Australia's two
    target cities.
    """
    # The job title when LinkedIn gives one; the headline/summary only stands in
    # without one, because "about" text mentions graduates for other reasons.
    role_text = person.title or person.headline or ""
    hay = _words(role_text)
    program = _hits(hay, EARLY_PROGRAM)
    early_words = _hits(hay, EARLY_WORDS)
    recruit = _hits(hay, RECRUIT)
    teams = [team for team, terms in TEAMS.items() if _hits(hay, terms)]
    unrelated = [area for area, terms in UNRELATED.items() if _hits(hay, terms)]
    loc_kind, city, loc_text = location_info(person.location, cities)
    employer = person.company or headline_employer(person.title) or headline_employer(person.headline)
    company_known = bool(person.company)
    wrong_company = bool(company and employer and not company_matches(employer, company))

    learning_only = bool(_hits(hay, LEARNING)) and not recruit
    # "Graduate" only signals early-careers hiring next to a real recruiting
    # word: "Human Resources Graduate" (Westpac, 7 Oct 2026) is a graduate.
    recruits_proper = bool(_hits(hay, GENERAL_RECRUIT))
    graduate_themselves = bool(early_words) and not program and not recruits_proper
    is_recruiter = bool(program or recruit) and not learning_only and not graduate_themselves
    is_early = bool(program or (recruits_proper and early_words)) and not learning_only

    def out(match, score, reasons):
        reason = "; ".join(r for r in reasons if r)
        rank = {STRONG: 2, POSSIBLE: 1, UNSUITABLE: 0}[match]
        return Assessment(match, rank * 100 + score, reason[:1].upper() + reason[1:] + "." if reason else "")

    if wrong_company:
        return out(UNSUITABLE, 0, [f"Works at {employer} now, not {company}"])
    if not role_text:
        return out(UNSUITABLE, 0, ["No job title or headline shown publicly"])
    if learning_only and program:
        return out(POSSIBLE, 0, ["Runs an early-careers program (learning and development), not hiring",
                                 loc_text if loc_kind != "unknown" else ""])
    if not is_recruiter:
        if early_words:
            if _hits(hay, ["consultant", "advisor", "manager", "specialist", "lead", "partner", "coordinator",
                           "experience", "reward", "learning", "program"]):
                return out(UNSUITABLE, 0, ["Works with graduates, not recruiting them"])
            return out(UNSUITABLE, 0, ["A graduate or intern themselves, not a recruiter"])
        return out(UNSUITABLE, 0, ["Not a recruiting role"])
    if loc_kind in ("aus", "overseas"):
        return out(UNSUITABLE, 0, [loc_text])

    score = 0
    reasons = []
    if is_early:
        score += 4
        reasons.append(f"Runs early-careers hiring ({_early_phrase(program + early_words)})")
    else:
        score += 1
        reasons.append("Recruiter, not early-careers specific")
    if teams:
        score += 3 + (1 if industry_team in teams else 0)
        reasons.append(f"for {' and '.join(teams)}")
    only_unrelated = bool(unrelated) and not teams
    if only_unrelated:
        score -= 3
        reasons.append(f"but only names {' and '.join(unrelated)}")
    if loc_kind == "target":
        score += 3 + (1 if city == cities[0] else 0)
    reasons.append(loc_text)
    if not company_known:
        reasons.append("current employer not shown publicly, check before contacting")
    if person.partial:
        reasons.append("profile only partly visible")

    strong = is_early and loc_kind == "target" and company_known and not only_unrelated and not person.partial
    if strong:
        return out(STRONG, score, reasons)
    if is_early or teams:
        return out(POSSIBLE, score, reasons)
    # A company with no early-careers or target-team recruiter still gets its
    # best general recruiter (e.g. KPMG's Head of Talent Attraction), as the
    # one fallback pick_best allows, ranked below every other Possible.
    if (_hits(hay, GENERAL_RECRUIT) and not only_unrelated and loc_kind == "target" and company_known
            and not person.partial):
        senior = 1 if _hits(hay, ["head", "director", "manager", "lead", "principal"]) else 0
        return out(POSSIBLE, senior + (1 if city == cities[0] else 0),
                   ["General recruiter; ask them for the early-careers contact"] + reasons[1:])
    return out(UNSUITABLE, score, ["General recruiter with no early-careers or target-team focus"] + reasons[1:])


RECRUITER, MARKETING_TEAM, LEADERSHIP = "Recruiter", "Marketing team", "Leadership"
SEARCH_MODES = {norm(x): x for x in (RECRUITER, MARKETING_TEAM, LEADERSHIP)}

SENIOR = ["manager", "head", "director", "lead", "leader", "senior manager", "gm", "general manager"]
MARKETING_ROLE = ["marketing", "brand", "student marketing", "growth", "communications", "pr", "public relations"]
STUDENT = ["student", "students", "student marketing", "university", "campus", "graduate", "graduates", "intern",
           "interns", "early careers"]
LEADER_TOP = ["head of people", "people & culture", "people and culture", "talent", "hr", "human resources"]
LEADER_OWNER = ["founder", "co founder", "cofounder", "managing director", "md", "ceo", "chief executive",
                "general manager", "owner", "managing partner"]
JUNIOR = ["coordinator", "assistant", "executive", "associate", "intern", "graduate", "trainee", "junior"]


def assess_manager(person: "Person", company: str, cities: list, mode: str) -> Assessment:
    """
    For SEARCH AS = Marketing team or Leadership: the person who hires
    students without being a recruiter.

    Marketing team -- Strong: a manager-level marketing/brand person in
    Melbourne or Sydney; one whose title names students (Student Marketing
    Manager) ranks first. Possible: marketing but not manager-level.
    Leadership -- Strong: head of people/P&C, founder, MD or GM in Melbourne
    or Sydney (people leads rank first). Possible: another director (account,
    client, creative), who often hires juniors at an agency.
    Graduates, interns and people who left are Not suitable, as everywhere.
    """
    role_text = person.title or person.headline or ""
    hay = _words(role_text)
    loc_kind, city, loc_text = location_info(person.location, cities)
    employer = person.company or headline_employer(person.title) or headline_employer(person.headline)

    def out(match, score, reasons):
        reason = "; ".join(r for r in reasons if r)
        rank = {STRONG: 2, POSSIBLE: 1, UNSUITABLE: 0}[match]
        return Assessment(match, rank * 100 + score, reason[:1].upper() + reason[1:] + "." if reason else "")

    if company and employer and not company_matches(employer, company):
        return out(UNSUITABLE, 0, [f"Works at {employer} now, not {company}"])
    if not role_text:
        return out(UNSUITABLE, 0, ["No job title or headline shown publicly"])
    junior = _hits(hay, JUNIOR) and not _hits(hay, SENIOR)
    if junior and _hits(hay, ["graduate", "intern", "trainee"]):
        return out(UNSUITABLE, 0, ["A graduate or intern themselves"])
    if loc_kind in ("aus", "overseas"):
        return out(UNSUITABLE, 0, [loc_text])
    city_bonus = (3 + (1 if city == cities[0] else 0)) if loc_kind == "target" else 0
    caveats = [loc_text] + ([] if person.company else ["current employer not shown publicly, check before contacting"])
    confirmed = loc_kind == "target" and bool(person.company)

    if mode == MARKETING_TEAM:
        if not _hits(hay, MARKETING_ROLE):
            return out(UNSUITABLE, 0, ["Not in the marketing team"])
        students = bool(_hits(hay, STUDENT))
        senior = bool(_hits(hay, SENIOR))
        score = city_bonus + (5 if students else 0) + (2 if senior else 0)
        what = "Runs student marketing" if students else ("Marketing/brand manager" if senior else "Marketing, not manager-level")
        if (senior or students) and confirmed:
            return out(STRONG, score, [what] + caveats)
        return out(POSSIBLE, score, [what] + caveats)

    people_lead = bool(_hits(hay, LEADER_TOP))
    owner = bool(_hits(hay, LEADER_OWNER))
    director = _has(hay, "director") and not owner
    if people_lead:
        score, what = 6, "Leads people and hiring"
    elif owner:
        score, what = 4, "Founder or managing director"
    elif director:
        score, what = 1, "Director; often hires juniors at an agency"
    else:
        return out(UNSUITABLE, 0, ["Not a leadership or people role"])
    if (people_lead or owner) and confirmed:
        return out(STRONG, score + city_bonus, [what] + caveats)
    return out(POSSIBLE, score + city_bonus, [what] + caveats)


def pick_best(ranked: list, limit: int) -> list:
    """
    The one or two people worth contacting: the top `limit` Strong matches,
    or, when a company has none, its single best Possible match so the
    company isn't left blank. Not suitable people are never written.
    """
    strong = [pa for pa in ranked if pa[1].match == STRONG]
    if strong:
        return strong[:limit]
    possible = [pa for pa in ranked if pa[1].match == POSSIBLE]
    return possible[:1]


# --------------------------------------------------------------------------
# People from Apify
# --------------------------------------------------------------------------


@dataclass
class Person:
    name: str
    title: str | None = None
    headline: str | None = None
    company: str | None = None
    location: str | None = None
    profile_url: str | None = None
    partial: bool = False


LINKEDIN_PROFILE = re.compile(r"^https://([a-z]{2,3}\.)?linkedin\.com/in/[^\s\"<>]+$", re.I)
LINKEDIN_COMPANY = re.compile(r"^https://([a-z]{2,3}\.)?linkedin\.com/company/[^\s\"<>/?#]+/?$", re.I)


def safe_linkedin_url(url: str | None) -> str | None:
    u = (url or "").strip()
    return u if LINKEDIN_PROFILE.match(u) else None


def company_page(url: str | None) -> str | None:
    """A tracker LINKEDIN PAGE cell as https://www.linkedin.com/company/<slug>, or None if it isn't one."""
    u = (url or "").strip()
    if u and not u.startswith("http"):
        u = "https://" + u
    u = re.sub(r"^https?://([a-z]{2,3}\.)?linkedin\.com", "https://www.linkedin.com", u.split("?")[0], flags=re.I)
    return u.rstrip("/") if LINKEDIN_COMPANY.match(u) else None


def person_key(name: str, company: str | None, profile_url: str | None) -> str:
    """Identity for de-duplication: the profile URL when there is one, else name + company."""
    if profile_url:
        k = re.sub(r"^https?://(www\.|[a-z]{2}\.)?", "", norm(profile_url))
        return re.sub(r"[?#].*$", "", k).rstrip("/")
    return f"{norm(name)}|{norm(company)}"


def _clean(v: object) -> str | None:
    s = re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else ""
    return s or None


def normalise_items(items: list) -> dict:
    """
    HarvestAPI rows -> {company page URL: [Person]}, keeping only work fields
    (photos, "About" bios, premium flags and the like are dropped here). The page each row
    came from is in its _meta.query, which is how a batched run is split
    back into companies.
    """
    out: dict = {}
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = _clean(" ".join(x for x in (it.get("firstName"), it.get("lastName")) if isinstance(x, str)))
        if not name:
            continue
        pages = ((it.get("_meta") or {}).get("query") or {}).get("currentCompanies") or [""]
        page = company_page(pages[0]) or ""
        positions = [p for p in (it.get("currentPositions") or []) if isinstance(p, dict)]
        pos = next((p for p in positions if p.get("current") is not False), positions[0] if positions else {})
        loc = it.get("location")
        p = Person(
            name=name,
            title=_clean(pos.get("title")),
            # Not it["summary"]: for this actor that is the person's "About" bio, which
            # MMSS has no use for and shouldn't hold.
            headline=None,
            company=_clean(pos.get("companyName")),
            location=_clean(loc.get("linkedinText") if isinstance(loc, dict) else loc),
            profile_url=safe_linkedin_url(_clean(it.get("linkedinUrl"))),
        )
        key = (page, person_key(p.name, p.company, p.profile_url))
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(page, []).append(p)
    return out


class ApifyError(Exception):
    pass


class ApifyClient:
    """
    HarvestAPI's `linkedin-company-employees` actor: LinkedIn's own people
    search, filtered by current company page, location, function and job
    title. No LinkedIn login of ours is involved.

    Prices on Apify's free plan (checked 7 Oct 2026): US$0.003 per short
    profile, and US$0.02 "actor start" charged PER COMPANY searched (one
    LinkedIn query each in one_by_one mode -- seen on the first full run:
    15 start events for one run). The actor sizes its stop against the
    full-profile price (US$0.008), so runs are started with a cap computed
    at that price or they stop early ("max charge reached").
    """

    API = "https://api.apify.com/v2"
    ACTOR = "harvestapi~linkedin-company-employees"
    PRICE_PER_PROFILE = 0.003
    PRICE_RESERVED_PER_PROFILE = 0.008
    PRICE_PER_RUN = 0.02
    HR_FUNCTION_ID = "12"  # LinkedIn's "Human Resources" function
    FREE_ITEMS_PER_RUN = 25
    REFUSED = "free user run limit exceeded"  # the run's statusMessage once the free runs are used
    MARKETING_FUNCTION_IDS = ["15", "16"]  # LinkedIn's "Marketing" and "Media and Communication"

    def __init__(self, token: str, session: requests.Session | None = None, sleep: Callable = time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.sleep = sleep

    @classmethod
    def expected_cost(cls, companies: int, per_company: int) -> float:
        """What a run can really cost; the budget is counted at this."""
        return round(companies * (per_company * cls.PRICE_PER_PROFILE + cls.PRICE_PER_RUN), 4)

    @classmethod
    def apify_cap(cls, companies: int, per_company: int) -> float:
        return round(max(0.05, companies * (per_company * cls.PRICE_RESERVED_PER_PROFILE + cls.PRICE_PER_RUN)), 4)

    @classmethod
    def run_cost(cls, run: dict, raw_items: list, companies: int = 1) -> float:
        """
        What a finished run cost: the highest of Apify's usage figure (which
        can lag at finish), the charged-event counts, and what was delivered.
        """
        usage = run.get("usageTotalUsd")
        usage = float(usage) if isinstance(usage, (int, float)) else 0.0
        events = run.get("chargedEventCounts") or {}
        from_events = (events.get("actor-start", 0) * cls.PRICE_PER_RUN
                       + events.get("short-profile", 0) * cls.PRICE_PER_PROFILE
                       + events.get("full-profile", 0) * cls.PRICE_RESERVED_PER_PROFILE)
        delivered = sum(1 for it in raw_items if isinstance(it, dict))
        from_items = companies * cls.PRICE_PER_RUN + delivered * cls.PRICE_PER_PROFILE
        return round(max(usage, from_events, from_items), 4)

    @classmethod
    def actor_input(cls, pages: list, titles: list, cities: list, per_company: int,
                    function_ids: list | None = None) -> dict:
        """function_ids None means LinkedIn's Human Resources function; [] means no function filter."""
        inp = {
            "companies": pages,
            "companyBatchMode": "one_by_one",
            "maxItemsPerCompany": per_company,
            "maxItems": per_company * len(pages),
            "locations": cities,
            "functionIds": [cls.HR_FUNCTION_ID] if function_ids is None else function_ids,
            "jobTitles": titles,
            # Short: name, current position, location, profile URL. Never the email-search mode.
            "profileScraperMode": "Short ($4 per 1k)",
        }
        if not inp["functionIds"]:
            del inp["functionIds"]
        return inp

    def _request(self, method: str, path: str, **kw) -> dict | list:
        for attempt in range(4):
            try:
                res = self.session.request(method, self.API + path, timeout=90, **kw)
            except requests.RequestException as e:
                if attempt == 3:
                    raise ApifyError(f"Apify unreachable: {e}") from e
                self.sleep(5 * (attempt + 1))
                continue
            if res.status_code == 429 or res.status_code >= 500:
                if attempt == 3:
                    raise ApifyError(f"Apify {res.status_code} after retries")
                self.sleep(5 * (attempt + 1))
                continue
            if res.status_code == 401:
                raise ApifyError("Apify rejected the token (401). Check APIFY_TOKEN.")
            if not res.ok:
                try:
                    msg = res.json().get("error", {}).get("message")
                except ValueError:
                    msg = None
                raise ApifyError(f"Apify {res.status_code}: {msg or res.text[:200]}")
            return res.json()
        raise ApifyError("unreachable")  # pragma: no cover

    def check(self) -> str:
        """Free call: who the token belongs to."""
        data = self._request("GET", "/users/me")["data"]
        return data.get("username") or data.get("id") or "unknown"

    def start(self, actor_input: dict, cap_usd: float) -> dict:
        return self._request("POST", f"/acts/{self.ACTOR}/runs?maxTotalChargeUsd={cap_usd}", json=actor_input)["data"]

    def wait(self, run_id: str, timeout_secs: int) -> dict:
        deadline = time.monotonic() + timeout_secs
        while True:
            run = self._request("GET", f"/actor-runs/{run_id}?waitForFinish=60")["data"]
            if run.get("status") not in ("READY", "RUNNING"):
                return run
            if time.monotonic() > deadline:
                self._request("POST", f"/actor-runs/{run_id}/abort")
                raise ApifyError(f"run {run_id} still running after {timeout_secs}s; aborted")

    def cycle_start(self, now: datetime | None = None) -> str:
        """
        Start of the account's current Apify usage cycle (ISO, UTC). Apify's free
        allowance runs from the account's signup day, not the calendar month: the
        4th to the 3rd for this account (seen 8 Oct 2026). Falls back to the first
        of the calendar month if Apify doesn't say.
        """
        now = now or datetime.now(timezone.utc)
        try:
            start = self._request("GET", "/users/me/limits")["data"]["monthlyUsageCycle"]["startAt"]
            if isinstance(start, str) and start[:4].isdigit():
                return start
        except (ApifyError, KeyError, TypeError):
            pass
        return f"{now.year:04d}-{now.month:02d}-01"

    def runs_this_month(self, now: datetime | None = None, start: str | None = None) -> int:
        """Runs of this actor in the current usage cycle that HarvestAPI counts: every run it didn't refuse."""
        start = start or self.cycle_start(now)
        used, offset = 0, 0
        while True:
            page = self._request("GET", f"/acts/{self.ACTOR}/runs?desc=1&limit=100&offset={offset}")["data"]
            items = page.get("items") or []
            for r in items:
                if (r.get("startedAt") or "") < start:
                    return used
                full = self._request("GET", f"/actor-runs/{r['id']}")["data"]
                if self.REFUSED not in (full.get("statusMessage") or ""):
                    used += 1
            if len(items) < 100:
                return used
            offset += 100

    def refresh(self, run_id: str) -> dict | None:
        """The finished run again, a few seconds later, for its settled charge counts."""
        self.sleep(5)
        try:
            return self._request("GET", f"/actor-runs/{run_id}")["data"]
        except ApifyError:
            return None

    def items(self, dataset_id: str) -> list:
        return self._request("GET", f"/datasets/{dataset_id}/items?clean=true&format=json")


# --------------------------------------------------------------------------
# The sheet
# --------------------------------------------------------------------------

RESULT_HEADERS = [
    "COMPANY", "FULL NAME", "JOB TITLE", "HEADLINE", "LOCATION", "LINKEDIN", "MATCH", "WHY",
    "SHORTLIST", "NOTES", "FIRST FOUND", "LAST SEEN",
]
# KEPT came after the first version, so it sits after ERROR rather than beside PROFILES.
LOG_HEADERS = ["SEARCHED AT", "COMPANY", "STATUS", "PROFILES", "COST USD", "RUN ID", "ERROR", "KEPT"]
PLACEHOLDER_COMPANIES = {"insert company here"}


@dataclass
class Company:
    name: str
    industry: str = ""
    # The company's LinkedIn page, from the tracker's LINKEDIN PAGE column.
    page: str = ""
    # The tracker's SEARCH AS column: Recruiter (default), Marketing team or Leadership.
    mode: str = RECRUITER

    @property
    def team(self) -> str:
        return INDUSTRY_TEAM.get(norm(self.industry), "")


def read_companies(rows: list) -> list:
    """Companies under the tracker's COMPANY header, wherever that row is."""
    for r, row in enumerate(rows):
        cells = [norm(c) for c in row]
        if "company" in cells:
            ci = cells.index("company")
            ii = cells.index("industry") if "industry" in cells else None
            pi = cells.index("linkedin page") if "linkedin page" in cells else None
            si = cells.index("search as") if "search as" in cells else None
            out, seen = [], set()
            for data in rows[r + 1:]:
                name = (data[ci] if ci < len(data) else "").strip()
                if not name or norm(name) in PLACEHOLDER_COMPANIES or norm(name) in seen:
                    continue
                seen.add(norm(name))
                industry = (data[ii] if ii is not None and ii < len(data) else "").strip()
                page = company_page(data[pi] if pi is not None and pi < len(data) else "") or ""
                mode = SEARCH_MODES.get(norm(data[si] if si is not None and si < len(data) else ""), RECRUITER)
                out.append(Company(name, industry, page, mode))
            return out
    raise ValueError("No COMPANY header found in the tracker tab")


def _parse_date(s: str) -> date | None:
    """The date part of a 'YYYY-MM-DD[ HH:MM]' log timestamp."""
    try:
        return datetime.strptime((s or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


@dataclass
class LogRow:
    searched_at: str
    company: str
    status: str
    profiles: str = ""
    cost_usd: str = ""
    run_id: str = ""
    error: str = ""


def read_log(rows: list) -> list:
    if not rows:
        return []
    head = [norm(h) for h in rows[0]]
    idx = {h: head.index(norm(h)) if norm(h) in head else None for h in LOG_HEADERS}

    def cell(row, h):
        i = idx[h]
        return row[i].strip() if i is not None and i < len(row) else ""

    return [
        LogRow(cell(r, "SEARCHED AT"), cell(r, "COMPANY"), cell(r, "STATUS"), cell(r, "PROFILES"),
               cell(r, "COST USD"), cell(r, "RUN ID"), cell(r, "ERROR"))
        for r in rows[1:] if any(c.strip() for c in r)
    ]


def last_success(log_rows: list) -> dict:
    out = {}
    for r in log_rows:
        d = _parse_date(r.searched_at)
        if r.status == "succeeded" and d:
            k = norm(r.company)
            out[k] = max(out.get(k, d), d)
    return out


def due_companies(companies: list, log_rows: list, interval_months: int, today: date) -> list:
    done = last_success(log_rows)
    due = [c for c in companies if norm(c.name) not in done or _add_months(done[norm(c.name)], interval_months) <= today]
    # Never-searched first, then the longest ago.
    return sorted(due, key=lambda c: done.get(norm(c.name), date.min))


def spend_this_month(log_rows: list, today: date) -> float:
    """Each run's recorded cost; a run with none recorded counts its cap (written when it started)."""
    total = 0.0
    for r in log_rows:
        d = _parse_date(r.searched_at)
        if d and d.year == today.year and d.month == today.month:
            try:
                total += float(r.cost_usd or 0)
            except ValueError:
                pass
    return round(total, 4)


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


# Company website domains for the logo column (Brandfetch). Companies not
# listed get no logo; add a domain here to give them one.
LOGO_DOMAINS = {
    "canva": "canva.com", "luxury escapes": "luxuryescapes.com", "tiktok": "tiktok.com", "telstra": "telstra.com.au",
    "afl": "afl.com.au", "deloitte": "deloitte.com", "capgemini": "capgemini.com", "kpmg": "kpmg.com", "ey": "ey.com",
    "accenture": "accenture.com", "nature": "nature.com.au", "forethought": "forethought.com.au", "mercedes-benz": "mercedes-benz.com.au", "loreal": "loreal.com", "mars": "mars.com",
    "bmw": "bmw.com.au", "bosch": "bosch.com.au", "nestle": "nestle.com", "uniqlo": "uniqlo.com",
    "red bull": "redbull.com", "bondi sands": "bondisands.com.au", "asahi": "asahibeverages.com",
    "kraft heinz": "kraftheinz.com", "kmart": "kmart.com.au", "mondelez": "mondelezinternational.com", "p&g": "pg.com",
    "unilever": "unilever.com", "penfolds": "penfolds.com",
    # AXIA (formerly Vidacorp) has no site Brandfetch knows; its parent DBG Health's logo stands in.
    "vidacorp": "dbghealth.com.au", "axia beauty": "dbghealth.com.au", "axia": "dbghealth.com.au", "coles group": "colesgroup.com.au",
    "talaria": "talariacapital.com.au", "westpac": "westpac.com.au", "anz": "anz.com", "commbank": "commbank.com.au",
    "protiviti": "protiviti.com", "ipsos": "ipsos.com", "the lab insight & strategy": "thelabstrategy.com",
    "honeycomb research": "honeycombstrategy.com.au", "oohmedia": "oohmedia.com.au",
    "news corp australia": "newscorpaustralia.com", "nine": "nine.com.au", "thinkerbell agency": "thinkerbell.com",
    "clemenger": "clemengerbbdo.com.au", "ogilvy": "ogilvy.com.au", "dentsu": "dentsu.com", "ddb": "ddb.com.au",
    "omd": "omd.com", "mccann australia": "mccann.com.au", "taboo group": "taboo.com.au", "made this": "madethis.com.au",
    "onetwo agency": "onetwoagency.com", "melbourne social co": "melbournesocialco.com.au", "thrive pr": "thrivepr.com.au",
    "id collective": "idcollective.com.au", "ampr": "ampr.com.au", "mcmpr": "mcmpr.com.au",
    "porter novelli": "porternovelli.com.au", "maxmedialab": "maxmedialab.com.au",
}
# MMSS's own Brandfetch client ID (the Job Board's; public by design). Brandfetch's
# terms require hotlinking, which =IMAGE() does; .png because Sheets can't show WebP.
BRANDFETCH_CLIENT_ID = "1ido3HOcLqD6CO4-TB5"


def logo_formula(company: str) -> str:
    domain = LOGO_DOMAINS.get(norm(company))
    if not domain:
        return ""
    url = f"https://cdn.brandfetch.io/domain/{domain}/w/128/h/128/fallback/lettermark/icon.png?c={BRANDFETCH_CLIENT_ID}"
    return f'=IMAGE("{url}")'


@dataclass
class Layout:
    """Where things are on the Hiring Managers tab, read from its header row (1-based rows, 0-based columns)."""
    header_row: int
    col: dict
    logo_col: int | None

    @property
    def first_col(self) -> int:
        return min(list(self.col.values()) + ([self.logo_col] if self.logo_col is not None else []))

    @property
    def last_col(self) -> int:
        return max(self.col.values())


def results_layout(rows: list) -> Layout:
    """
    The header is the row with COMPANY and FULL NAME, wherever it is (row 10
    since partnerships added a title block on 7 Oct 2026). Only the columns in
    it are written; columns people deleted are not put back. The logo goes in
    a LOGO column, or the unlabelled column just left of COMPANY.
    """
    for r, row in enumerate(rows):
        head = [c.strip().upper() for c in row]
        if "COMPANY" in head and "FULL NAME" in head:
            col = {h: head.index(h) for h in RESULT_HEADERS if h in head}
            if "LOGO" in head:
                logo = head.index("LOGO")
            elif col["COMPANY"] > 0 and not head[col["COMPANY"] - 1]:
                logo = col["COMPANY"] - 1
            else:
                logo = None
            return Layout(r + 1, col, logo)
    raise ValueError("No header row with COMPANY and FULL NAME on the Hiring Managers tab")


def first_free_row(rows: list, layout: Layout | None = None) -> int:
    """
    1-based row after the last one with a name or company. Not len(rows):
    blank checkbox or formatted rows read as content, so the sheet looks full
    to row 1000 and an append would land below it.
    """
    layout = layout or results_layout(rows)
    ids = [layout.col[h] for h in ("COMPANY", "FULL NAME") if h in layout.col]
    last = layout.header_row
    for r in range(layout.header_row, len(rows)):
        row = rows[r]
        if any(i < len(row) and row[i].strip() for i in ids):
            last = r + 1
    return last + 1


@dataclass
class CompanyPlan:
    """How one company's rows change: `rows` maps sheet row -> full row values,
    `new` are rows to add after `anchor` (or at the end), `delete` rows go."""
    rows: dict
    new: list
    anchor: int | None
    delete: list
    replaced: list


def plan_company(rows: list, layout: Layout, company: str, best: list, today: str) -> CompanyPlan:
    """
    Replace a company's contacts with `best` (agreed 7 Oct 2026: the tab holds
    each company's current one or two, not a history). Someone still in
    `best` keeps their row, FIRST FOUND and any SHORTLIST/NOTES; a new person
    takes over a row that is no longer needed; leftover rows are deleted. An
    empty `best` changes nothing, so a bad search never wipes good contacts.
    """
    if not best:
        return CompanyPlan({}, [], None, [], [])
    col, lo, hi = layout.col, layout.first_col, layout.last_col

    def cell(row, h):
        i = col.get(h)
        return row[i] if i is not None and i < len(row) else ""

    mine = []
    for r in range(layout.header_row, len(rows)):
        row = rows[r]
        if norm(cell(row, "COMPANY")) == norm(company) and cell(row, "FULL NAME").strip():
            mine.append((r + 1, row, person_key(cell(row, "FULL NAME"), company, safe_linkedin_url(cell(row, "LINKEDIN")))))
    by_key = {k: (n, row) for n, row, k in mine}

    def build(p, a, old_row, same_person):
        out = [""] * (hi - lo + 1)
        if old_row is not None:
            out = [(old_row[i] if i < len(old_row) else "") for i in range(lo, hi + 1)]
        values = {
            "COMPANY": company, "FULL NAME": p.name, "JOB TITLE": p.title or "", "HEADLINE": p.headline or "",
            "LOCATION": p.location or "", "LINKEDIN": p.profile_url or "", "MATCH": a.match, "WHY": a.reason,
            "LAST SEEN": today,
        }
        if not same_person:
            values.update({"FIRST FOUND": today, "SHORTLIST": False, "NOTES": ""})
        for h, v in values.items():
            if h in col:
                out[col[h] - lo] = v
        if layout.logo_col is not None:
            out[layout.logo_col - lo] = logo_formula(company)
        return out

    plan = CompanyPlan({}, [], mine[-1][0] if mine else None, [], [])
    keep_keys = set()
    pending = []
    for p, a in best:
        k = person_key(p.name, company, p.profile_url)
        if k in by_key and k not in keep_keys:
            n, row = by_key[k]
            plan.rows[n] = build(p, a, row, True)
            keep_keys.add(k)
        else:
            pending.append((p, a))
    spare = [(n, row, k) for n, row, k in mine if k not in keep_keys]
    for p, a in pending:
        if spare:
            n, row, _ = spare.pop(0)
            plan.replaced.append(cell(row, "FULL NAME"))
            plan.rows[n] = build(p, a, row, False)
        else:
            plan.new.append(build(p, a, None, False))
    for n, row, _ in spare:
        plan.replaced.append(cell(row, "FULL NAME"))
        plan.delete.append(n)
    return plan


def shortlist_rows(rows: list, company: str = "") -> list:
    """Contacts as dicts, optionally for one company. With a SHORTLIST column, only ticked rows."""
    layout = results_layout(rows)
    has_shortlist = "SHORTLIST" in layout.col
    out = []
    for row in rows[layout.header_row:]:
        d = {h: (row[i] if i < len(row) else "") for h, i in layout.col.items()}
        if not d.get("FULL NAME", "").strip():
            continue
        ticked = str(d.get("SHORTLIST", "")).strip().upper() in ("TRUE", "YES", "Y", "1", "✓")
        if (ticked or not has_shortlist) and (not company or company_matches(d.get("COMPANY"), company)):
            out.append(d)
    return out


def defuse(value: object) -> str:
    """Stop a spreadsheet treating an exported cell as a formula."""
    s = str(value if value is not None else "")
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


class Sheet:
    """The three tabs this tool reads and writes, via a service account."""

    def __init__(self, cfg: Config):
        import gspread  # imported here so tests and --dry-run math don't need credentials

        self.gspread = gspread
        self.cfg = cfg
        # BackOffHTTPClient retries Google's 429 "quota exceeded" (60 reads a
        # minute) instead of crashing mid-run; the first full run on 7 Oct 2026
        # stopped after 13 companies on exactly that.
        self.book = gspread.service_account(
            filename=cfg.credentials_file, http_client=gspread.http_client.BackOffHTTPClient
        ).open_by_key(cfg.sheet_id)
        self._tabs: dict = {}

    def _tab(self, title: str, headers: list):
        try:
            ws = self.book.worksheet(title)
        except self.gspread.WorksheetNotFound:
            ws = self.book.add_worksheet(title=title, rows=1000, cols=len(headers))
            ws.update([headers], "A1")
            ws.freeze(rows=1)
            return ws, True
        current = [h.strip().upper() for h in ws.row_values(1)]
        missing = [h for h in headers if h not in current]
        if missing:
            start = len(current)
            if ws.col_count < start + len(missing):
                ws.add_cols(start + len(missing) - ws.col_count)
            ws.update([missing], f"{_col_letter(start)}1")
            log.info("Added missing columns to %s: %s", title, ", ".join(missing))
        return ws, False

    def tracker_rows(self) -> list:
        return self.book.worksheet(self.cfg.tracker_tab).get_all_values()

    def results(self):
        if "results" not in self._tabs:
            try:
                # Use the tab as people laid it out; never add back columns they removed.
                ws = self.book.worksheet(self.cfg.results_tab)
            except self.gspread.WorksheetNotFound:
                ws, _ = self._tab(self.cfg.results_tab, ["LOGO"] + RESULT_HEADERS)
                self._shortlist_checkboxes(ws)
            self._tabs["results"] = ws
        return self._tabs["results"]

    def log(self):
        if "log" not in self._tabs:
            self._tabs["log"] = self._tab(self.cfg.log_tab, LOG_HEADERS)[0]
        return self._tabs["log"]

    def _shortlist_checkboxes(self, ws):
        i = (["LOGO"] + RESULT_HEADERS).index("SHORTLIST")
        self.book.batch_update({"requests": [{
            "setDataValidation": {
                "range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": i, "endColumnIndex": i + 1},
                "rule": {"condition": {"type": "BOOLEAN"}},
            }
        }]})


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def now_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def rank(people: list, company: Company, cities: list) -> list:
    if company.mode == RECRUITER:
        ranked = [(p, assess(p, company.name, cities, company.team)) for p in people]
    else:
        ranked = [(p, assess_manager(p, company.name, cities, company.mode)) for p in people]
    return sorted(ranked, key=lambda pa: -pa[1].score)


class BudgetReached(Exception):
    pass


class RunLimitReached(BudgetReached):
    """The Apify free plan's monthly runs are used up; the rest waits for next month."""

    def __init__(self, message: str, refused: bool = False):
        super().__init__(message)
        # True when HarvestAPI itself refused a run the script's count said was allowed:
        # the count is wrong (e.g. the free runs don't reset monthly) and needs a person.
        self.refused = refused


class Searcher:
    """
    Runs the two-stage search for a batch of companies and keeps the books.

    Stage 1 asks LinkedIn only for early-careers titles. Stage 2 runs only for
    the companies stage 1 found no Strong match at, with general talent-
    acquisition titles. Each run is logged in the Search Log at its expected
    cost BEFORE it starts and corrected after, so a crash never hides a cost.
    """

    def __init__(self, cfg: Config, sheet, apify: ApifyClient, spent: float, runs_left: int | None = None):
        self.cfg, self.sheet, self.apify, self.spent = cfg, sheet, apify, spent
        # None: no run limit (paid plan). Otherwise runs HarvestAPI still allows this month.
        self.runs_left = runs_left
        self.runs_done = 0       # runs that finished and delivered (possibly zero) people
        self.profiles_found = 0  # people delivered across those runs
        self._rows: list | None = None  # the Hiring Managers tab, read once and kept current

    def search(self, companies: list, titles: list, stage: str, function_ids: list | None = None) -> dict:
        """One paid run for `companies`. Returns {page: [Person]}."""
        if self.runs_left is not None and self.runs_left <= 0:
            raise RunLimitReached("this month's free Apify runs are used up")
        per = self.cfg.profiles_per_company
        expected = ApifyClient.expected_cost(len(companies), per)
        if self.spent + expected > self.cfg.monthly_budget_usd + 1e-9:
            raise BudgetReached(f"US${self.spent:.2f} spent; this run could cost US${expected:.3f}")
        log_ws = self.sheet.log()
        label = f"[{stage} run] " + ", ".join(c.name for c in companies)
        log_ws.append_row([now_str(), label[:250], "running", "", expected, "", "", ""], value_input_option="RAW")
        log_row = len(log_ws.col_values(1))
        run_id, cost = "", expected
        try:
            run = self.apify.start(ApifyClient.actor_input([c.page for c in companies], titles, self.cfg.cities, per,
                                                           function_ids),
                                   ApifyClient.apify_cap(len(companies), per))
            run_id = run["id"]
            if self.runs_left is not None:
                self.runs_left -= 1
            run = self.apify.wait(run_id, self.cfg.run_timeout_secs)
            raw = self.apify.items(run["defaultDatasetId"]) if run.get("defaultDatasetId") else []
            # Apify's figures lag at finish (0.296 read, 0.336 charged on 7 Oct 2026),
            # so re-read the run once before recording.
            run = self.apify.refresh(run_id) or run
            if ApifyClient.REFUSED in (run.get("statusMessage") or ""):
                # Refused, not empty: nothing about these companies is known yet.
                self.runs_left = 0
                cost = ApifyClient.run_cost(run, [], 0)
                log_ws.update([["refused", "", cost, run_id, "HarvestAPI free-plan run limit reached"]],
                              f"C{log_row}:G{log_row}", value_input_option="RAW")
                raise RunLimitReached("HarvestAPI refused the run: free-plan run limit reached", refused=True)
            cost = ApifyClient.run_cost(run, raw, len(companies))
            if run.get("status") != "SUCCEEDED":
                raise ApifyError(f"run {run_id} ended {run.get('status')}")
            log_ws.update([["succeeded", len(raw), cost, run_id, ""]], f"C{log_row}:G{log_row}", value_input_option="RAW")
            self.runs_done += 1
            self.profiles_found += len(raw)
            return normalise_items(raw)
        except ApifyError as e:
            log_ws.update([["failed", "", cost, run_id, str(e)[:300]]], f"C{log_row}:G{log_row}", value_input_option="RAW")
            raise
        finally:
            self.spent += cost

    def batch(self, companies: list) -> None:
        self.run_all(companies)

    def run_all(self, companies: list) -> None:
        """
        Every company's first search before any second one, so a limited
        month (the free plan's runs) is spent reaching as many companies as
        possible. Runs are grouped by SEARCH AS mode and sized by
        cfg.companies_per_run. If the month's runs or budget run out partway,
        companies already searched are still written; the rest stay due.
        """
        size = self.cfg.companies_per_run
        stage_one = [
            (RECRUITER, self.cfg.early_titles, "early careers", None),
            (MARKETING_TEAM, self.cfg.marketing_titles, "marketing team", ApifyClient.MARKETING_FUNCTION_IDS),
            (LEADERSHIP, self.cfg.leadership_titles, "leadership", []),
        ]
        people: dict = {}
        searched: list = []
        try:
            for mode, titles, stage, functions in stage_one:
                group = [c for c in companies if c.mode == mode]
                for i in range(0, len(group), size):
                    chunk = group[i:i + size]
                    found = self.search(chunk, titles, stage, functions)
                    for c in chunk:
                        people[c.page] = found.get(c.page, [])
                    searched += chunk
            no_strong = [c for c in searched if c.mode == RECRUITER
                         and not any(a.match == STRONG for _, a in rank(people[c.page], c, self.cfg.cities))]
            if self.cfg.fallback_titles:
                for i in range(0, len(no_strong), size):
                    chunk = no_strong[i:i + size]
                    more = self.search(chunk, self.cfg.fallback_titles, "talent acquisition")
                    for c in chunk:
                        have = {person_key(p.name, p.company, p.profile_url) for p in people[c.page]}
                        people[c.page] += [p for p in more.get(c.page, [])
                                           if person_key(p.name, p.company, p.profile_url) not in have]
        finally:
            for c in searched:
                self.write(c, people[c.page])

    def place(self, company: Company, best: list) -> list:
        """Put a company's best contacts on the tab, replacing its old ones. Returns who was replaced."""
        ws = self.sheet.results()
        if self._rows is None:
            self._rows = ws.get_all_values()
        rows = self._rows
        layout = results_layout(rows)
        plan = plan_company(rows, layout, company.name, best, date.today().isoformat())
        data_lo = min(layout.col.values())
        lo, hi = _col_letter(data_lo), _col_letter(layout.last_col)
        targets = list(plan.rows.items())
        if plan.new:
            if plan.anchor:
                # Straight under the company's own rows, with their formatting.
                ws.insert_rows([[""]] * len(plan.new), plan.anchor + 1, inherit_from_before=True)
                start = plan.anchor + 1
            else:
                start = first_free_row(rows, layout)
                if start + len(plan.new) - 1 > ws.row_count:
                    ws.add_rows(start + len(plan.new) - 1 - ws.row_count + 100)
            targets += [(start + k, v) for k, v in enumerate(plan.new)]
        # Data as RAW (dates stay text; a name can never run as a formula), the
        # logo as USER_ENTERED so its =IMAGE() formula works.
        data = [{"range": f"{lo}{n}:{hi}{n}", "values": [v[data_lo - layout.first_col:]]} for n, v in targets]
        logos = [{"range": f"{_col_letter(layout.logo_col)}{n}", "values": [[v[layout.logo_col - layout.first_col]]]}
                 for n, v in targets] if layout.logo_col is not None else []
        if data:
            ws.batch_update(data, value_input_option="RAW")
        if logos:
            ws.batch_update(logos, value_input_option="USER_ENTERED")
        for n in sorted(plan.delete, reverse=True):
            ws.delete_rows(n)
        if targets or plan.delete:
            # Rows moved: read the tab again next time. Plain writes: patch the cached copy.
            self._rows = None if (plan.new and plan.anchor) or plan.delete else self._apply(rows, data)
        return plan.replaced

    @staticmethod
    def _apply(rows, updates):
        """Keep the cached tab current after plain cell writes (no rows inserted or deleted)."""
        for u in updates:
            letters, n = re.match(r"([A-Z]+)(\d+)", u["range"]).groups()
            lo = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(letters))) - 1
            vals = u["values"][0]
            while len(rows) < int(n):
                rows.append([])
            row = rows[int(n) - 1]
            row += [""] * max(0, lo + len(vals) - len(row))
            for k, v in enumerate(vals):
                row[lo + k] = str(v)
        return rows

    def write(self, company: Company, people: list) -> None:
        ranked = rank(people, company, self.cfg.cities)
        best = pick_best(ranked, self.cfg.contacts_per_company)
        replaced = self.place(company, best)
        kept = "; ".join(f"{p.name} ({a.match})" for p, a in best) or "none suitable"
        if replaced:
            kept += " | replaced: " + ", ".join(replaced)
        # Cost is on the run rows above; this row is the company's schedule entry.
        # Nobody at all in either stage almost always means the LINKEDIN PAGE is
        # wrong (e.g. a global page when Australian staff list a local one), so
        # that isn't a finished search: the company stays due.
        status, note = ("succeeded", "") if people else ("no results", "Nobody found: check LINKEDIN PAGE")
        self.sheet.log().append_row([now_str(), company.name, status, len(people), 0, "", note, kept],
                                    value_input_option="RAW")
        counts = {m: sum(1 for _, a in ranked if a.match == m) for m in (STRONG, POSSIBLE, UNSUITABLE)}
        log.info("%s: %d found (%d strong, %d possible, %d not suitable); kept %s",
                 company.name, len(people), counts[STRONG], counts[POSSIBLE], counts[UNSUITABLE], kept)
        for p, a in ranked:
            log.debug("  %-14s %s | %s @ %s | %s | %s", a.match, p.name, p.title, p.company, p.location, a.reason)


def run(cfg: Config, only: str = "", dry_run: bool = False) -> int:
    sheet = Sheet(cfg)
    companies = read_companies(sheet.tracker_rows())
    log_rows = read_log(sheet.log().get_all_values())
    today = date.today()

    if only:
        wanted = [w.strip() for w in only.split(",") if w.strip()]
        todo = [c for c in companies if any(company_matches(c.name, w) for w in wanted)]
        missing = [w for w in wanted if not any(company_matches(c.name, w) for c in companies)]
        if missing:
            log.error("Not on the tracker: %s", ", ".join(missing))
            return 1
    else:
        todo = due_companies(companies, log_rows, cfg.research_interval_months, today)
    no_page = [c for c in todo if not c.page]
    todo = [c for c in todo if c.page]
    if no_page:
        log.warning("No LINKEDIN PAGE on the tracker yet, skipped: %s", ", ".join(c.name for c in no_page))

    spent = spend_this_month(log_rows, today)
    per = cfg.profiles_per_company
    size = cfg.companies_per_run
    worst = ApifyClient.expected_cost(len(todo), per) * 2
    first_runs = sum(-(-sum(1 for c in todo if c.mode == mode) // size) for mode in (RECRUITER, MARKETING_TEAM, LEADERSHIP))
    log.info("%d companies on the tracker, %d to search, %d per run, %d first-search runs. Spent US$%.2f of "
             "US$%.2f this month; these searches cost at most US$%.2f (both stages for every company).",
             len(companies), len(todo), size, first_runs, spent, cfg.monthly_budget_usd, worst)
    apify = ApifyClient(cfg.apify_token) if cfg.apify_token else None
    runs_left = None
    if cfg.free_plan and apify:
        runs_left = max(0, cfg.free_runs_per_month - apify.runs_this_month())
        log.info("Apify free plan: %d of %d HarvestAPI runs left this usage cycle.", runs_left, cfg.free_runs_per_month)

    if dry_run:
        for c in todo:
            log.info("Would search: %-28s as %-15s %s", c.name, c.mode, c.page)
        return 0

    if runs_left == 0:
        log.warning("No free Apify runs left this month; %d companies wait for next month's run.", len(todo))
        return 0
    searcher = Searcher(cfg, sheet, apify, spent, runs_left)
    try:
        searcher.run_all(todo)
    except RunLimitReached as e:
        if e.refused:
            # Fail loudly (GitHub emails on a failed run): left alone, this would
            # "succeed" every month while searching nothing.
            log.error("HarvestAPI refused a run although its free allowance should have had %d left this month "
                      "(%d done). The free runs may not reset monthly, or HarvestAPI changed its limits. "
                      "Nothing more will be found until someone moves to a paid Apify plan or another source.",
                      runs_left or 0, searcher.runs_done)
            return 1
        log.warning("Stopped: %s. Companies not searched stay due for next month.", e)
    except BudgetReached as e:
        log.warning("Monthly budget reached (%s). Companies not searched stay due for next month.", e)
    except ApifyError as e:
        log.error("%s", e)
        return 1
    if searcher.runs_done >= 2 and searcher.profiles_found == 0:
        # Two or more searches and nobody at all, at any company: the actor or LinkedIn
        # has almost certainly changed, not every company emptied out at once.
        log.error("%d searches returned nobody at all. The Apify actor (%s) or LinkedIn has probably changed; "
                  "the tracker needs a look.", searcher.runs_done, ApifyClient.ACTOR)
        return 1
    log.info("Run complete: spent US$%.4f this month.", searcher.spent)
    return 0


def check_keys(cfg: Config) -> int:
    ok = True
    if not cfg.apify_token:
        log.error("APIFY_TOKEN is not set")
        ok = False
    else:
        try:
            log.info("Apify token OK (account: %s)", ApifyClient(cfg.apify_token).check())
        except ApifyError as e:
            log.error("%s", e)
            ok = False
    try:
        sheet = Sheet(cfg)
        n = len(read_companies(sheet.tracker_rows()))
        log.info("Sheet OK: %d companies on %s", n, cfg.tracker_tab)
    except Exception as e:  # noqa: BLE001 -- report any access problem plainly
        log.error("Sheet check failed: %s", e)
        ok = False
    return 0 if ok else 1


def export_shortlist(cfg: Config, path: str, company: str = "") -> int:
    rows = Sheet(cfg).results().get_all_values()
    picked = shortlist_rows(rows, company)
    cols = ["COMPANY", "FULL NAME", "JOB TITLE", "LOCATION", "LINKEDIN", "MATCH", "WHY", "NOTES", "LAST SEEN"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for d in picked:
            w.writerow([defuse(d.get(c, "")) for c in cols])
    log.info("Wrote %d contacts to %s", len(picked), path)
    return 0


def parse_args(argv: list | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Find recruiters for the companies on the MMSS outreach tracker.")
    p.add_argument("--check-keys", action="store_true", help="Free: check the Apify token and sheet access, then exit.")
    p.add_argument("--dry-run", action="store_true", help="Free: list what would be searched this month, then exit.")
    p.add_argument("--company", default="", help="Search just these companies (comma-separated), due or not.")
    p.add_argument("--export-shortlist", metavar="CSV", help="Write ticked SHORTLIST rows to a CSV file.")
    return p.parse_args(argv)


def main(argv: list | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    cfg = Config()
    if not cfg.sheet_id:
        log.error("GOOGLE_SHEET_ID is not set")
        return 1
    if args.check_keys:
        return check_keys(cfg)
    if args.export_shortlist:
        return export_shortlist(cfg, args.export_shortlist, args.company)
    if not args.dry_run and not cfg.apify_token:
        log.error("APIFY_TOKEN is not set")
        return 1
    return run(cfg, only=args.company, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
