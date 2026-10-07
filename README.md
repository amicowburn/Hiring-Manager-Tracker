# Hiring Manager Tracker

Finds the graduate, early-careers and marketing recruiters at every company on
the MMSS **Company_Outreach_Tracker** Google Sheet, ranks them for MMSS, and
keeps them in a **Hiring Managers** tab of that sheet for partnerships to work
from. Set up like the Alumni Database: a Python script, a Google service
account, and a monthly GitHub Action.

## What you get in the sheet

The **Hiring Managers** tab is laid out by partnerships (title block, header
row 10, Montserrat). The script finds the header row by its COMPANY and FULL
NAME cells and writes only the columns that exist there; columns people
remove are never put back. Column A, left of COMPANY, shows each company's
logo via `=IMAGE()` from Brandfetch (MMSS's client ID; add a company's domain
to `LOGO_DOMAINS` to give it one).

**Each company keeps only its current one or two contacts.** When a search
finds someone better, they replace the old contact in the same row (and a
spare row is inserted under the company, or a leftover row deleted, if the
count changes). A person who is still the best keeps their row and FIRST
FOUND date. A search that finds nobody suitable leaves the existing contacts
alone. The Search Log's KEPT column names anyone replaced.

**Hiring Managers** tab, one row per person:

| Column | Written by | Notes |
|---|---|---|
| COMPANY, FULL NAME, LINKEDIN, FIRST FOUND | script, once | |
| JOB TITLE, HEADLINE, LOCATION, MATCH, WHY, LAST SEEN | script, refreshed each search | an empty new value never blanks an existing cell |
| SHORTLIST | **people** | a checkbox; the script never writes it |
| NOTES | **people** | the script never writes it |

Quality over quantity: only the **one or two best people per company** are
written, not everyone the search finds. `MATCH` is one of:

- **Strong match**: confirmed at the company, based in Melbourne or Sydney,
  and runs early-careers hiring, ideally for marketing, sales,
  communications or consulting. Team-specific beats general; Melbourne
  beats Sydney. Up to two per company.
- **Possible match**: written only when a company has no Strong match, and
  then just the best one: employer or location hidden on LinkedIn, graduate
  hiring only for audit/tech/finance, or a marketing/sales recruiter who
  isn't early-careers. Check these by hand before contacting.

Not suitable people are never written: graduates and interns themselves,
"talent" managers who look after creators, people who have left the
company, general HR, and anyone outside Melbourne and Sydney.

Searches are aimed at the right teams too: each company's INDUSTRY picks
the first phrases searched (consulting firms "graduate recruitment
consulting", agencies and FMCG "graduate recruitment marketing", PR firms
"graduate recruitment communications"), with generic early-careers phrases
after them.

**Search Log** tab: one row per search (time, company, status, profiles
found, cost, Apify run ID, error, and KEPT: who was written to the sheet). It is both the schedule (a company is due again
`RESEARCH_INTERVAL_MONTHS` after its last successful search) and the budget
ledger. Deleting rows from it makes companies due again and frees budget,
so don't tidy it.

To export a shortlist: filter SHORTLIST to TRUE and use File → Download →
CSV, or run `python find_hiring_managers.py --export-shortlist shortlist.csv
[--company Canva]`.

## Where the people come from, and what it costs

Apify's [`harvestapi/linkedin-company-employees`](https://apify.com/harvestapi/linkedin-company-employees)
actor, which runs LinkedIn's own people search with exact filters: the
company's LinkedIn page (the tracker's **LINKEDIN PAGE** column), Melbourne
or Sydney, LinkedIn's Human Resources function, and job titles. No LinkedIn
login of ours is involved.

Two stages per batch of companies:

1. Early-careers titles only (Early Careers, Graduate, Campus, Emerging
   Talent, Talent Programs, Intern, University).
2. Only for companies with no Strong match after stage 1: general
   talent-acquisition titles, so a company still gets its best recruiter.

**Free-plan limits (found the hard way, 7 Oct 2026):** HarvestAPI lets free
Apify accounts make only **10 runs** and get **25 profiles per run**; later
runs come back "free user run limit exceeded" with nothing in them. MMSS
stays on the free plan, so the script asks for 3 people per company, 8
companies per run, counts this month's runs on Apify before starting, does
every company's first search before any fallback, and stops cleanly when
the runs are used up. Companies it didn't reach stay due for next month.
It is not documented whether the 10 runs reset monthly; the run on 5 Nov
2026 will show. Runs are counted from the start of Apify's usage cycle
(the 4th for this account), not the calendar month.

Pricing on Apify's free plan (7 Oct 2026): US$0.003 per profile plus a
US$0.02 start fee per company searched. With 3 profiles per company that is
about US$0.03 per company per stage, well inside Apify's US$5 free monthly
credit. `APIFY_MONTHLY_BUDGET_USD`
(default 4.50) is enforced from the Search Log.

(The first version used `memo23/linkedin-people-search`. Two test searches
on 7 Oct 2026 returned general staff and one recruiter from another
company: it runs loose web searches and reads logged-out pages, which hide
most titles and employers.)

Only work fields are kept. Photos and everything else are dropped before
anything is written. No emails or phone numbers are requested.

**SEARCH AS** (tracker column, a dropdown) picks who to look for at each company:

| SEARCH AS | Who | LinkedIn search |
|---|---|---|
| Recruiter (default) | Early-careers / graduate recruiters, then general talent acquisition | Human Resources function; early-careers titles, then TA titles |
| Marketing team | The marketing manager who runs student or graduate hiring (e.g. Red Bull's student marketing) | Marketing and Media & Communication functions; Student Marketing, Brand/Marketing Manager, Head of Marketing |
| Leadership | Small firms with no recruiter: head of people, founder, MD or GM; other directors as Possible | Any function; Founder, Managing Director, General Manager, Head of People, People & Culture, Director |

Set to Leadership on 7 Oct 2026: the PR firms, small agencies and
consultancies, Bondi Sands, Vidacorp, Talaria, The Lab, Honeycomb, McCann
Australia, Porter Novelli. Marketing team: Red Bull, Uniqlo (Uniqlo's
graduate program is retail management, which MMSS doesn't want).

**Filling in LINKEDIN PAGE:** open the company on LinkedIn and copy the
address, e.g. `https://www.linkedin.com/company/canva`. Use the main
company page; the Melbourne/Sydney filter does the rest. A wrong page shows
up as people marked "Works at X now, not Y" and nothing written.

## Setup

1. `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
2. Google: reuse the Alumni Database's service account (or make one the
   same way) and share the Company_Outreach_Tracker sheet with its
   `client_email` as **Editor**. Put the key at `credentials.json`.
3. Apify: sign up with the shared MMSS Google login, then copy the API token
   from Settings → API & Integrations.
4. `cp .env.example .env` and fill in `APIFY_TOKEN`.
5. `python find_hiring_managers.py --check-keys` (free)
6. `python find_hiring_managers.py --dry-run` (free; shows what would run)
7. `python find_hiring_managers.py --company "Canva,Deloitte"` (real searches, a few cents)

## GitHub Action

`.github/workflows/monthly.yml` runs at 09:00 UTC on the 5th of each month (the Apify free allowance resets on the 4th for this account)
and can be run by hand from the Actions tab (optionally for one company).
Repository secrets:

- `APIFY_TOKEN`
- `GOOGLE_SHEET_ID`
- `GOOGLE_SHEETS_CREDENTIALS` (the whole service account JSON)

## Tests

`python -m unittest test_find_hiring_managers -v`. Every API is mocked:
running them spends nothing and never touches the sheet.

## Running unattended (checked 8 Oct 2026)

Built to run for years without anyone touching it:

- **The schedule can't lapse.** GitHub switches off scheduled workflows in a
  public repo after 60 days without commits; the workflow re-enables itself
  every month.
- **Python 3.13** (supported until October 2029) and the current major
  versions of GitHub's checkout and Python-setup actions.
- **Tests run before every search**, so a runner, Python or library change
  fails the run before it touches the sheet.
- **Failures are loud.** These fail the run, and GitHub emails the repo owner:
  - a rejected key or sheet access;
  - an Apify error;
  - HarvestAPI refusing a run the script thought was allowed (its free
    allowance has changed);
  - two or more searches returning nobody at all (the actor or LinkedIn has
    changed).
  Running out of the month's free runs, or the budget, is normal and isn't a
  failure.
- **No surprise bills.** The Apify account is on the free plan, with no
  payment method, so it can't be charged. The script also caps itself at
  US$4.50 a month.

What can still stop it, and what each needs:

| Risk | What you'd see | Fix |
|---|---|---|
| HarvestAPI's 10 free runs turn out not to reset monthly | Failed run email in November 2026 | Paid Apify plan, or another source |
| HarvestAPI or Apify retires or changes the actor, its pricing or its free limits | Failed run email | Pick another actor; update `ApifyClient` |
| The Apify token is revoked or expires (if it was created with an expiry date) | Failed "Check keys" step | New token, then `gh secret set APIFY_TOKEN` |
| The Google Cloud project `alumni-database-506007` or its service account key is deleted or disabled | Failed "Check keys" step | New key, then `gh secret set GOOGLE_SHEETS_CREDENTIALS` (and the Alumni Database's too) |
| Someone renames or deletes the COMPANY or FULL NAME headers, or a tab | Failed run naming the missing header | Put the header back |
| A company changes its LinkedIn page | That company shows "no results" in the Search Log every month | Update its LINKEDIN PAGE |
| The repo owner's GitHub account goes away | Nothing runs | Transfer the repo to the MMSS organisation first |
| Brandfetch changes its terms or retires the client ID | Logos go blank; searching is unaffected | New client ID in `BRANDFETCH_CLIENT_ID` |
