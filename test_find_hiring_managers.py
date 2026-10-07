"""
Test suite for find_hiring_managers.py
======================================

Every Apify and Google Sheets call is mocked. Running this suite spends no
Apify credit and never touches a real sheet.

Run:
    python -m unittest test_find_hiring_managers -v
"""

from __future__ import annotations

import logging
import unittest
from datetime import date
from unittest.mock import MagicMock

import find_hiring_managers as m

logging.getLogger("hiring_manager_tracker").setLevel(logging.CRITICAL)


CITIES = ["Melbourne", "Sydney"]
CANVA = "https://www.linkedin.com/company/canva"


def harvest_row(first, last, title, company="Canva", location="Sydney, New South Wales, Australia",
                page=CANVA, slug=None, summary="Growing our team"):
    """One item as harvestapi/linkedin-company-employees returns it (Short mode)."""
    return {
        "id": "ACwAAx", "firstName": first, "lastName": last, "summary": summary,
        "linkedinUrl": f"https://www.linkedin.com/in/{slug or (first + last).lower()}",
        "pictureUrl": "https://media.licdn.com/photo", "premium": True, "openProfile": False,
        "currentPositions": [{"companyName": company, "title": title, "current": True,
                              "companyLinkedinUrl": "https://www.linkedin.com/company/2850862"}],
        "location": {"linkedinText": location},
        "_meta": {"query": {"currentCompanies": [page]}},
    }


def person(title, location="Melbourne, Victoria, Australia", company="Canva", **kw):
    return m.Person(name=kw.pop("name", "Test Person"), title=title, company=company, location=location, **kw)


def judge(p, company="Canva", team=""):
    return m.assess(p, company, CITIES, team)


class TestAssess(unittest.TestCase):
    def check(self, title, expected, works_at="Canva", search="Canva", team="", **kw):
        """`works_at` is the person's current employer; `search` the tracker company."""
        a = judge(person(title, company=works_at, **kw), search, team)
        self.assertEqual(a.match, expected, f"{title}: {a.reason}")
        return a

    # --- Strong: early-careers hiring, confirmed employer, Melbourne or Sydney
    def test_graduate_marketing_recruiter_in_melbourne_is_strong(self):
        a = self.check("Graduate Recruitment Lead, Marketing", m.STRONG)
        self.assertIn("for marketing", a.reason)
        self.assertIn("Based in Melbourne", a.reason)

    def test_sydney_counts(self):
        self.check("Campus Recruiter – Sales", m.STRONG, location="Sydney, New South Wales, Australia")

    def test_vic_and_nsw_abbreviations_count(self):
        self.check("Early Careers Lead", m.STRONG, location="Melbourne, VIC")
        self.check("Early Careers Lead", m.STRONG, location="Greater Sydney Area, NSW")

    def test_tracker_role_talent_programs_manager_is_strong(self):
        # Canva's CONTACT ROLE on the tracker today.
        self.check("Talent Programs Manager", m.STRONG)

    def test_general_early_careers_lead_is_strong_but_ranks_below_team_specific(self):
        general = self.check("Early Careers Lead", m.STRONG)
        marketing = self.check("Early Careers Lead – Marketing & Brand", m.STRONG)
        self.assertGreater(marketing.score, general.score)

    def test_melbourne_ranks_above_sydney(self):
        mel = self.check("Early Careers Lead", m.STRONG)
        syd = self.check("Early Careers Lead", m.STRONG, location="Sydney")
        self.assertGreater(mel.score, syd.score)

    def test_industry_team_adds_a_point(self):
        p = person("Graduate Recruitment – Consulting")
        self.assertGreater(judge(p, team="consulting").score, judge(p).score)

    def test_communications_and_publications(self):
        a = self.check("Graduate Talent Acquisition – Corporate Affairs & Publications", m.STRONG)
        self.assertIn("communications", a.reason)

    def test_headline_stands_in_for_hidden_title(self):
        a = judge(m.Person("X", headline="Early Careers @ Canva | Hiring our next grads", company="Canva",
                           location="Melbourne"))
        self.assertEqual(a.match, m.STRONG)

    # --- real titles from the first HarvestAPI test (7 Oct 2026)
    def test_ta_consultant_for_graduates_is_strong_not_consulting(self):
        a = self.check("Talent Acquisition Consultant | Graduate & Early Careers", m.STRONG, works_at="Deloitte",
                       search="Deloitte", location="Sydney, New South Wales, Australia")
        self.assertNotIn("consulting", a.reason, "'Consultant' is a recruiter's title, not a team")

    def test_misspelt_acquisition_and_audit_only_is_possible(self):
        a = self.check("Graduate Talent Aquisition Manager - National Audit & Assurance + Enabling Area Lead",
                       m.POSSIBLE, works_at="Deloitte Australia", search="Deloitte", location="Greater Melbourne Area")
        self.assertIn("only names audit", a.reason)

    def test_consulting_ta_manager_is_possible(self):
        self.check("Talent Acquisition Manager- Consulting", m.POSSIBLE, works_at="Deloitte", search="Deloitte",
                   location="Greater Sydney Area")

    def test_general_tech_ta_is_not_suitable(self):
        self.check("Talent Acquisition Partner, Technology", m.UNSUITABLE, location="Sydney")
        a = self.check("Talent Acquisition Partner", m.POSSIBLE, location="The Rocks, New South Wales, Australia")
        self.assertIn("ask them for the early-careers contact", a.reason)

    def test_general_recruiter_is_the_last_resort(self):
        general = self.check("Head of Talent Attraction Delivery", m.POSSIBLE, works_at="KPMG", search="KPMG")
        audit_grad = self.check("Graduate Recruiter, Audit", m.POSSIBLE, works_at="KPMG", search="KPMG")
        self.assertGreater(audit_grad.score, general.score)
        self.check("HR Business Partner", m.UNSUITABLE, works_at="KPMG", search="KPMG")
        self.check("Talent Acquisition Partner", m.UNSUITABLE, location="Perth, Western Australia")
        a = self.check("Graduate Reward Consultant", m.UNSUITABLE, works_at="EY", search="EY")
        self.assertIn("Works with graduates", a.reason)

    def test_learning_and_development_is_not_hiring(self):
        a = self.check("Learning Manager, Top Talent Programs", m.POSSIBLE, works_at="Deloitte", search="Deloitte")
        self.assertIn("not hiring", a.reason)
        self.check("Program Manager - The Academy by Deloitte", m.UNSUITABLE, works_at="Deloitte", search="Deloitte")

    def test_summary_does_not_override_a_real_title(self):
        p = person("Talent Acquisition Partner", headline="Helping emerging talent launch their careers")
        a = judge(p)
        self.assertNotEqual(a.match, m.STRONG)
        self.assertIn("General recruiter", a.reason)

    # --- the false positives the first version had
    def test_a_graduate_is_not_a_recruiter(self):
        a = self.check("Graduate Marketing Associate", m.UNSUITABLE)
        self.assertIn("graduate or intern themselves", a.reason)
        self.check("Marketing Intern", m.UNSUITABLE)

    def test_hr_graduate_is_a_graduate(self):
        self.check("Human Resources Graduate", m.UNSUITABLE, works_at="Westpac", search="Westpac")
        self.check("Graduate Program Consultant ( Campus Recruiter )", m.STRONG, works_at="Westpac", search="Westpac")

    def test_creator_talent_manager_is_not_a_recruiter(self):
        self.check("Creator Talent Manager", m.UNSUITABLE, works_at="TikTok", search="TikTok")

    def test_word_boundaries(self):
        # "it" must not fire inside "recruitment", nor "hr" inside "three".
        a = self.check("Early Careers Recruitment for three brands", m.STRONG)
        self.assertNotIn("technology", a.reason)

    # --- Possible: worth a human look
    def test_early_careers_for_audit_only_is_possible_not_strong(self):
        a = self.check("Graduate Recruiter, Audit & Assurance", m.POSSIBLE)
        self.assertIn("only names audit", a.reason)

    def test_employer_hidden_is_possible(self):
        a = self.check("Early Careers Lead", m.POSSIBLE, works_at=None)
        self.assertIn("check before contacting", a.reason)

    def test_location_hidden_is_possible(self):
        self.check("Early Careers Lead", m.POSSIBLE, location=None)

    def test_partial_profile_is_possible(self):
        self.check("Early Careers Lead", m.POSSIBLE, partial=True)

    def test_marketing_recruiter_not_early_careers_is_possible(self):
        self.check("Talent Acquisition Partner – Marketing", m.POSSIBLE)

    # --- Not suitable
    def test_brisbane_is_not_suitable(self):
        a = self.check("Early Careers Lead", m.UNSUITABLE, location="Brisbane, Queensland, Australia")
        self.assertIn("not Melbourne or Sydney", a.reason)

    def test_overseas_is_not_suitable(self):
        self.check("Graduate Recruitment Specialist", m.UNSUITABLE, location="Singapore")

    def test_general_people_and_culture_is_not_suitable(self):
        self.check("People & Culture Business Partner", m.UNSUITABLE)

    def test_tech_recruiter_is_not_suitable(self):
        self.check("Senior Technical Recruiter, Engineering", m.UNSUITABLE)

    def test_marketer_is_not_suitable(self):
        self.check("Brand Marketing Manager", m.UNSUITABLE)

    def test_left_the_company_is_not_suitable(self):
        a = self.check("Graduate Recruiter", m.UNSUITABLE, works_at="Atlassian")
        self.assertIn("Works at Atlassian now", a.reason)

    def test_employer_named_in_headline_is_checked(self):
        # Second Canva test, 7 Oct 2026: employer field hidden, headline said Google.
        a = judge(m.Person("Timothy", headline="Campus Recruiter at Google", location=None, partial=True))
        self.assertEqual(a.match, m.UNSUITABLE)
        self.assertIn("Works at Google now", a.reason)
        ok = judge(m.Person("X", headline="Early Careers @ Canva | Hiring grads", location="Melbourne"))
        self.assertNotEqual(ok.match, m.UNSUITABLE)
        self.assertIsNone(m.headline_employer("Senior Talent Acquisition Partner at ..."))

    def test_company_alias_counts_as_same_company(self):
        a = judge(person("Graduate Recruitment Lead", company="Commonwealth Bank"), "Commbank")
        self.assertEqual(a.match, m.STRONG)

    def test_vidacorp_is_now_axia_beauty(self):
        a = judge(person("Founder & Managing Director", company="Axia Beauty"), "Vidacorp")
        self.assertNotIn("Works at", a.reason)
        self.assertIn("dbghealth.com.au", m.logo_formula("Vidacorp"))

    def test_no_title_or_headline(self):
        a = judge(m.Person("X", company="Canva", location="Melbourne"))
        self.assertEqual(a.match, m.UNSUITABLE)


class TestAssessManager(unittest.TestCase):
    def check(self, title, mode, expected, works_at="Red Bull", search="Red Bull", location="Melbourne, Victoria"):
        a = m.assess_manager(person(title, company=works_at, location=location), search, CITIES, mode)
        self.assertEqual(a.match, expected, f"{title}: {a.reason}")
        return a

    def test_marketing_team(self):
        student = self.check("Student Marketing Manager", m.MARKETING_TEAM, m.STRONG)
        brand = self.check("Brand Manager", m.MARKETING_TEAM, m.STRONG)
        self.assertGreater(student.score, brand.score, "the person who runs student marketing ranks first")
        self.check("Marketing Coordinator", m.MARKETING_TEAM, m.POSSIBLE)
        self.check("Graduate Marketing Associate", m.MARKETING_TEAM, m.UNSUITABLE)
        self.check("Finance Manager", m.MARKETING_TEAM, m.UNSUITABLE)
        self.check("Brand Manager", m.MARKETING_TEAM, m.UNSUITABLE, location="Brisbane, Queensland")
        self.check("Brand Manager", m.MARKETING_TEAM, m.UNSUITABLE, works_at="Monster Energy")

    def test_leadership(self):
        people = self.check("Head of People & Culture", m.LEADERSHIP, m.STRONG, works_at="Thrive PR", search="Thrive PR")
        founder = self.check("Founder & Managing Director", m.LEADERSHIP, m.STRONG, works_at="Thrive PR", search="Thrive PR")
        account = self.check("Account Director", m.LEADERSHIP, m.POSSIBLE, works_at="Thrive PR", search="Thrive PR")
        self.assertGreater(people.score, founder.score)
        self.assertGreater(founder.score, account.score)
        self.check("Account Executive", m.LEADERSHIP, m.UNSUITABLE, works_at="Thrive PR", search="Thrive PR")


class TestPickBest(unittest.TestCase):
    def ranked(self, *titles):
        ps = [person(t, name=t) for t in titles]
        return sorted([(p, judge(p)) for p in ps], key=lambda pa: -pa[1].score)

    def test_top_two_strong_only(self):
        best = m.pick_best(self.ranked("Early Careers Lead", "Graduate Recruitment – Marketing",
                                       "Campus Recruiter", "Brand Manager"), 2)
        self.assertEqual([p.name for p, _ in best], ["Graduate Recruitment – Marketing", "Early Careers Lead"])

    def test_falls_back_to_one_possible(self):
        best = m.pick_best(self.ranked("Talent Acquisition Partner – Marketing", "Talent Acquisition – Sales",
                                       "Brand Manager"), 2)
        self.assertEqual(len(best), 1)
        self.assertEqual(best[0][1].match, m.POSSIBLE)

    def test_nothing_suitable_writes_nothing(self):
        self.assertEqual(m.pick_best(self.ranked("Brand Manager", "Software Engineer"), 2), [])


class TestNormaliseItems(unittest.TestCase):
    def test_harvest_rows_grouped_by_company_page_with_work_fields_only(self):
        deloitte = "https://www.linkedin.com/company/deloitte"
        items = [
            harvest_row("Ann", "Lee", "Early Careers Lead"),
            harvest_row("Ann", "Lee", "Early Careers Lead"),  # duplicate
            harvest_row("Raj", "Patel", "Graduate Talent Acquisition", company="Deloitte Australia",
                        location="Greater Melbourne Area", page=deloitte),
            harvest_row("", "", "No name"),
            {"firstName": "Bad", "lastName": "Url", "linkedinUrl": "javascript:alert(1)",
             "_meta": {"query": {"currentCompanies": [CANVA + "/"]}}},
            "junk",
        ]
        out = m.normalise_items(items)
        self.assertEqual(sorted(out), [CANVA, deloitte])
        self.assertEqual([p.name for p in out[CANVA]], ["Ann Lee", "Bad Url"])
        ann = out[CANVA][0]
        self.assertEqual((ann.title, ann.company, ann.location),
                         ("Early Careers Lead", "Canva", "Sydney, New South Wales, Australia"))
        self.assertEqual(ann.profile_url, "https://www.linkedin.com/in/annlee")
        self.assertFalse(hasattr(ann, "pictureUrl"))
        self.assertIsNone(ann.headline, "the actor's summary is an About bio; never kept")
        self.assertIsNone(out[CANVA][1].profile_url, "non-LinkedIn URLs are dropped")
        self.assertEqual(out[deloitte][0].company, "Deloitte Australia")

    def test_company_page_normalised(self):
        self.assertEqual(m.company_page("linkedin.com/company/canva/"), CANVA)
        self.assertEqual(m.company_page("https://au.linkedin.com/company/canva?trk=x"), CANVA)
        self.assertIsNone(m.company_page("https://canva.com"))
        self.assertIsNone(m.company_page(""))

    def test_person_key_ignores_host_and_trailing_slash(self):
        a = m.person_key("x", "y", "https://www.linkedin.com/in/janedoe/")
        b = m.person_key("x", "z", "https://au.linkedin.com/in/janedoe?trk=1")
        self.assertEqual(a, b)


class TestReadCompanies(unittest.TestCase):
    ROWS = [
        ["", "", ""],
        ["MONASH MARKETING STUDENTS SOCIETY - OUTREACH", "", ""],
        ["", "COMPANY", "INDUSTRY", "CONTACT", "LINKEDIN PAGE", "SEARCH AS"],
        ["", "Canva", "Tech", "", "linkedin.com/company/canva", ""],
        ["", "", "", ""],
        ["", "Deloitte", "Consulting", "", "", "leadership"],
        ["", "canva", "Tech"],
        ["", "insert Company Here", "industry of company"],
        ["", "IPSOS", "Market research"],
    ]

    def test_finds_header_anywhere_and_skips_blanks_dupes_placeholder(self):
        cs = m.read_companies(self.ROWS)
        self.assertEqual([c.name for c in cs], ["Canva", "Deloitte", "IPSOS"])
        self.assertEqual(cs[1].team, "consulting")
        self.assertEqual(cs[2].team, "consulting")
        self.assertEqual(cs[0].team, "")
        self.assertEqual(cs[0].page, CANVA)
        self.assertEqual(cs[1].page, "")
        self.assertEqual((cs[0].mode, cs[1].mode, cs[2].mode), (m.RECRUITER, m.LEADERSHIP, m.RECRUITER))

    def test_no_header(self):
        with self.assertRaises(ValueError):
            m.read_companies([["a", "b"]])


class TestSchedulingAndBudget(unittest.TestCase):
    LOG = [
        m.LOG_HEADERS,
        ["2026-07-01 10:00", "Canva", "succeeded", "8", "0.053", "r1", ""],
        ["2026-10-02 10:00", "Deloitte", "succeeded", "10", "0.065", "r2", ""],
        ["2026-10-03 10:00", "KPMG", "failed", "", "0.065", "r3", "Apify 500"],
        ["2026-10-04 10:00", "EY", "running", "", "0.065", "", ""],
    ]

    def setUp(self):
        self.rows = m.read_log(self.LOG)
        self.companies = [m.Company(n) for n in ("Canva", "Deloitte", "KPMG", "EY", "Nine")]

    def test_due(self):
        due = m.due_companies(self.companies, self.rows, 3, date(2026, 10, 7))
        # Deloitte was searched this month; Canva 3+ months ago; failed/running count as not searched.
        self.assertEqual([c.name for c in due], ["KPMG", "EY", "Nine", "Canva"])

    def test_not_due_inside_interval(self):
        due = m.due_companies(self.companies, self.rows, 4, date(2026, 10, 7))
        self.assertNotIn("Canva", [c.name for c in due])

    def test_spend_counts_this_month_including_failed_and_running(self):
        self.assertAlmostEqual(m.spend_this_month(self.rows, date(2026, 10, 7)), 0.195)

    def test_add_months_clamps_day(self):
        self.assertEqual(m._add_months(date(2026, 11, 30), 3), date(2027, 2, 28))

    def test_expected_cost_and_apify_cap(self):
        # The US$0.02 start fee is charged per company searched.
        self.assertEqual(m.ApifyClient.expected_cost(20, 10), 1.0)
        # Apify is given the full-profile price, or the actor stops early ("max charge reached").
        self.assertEqual(m.ApifyClient.apify_cap(20, 10), 2.0)
        self.assertEqual(m.ApifyClient.apify_cap(1, 1), 0.05, "the actor's minimum cap")


# The tab as partnerships laid it out on 7 Oct 2026: title block, header on row 10 from
# column B, column A left blank for the logo, HEADLINE/SHORTLIST/NOTES removed.
HEAD = ["", "COMPANY", "FULL NAME", "JOB TITLE", "LOCATION", "LINKEDIN", "MATCH", "WHY", "FIRST FOUND", "LAST SEEN"]


def tab(*data):
    title = [["MONASH MARKETING STUDENTS SOCIETY"] + [""] * 9] + [[""] * 10] * 8
    return title + [HEAD] + [list(r) for r in data] + [[""] * 10] * 3


def old_row(company, name, slug, title="Recruiter", first="2026-07-01"):
    return ["", company, name, title, "Melbourne", f"https://www.linkedin.com/in/{slug}", "Strong match", "x",
            first, first]


class TestPlanCompany(unittest.TestCase):
    def ranked(self, *people):
        return [(p, judge(p)) for p in people]

    def test_layout_follows_the_header_row(self):
        lay = m.results_layout(tab())
        self.assertEqual(lay.header_row, 10)
        self.assertEqual(lay.col["COMPANY"], 1)
        self.assertEqual(lay.logo_col, 0)
        self.assertNotIn("SHORTLIST", lay.col, "removed columns stay removed")

    def test_same_person_keeps_row_and_first_found(self):
        rows = tab(old_row("Canva", "Ann Lee", "annlee"))
        ann = m.Person("Ann Lee", title="Early Careers Lead", company="Canva", location="Melbourne",
                       profile_url="https://au.linkedin.com/in/annlee/")
        plan = m.plan_company(rows, m.results_layout(rows), "Canva", self.ranked(ann), "2026-10-07")
        self.assertEqual(list(plan.rows), [11])
        row = plan.rows[11]
        self.assertEqual(row[HEAD.index("JOB TITLE")], "Early Careers Lead")
        self.assertEqual(row[HEAD.index("FIRST FOUND")], "2026-07-01")
        self.assertEqual(row[HEAD.index("LAST SEEN")], "2026-10-07")
        self.assertTrue(row[0].startswith('=IMAGE("https://cdn.brandfetch.io/domain/canva.com/'))
        self.assertTrue(row[0].endswith('icon.png?c=1ido3HOcLqD6CO4-TB5")'))
        self.assertEqual((plan.new, plan.delete, plan.replaced), ([], [], []))

    def test_new_contact_replaces_the_old_one_in_place(self):
        rows = tab(old_row("Canva", "Old Person", "old"), old_row("Mars", "Mo", "mo"))
        new = m.Person("Bo Ng", title="Early Careers Lead", company="Canva", location="Melbourne",
                       profile_url="https://www.linkedin.com/in/bong")
        plan = m.plan_company(rows, m.results_layout(rows), "Canva", self.ranked(new), "2026-10-07")
        self.assertEqual(list(plan.rows), [11], "same row, new person")
        self.assertEqual(plan.rows[11][HEAD.index("FULL NAME")], "Bo Ng")
        self.assertEqual(plan.rows[11][HEAD.index("FIRST FOUND")], "2026-10-07")
        self.assertEqual(plan.replaced, ["Old Person"])
        self.assertEqual(plan.delete, [])

    def test_fewer_contacts_deletes_leftover_rows(self):
        rows = tab(old_row("Canva", "A", "a"), old_row("Canva", "B", "b"), old_row("Mars", "Mo", "mo"))
        one = m.Person("C", title="Early Careers Lead", company="Canva", location="Melbourne",
                       profile_url="https://www.linkedin.com/in/c")
        plan = m.plan_company(rows, m.results_layout(rows), "Canva", self.ranked(one), "2026-10-07")
        self.assertEqual(list(plan.rows), [11])
        self.assertEqual(plan.delete, [12])
        self.assertEqual(plan.replaced, ["A", "B"])

    def test_more_contacts_insert_under_the_company(self):
        rows = tab(old_row("Canva", "A", "a"), old_row("Mars", "Mo", "mo"))
        ps = [m.Person(n, title="Early Careers Lead", company="Canva", location="Melbourne",
                       profile_url=f"https://www.linkedin.com/in/{n.lower()}") for n in ("A", "B")]
        plan = m.plan_company(rows, m.results_layout(rows), "Canva", self.ranked(*ps), "2026-10-07")
        self.assertEqual(list(plan.rows), [11])
        self.assertEqual(len(plan.new), 1)
        self.assertEqual(plan.anchor, 11, "inserted straight under Canva, above Mars")

    def test_new_company_goes_after_the_last_contact(self):
        rows = tab(old_row("Mars", "Mo", "mo"))
        p = m.Person("Z", title="Early Careers Lead", company="Canva", location="Melbourne")
        plan = m.plan_company(rows, m.results_layout(rows), "Canva", self.ranked(p), "2026-10-07")
        self.assertIsNone(plan.anchor)
        self.assertEqual(m.first_free_row(rows), 12)

    def test_nothing_suitable_leaves_existing_contacts(self):
        rows = tab(old_row("Canva", "A", "a"))
        plan = m.plan_company(rows, m.results_layout(rows), "Canva", [], "2026-10-07")
        self.assertEqual((plan.rows, plan.new, plan.delete), ({}, [], []))

    def test_no_logo_for_unknown_company(self):
        self.assertEqual(m.logo_formula("Some Unlisted Agency"), "")


class TestFirstFreeRow(unittest.TestCase):
    def test_blank_checkbox_rows_are_free(self):
        blank = ["", "", "", "", "", "", "", "", "FALSE", "", "", ""]
        rows = [m.RESULT_HEADERS, ["Canva", "A"] + [""] * 10, blank, blank]
        self.assertEqual(m.first_free_row(rows), 3)
        self.assertEqual(m.first_free_row([m.RESULT_HEADERS, blank, blank]), 2)


class TestShortlistExport(unittest.TestCase):
    def test_ticked_rows_for_company(self):
        rows = [m.RESULT_HEADERS,
                ["Canva", "A", "", "", "", "", m.STRONG, "", "TRUE", "", "", ""],
                ["Canva", "B", "", "", "", "", m.STRONG, "", "FALSE", "", "", ""],
                ["Deloitte", "C", "", "", "", "", m.STRONG, "", "TRUE", "", "", ""]]
        self.assertEqual([d["FULL NAME"] for d in m.shortlist_rows(rows)], ["A", "C"])
        self.assertEqual([d["FULL NAME"] for d in m.shortlist_rows(rows, "canva")], ["A"])

    def test_defuse_formulas(self):
        self.assertEqual(m.defuse("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(m.defuse("Jane"), "Jane")


class TestApifyClient(unittest.TestCase):
    def client(self, *responses):
        session = MagicMock()
        session.headers = {}
        resps = []
        for status, body in responses:
            r = MagicMock(status_code=status, ok=200 <= status < 300)
            r.json.return_value = body
            r.text = str(body)
            resps.append(r)
        session.request.side_effect = resps
        return m.ApifyClient("tok", session=session, sleep=lambda s: None), session

    def test_actor_input(self):
        inp = m.ApifyClient.actor_input([CANVA], ["Early Careers"], CITIES, 10)
        self.assertEqual(inp["companies"], [CANVA])
        self.assertEqual(inp["locations"], CITIES)
        self.assertEqual(inp["functionIds"], ["12"])
        self.assertEqual(inp["maxItemsPerCompany"], 10)
        self.assertEqual(inp["companyBatchMode"], "one_by_one")
        self.assertTrue(inp["profileScraperMode"].startswith("Short"), "never the email-search mode")
        self.assertNotIn("cookie", inp)

    def test_run_cost_uses_the_highest_figure(self):
        items = [harvest_row("A", str(i), "Recruiter") for i in range(10)]
        self.assertEqual(m.ApifyClient.run_cost({"usageTotalUsd": 0.02}, items), 0.05)
        # The first full run: 15 companies, 12 profiles, 15 start events.
        self.assertEqual(m.ApifyClient.run_cost({"chargedEventCounts": {"actor-start": 15, "short-profile": 12}}, []), 0.336)
        self.assertEqual(m.ApifyClient.run_cost({"usageTotalUsd": 0.0}, [], companies=15), 0.3)
        events = {"actor-start": 1, "short-profile": 5}
        self.assertEqual(m.ApifyClient.run_cost({"usageTotalUsd": 0.0, "chargedEventCounts": events}, []), 0.035)
        self.assertEqual(m.ApifyClient.run_cost({"usageTotalUsd": 0.09}, items), 0.09)

    def test_start_sends_charge_cap(self):
        c, s = self.client((201, {"data": {"id": "run1"}}))
        c.start({}, 1.62)
        self.assertIn("harvestapi~linkedin-company-employees/runs?maxTotalChargeUsd=1.62", s.request.call_args[0][1])

    def test_retries_5xx_then_succeeds(self):
        c, s = self.client((503, {}), (200, {"data": {"username": "mmss"}}))
        self.assertEqual(c.check(), "mmss")
        self.assertEqual(s.request.call_count, 2)

    def test_401_is_reported_plainly(self):
        c, _ = self.client((401, {"error": {"message": "bad"}}))
        with self.assertRaisesRegex(m.ApifyError, "APIFY_TOKEN"):
            c.check()


class TestSearcher(unittest.TestCase):
    def setUp(self):
        self.cfg = m.Config(sheet_id="x", apify_token="t", profiles_per_company=10, cities=CITIES,
                            contacts_per_company=2, early_titles=["Early Careers"], fallback_titles=["Talent Acquisition"],
                            batch_size=20, monthly_budget_usd=4.5, free_plan=False)
        self.log_ws = MagicMock()
        self.log_rows = [m.LOG_HEADERS]
        self.log_ws.append_row.side_effect = lambda row, **kw: self.log_rows.append(row)
        self.log_ws.col_values.side_effect = lambda i: [r[0] for r in self.log_rows]
        self.results_ws = MagicMock()
        self.results_ws.get_all_values.return_value = [m.RESULT_HEADERS] + [[""] * 8 + ["FALSE"] + [""] * 3] * 5
        self.results_ws.row_count = 1000
        self.sheet = MagicMock()
        self.sheet.log.return_value = self.log_ws
        self.sheet.results.return_value = self.results_ws
        self.canva = m.Company("Canva", "Tech", CANVA)
        self.mars = m.Company("Mars", "FMCG", "https://www.linkedin.com/company/mars")

    def apify(self, *runs, message="success"):
        """Each run: (status, items)."""
        a = MagicMock()
        a.start.side_effect = [{"id": f"run{i}"} for i in range(len(runs))]
        a.wait.side_effect = [{"status": st, "defaultDatasetId": f"ds{i}", "usageTotalUsd": 0.0, "statusMessage": message}
                              for i, (st, _) in enumerate(runs)]
        a.items.side_effect = [items for _, items in runs]
        a.refresh.return_value = None
        return a

    def test_two_stages_logged_and_only_best_written(self):
        mars = self.mars.page
        stage1 = [
            harvest_row("Ann", "Lee", "Graduate Recruitment Lead – Marketing", location="Melbourne, Victoria, Australia"),
            harvest_row("Bo", "Ng", "Early Careers Lead"),
            harvest_row("Cy", "Ho", "Early Careers Coordinator", location="Sydney"),
            harvest_row("Di", "Wu", "Graduate", company="Mars", page=mars, location="Melbourne"),  # a graduate
        ]
        stage2 = [harvest_row("Ed", "Ma", "Talent Acquisition Partner – Marketing", company="Mars", page=mars,
                              location="Melbourne")]
        apify = self.apify(("SUCCEEDED", stage1), ("SUCCEEDED", stage2))
        s = m.Searcher(self.cfg, self.sheet, apify, spent=0.0)
        s.batch([self.canva, self.mars])

        # Stage 2 only for Mars, the company with no Strong match.
        self.assertEqual(apify.start.call_args_list[1][0][0]["companies"], [mars])
        self.assertEqual(apify.start.call_args_list[1][0][0]["jobTitles"], ["Talent Acquisition"])
        run_rows = [r for r in self.log_rows[1:] if r[1].startswith("[")]
        self.assertEqual(len(run_rows), 2)
        self.assertEqual(run_rows[0][2:5], ["running", "", 0.1], "logged at expected cost (2 x (10 x 0.003 + 0.02)) before starting")
        finals = [c[0][0][0] for c in self.log_ws.update.call_args_list]
        self.assertEqual(finals[0][:3], ["succeeded", 4, 0.052])
        company_rows = {r[1]: r for r in self.log_rows[1:] if not r[1].startswith("[")}
        self.assertIn("Ann Lee (Strong match); Bo Ng (Strong match)", company_rows["Canva"][7])
        self.assertIn("Ed Ma (Possible match)", company_rows["Mars"][7])
        self.assertEqual(company_rows["Mars"][2], "succeeded")
        data_writes = [c[0][0] for c in self.results_ws.batch_update.call_args_list
                       if c[1].get("value_input_option") == "RAW"]
        self.assertEqual([len(w) for w in data_writes], [2, 1], "two for Canva, one for Mars")
        self.assertEqual(data_writes[0][0]["range"], "A2:L2", "header on row 1 here, first free row under it")
        self.assertAlmostEqual(s.spent, 0.052 + 0.023)

    def test_modes_split_into_their_own_searches(self):
        red_bull = m.Company("Red Bull", "FMCG", "https://www.linkedin.com/company/red-bull", m.MARKETING_TEAM)
        thrive = m.Company("Thrive PR", "Public Relations", "https://www.linkedin.com/company/thrive", m.LEADERSHIP)
        stage1 = [harvest_row("Ann", "Lee", "Early Careers Lead")]
        mkt = [harvest_row("Mo", "Ra", "Student Marketing Manager", company="Red Bull", page=red_bull.page,
                           location="Sydney")]
        lead = [harvest_row("Jo", "Ko", "Founder & Director", company="Thrive PR", page=thrive.page, location="Sydney")]
        apify = self.apify(("SUCCEEDED", stage1), ("SUCCEEDED", mkt), ("SUCCEEDED", lead))
        m.Searcher(self.cfg, self.sheet, apify, spent=0.0).batch([self.canva, red_bull, thrive])
        inputs = [c[0][0] for c in apify.start.call_args_list]
        self.assertEqual([i["companies"] for i in inputs], [[CANVA], [red_bull.page], [thrive.page]])
        self.assertEqual(inputs[0]["functionIds"], ["12"])
        self.assertEqual(inputs[1]["functionIds"], ["15", "16"])
        self.assertNotIn("functionIds", inputs[2], "leadership searches every function")
        kept = {r[1]: r[7] for r in self.log_rows[1:] if not r[1].startswith("[")}
        self.assertEqual(kept["Red Bull"], "Mo Ra (Strong match)")
        self.assertEqual(kept["Thrive PR"], "Jo Ko (Strong match)")

    def free_cfg(self):
        return m.Config(sheet_id="x", apify_token="t", profiles_per_company=3, cities=CITIES, contacts_per_company=2,
                        early_titles=["Early Careers"], fallback_titles=["Talent Acquisition"], batch_size=20,
                        monthly_budget_usd=4.5, free_plan=True)

    def companies(self, n):
        return [m.Company(f"Co{i}", "Tech", f"https://www.linkedin.com/company/co{i}") for i in range(n)]

    def test_free_plan_runs_fit_25_profiles(self):
        self.assertEqual(self.free_cfg().companies_per_run, 8)
        inputs = m.ApifyClient.actor_input(["p"] * 8, ["t"], CITIES, 3)
        self.assertLessEqual(inputs["maxItems"], m.ApifyClient.FREE_ITEMS_PER_RUN)

    def test_run_limit_first_searches_before_fallbacks_and_writes_what_was_searched(self):
        cos = self.companies(10)
        apify = self.apify(("SUCCEEDED", []), ("SUCCEEDED", []))
        s = m.Searcher(self.free_cfg(), self.sheet, apify, spent=0.0, runs_left=2)
        with self.assertRaises(m.RunLimitReached):
            s.run_all(cos)
        sizes = [len(c[0][0]["companies"]) for c in apify.start.call_args_list]
        self.assertEqual(sizes, [8, 2], "both first-search runs happen before any fallback")
        written = [r[1] for r in self.log_rows[1:] if not r[1].startswith("[")]
        self.assertEqual(written, [c.name for c in cos], "every searched company is still written")

    def test_refused_run_stops_and_leaves_companies_due(self):
        cos = self.companies(3)
        apify = self.apify(("SUCCEEDED", []), message=m.ApifyClient.REFUSED)
        s = m.Searcher(self.free_cfg(), self.sheet, apify, spent=0.0, runs_left=5)
        with self.assertRaises(m.RunLimitReached):
            s.run_all(cos)
        self.assertEqual(self.log_ws.update.call_args[0][0][0][0], "refused")
        self.assertEqual([r for r in self.log_rows[1:] if not r[1].startswith("[")], [],
                         "refused companies get no row, so they stay due")
        self.assertEqual(s.runs_left, 0)

    def test_refusal_is_marked_and_counted(self):
        apify = self.apify(("SUCCEEDED", [harvest_row("A", "B", "Early Careers Lead")]), ("SUCCEEDED", []),
                           message="success")
        apify.wait.side_effect = [
            {"status": "SUCCEEDED", "defaultDatasetId": "ds0", "usageTotalUsd": 0.0, "statusMessage": "success"},
            {"status": "SUCCEEDED", "defaultDatasetId": "ds1", "usageTotalUsd": 0.0, "statusMessage": m.ApifyClient.REFUSED},
        ]
        s = m.Searcher(self.free_cfg(), self.sheet, apify, spent=0.0, runs_left=5)
        with self.assertRaises(m.RunLimitReached) as ctx:
            s.run_all(self.companies(10))
        self.assertTrue(ctx.exception.refused)
        self.assertEqual((s.runs_done, s.profiles_found), (1, 1))

    def test_running_out_of_counted_runs_is_not_a_refusal(self):
        s = m.Searcher(self.free_cfg(), self.sheet, self.apify(), spent=0.0, runs_left=0)
        with self.assertRaises(m.RunLimitReached) as ctx:
            s.run_all(self.companies(2))
        self.assertFalse(ctx.exception.refused)

    def test_runs_this_month_skips_refused_and_last_month(self):
        c = m.ApifyClient("t", session=MagicMock(), sleep=lambda s: None)
        listing = {"data": {"items": [{"id": "a", "startedAt": "2026-10-07T12:00:00Z"},
                                      {"id": "b", "startedAt": "2026-10-07T11:00:00Z"},
                                      {"id": "c", "startedAt": "2026-09-30T23:00:00Z"}]}}
        details = {"a": {"data": {"statusMessage": m.ApifyClient.REFUSED}}, "b": {"data": {"statusMessage": "success"}}}
        c._request = lambda method, path, **kw: listing if "/runs?" in path else details[path.rsplit("/", 1)[1]]
        self.assertEqual(c.runs_this_month(m.datetime(2026, 10, 7, tzinfo=m.timezone.utc)), 1)

    def test_nobody_found_stays_due(self):
        apify = self.apify(("SUCCEEDED", []), ("SUCCEEDED", []))
        m.Searcher(self.cfg, self.sheet, apify, spent=0.0).batch([self.canva])
        row = [r for r in self.log_rows[1:] if r[1] == "Canva"][0]
        self.assertEqual(row[2], "no results")
        self.assertIn("check LINKEDIN PAGE", row[6])
        due = m.due_companies([self.canva], m.read_log([[str(c) for c in r] for r in self.log_rows]), 3,
                              m.date.today())
        self.assertEqual([c.name for c in due], ["Canva"])

    def test_budget_stops_before_a_run_that_could_overspend(self):
        s = m.Searcher(self.cfg, self.sheet, self.apify(), spent=4.0)  # 20 companies could cost US$1.00
        with self.assertRaises(m.BudgetReached):
            s.batch([self.canva] * 20)
        self.log_ws.append_row.assert_not_called()

    def test_failed_run_is_logged_and_counted(self):
        apify = self.apify(("FAILED", []))
        s = m.Searcher(self.cfg, self.sheet, apify, spent=0.0)
        with self.assertRaises(m.ApifyError):
            s.batch([self.canva])
        final = self.log_ws.update.call_args[0][0][0]
        self.assertEqual(final[:4], ["failed", "", 0.02, "run0"])
        self.results_ws.batch_update.assert_not_called()
        self.assertEqual(s.spent, 0.02)


if __name__ == "__main__":
    unittest.main()
