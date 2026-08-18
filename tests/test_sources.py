"""
Adapter tests against captured payload shapes. No network: each test serves a
recorded response through an httpx MockTransport, so a schema change in an
adapter breaks a test instead of silently returning zero jobs at 3am.
"""

import asyncio
import json

import httpx
import pytest

from radar import sources


def client_returning(payload, *, status=200, content_type="application/json",
                     capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if isinstance(payload, (dict, list)):
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=payload,
                              headers={"content-type": content_type})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ helpers

def test_strip_html():
    assert sources.strip_html("<p>Hello <b>world</b></p>").strip() == "Hello  world"
    assert sources.strip_html(None) == ""


def test_strip_html_unescapes_entities():
    assert "&" in sources.strip_html("<p>R&amp;D</p>")


def test_slug_to_title():
    assert sources._slug_to_title("software-engineering-phd-intern-2027") == \
        "software engineering phd intern 2027"


def test_deobfuscate_lisu_homoglyphs():
    """Jane Street serves 'Machine Learning' with Lisu lookalikes swapped in."""
    assert sources.deobfuscate("ꓟachine ꓡearning ꓣesearcher") == \
        "Machine Learning Researcher"


def test_deobfuscate_leaves_ascii_alone():
    assert sources.deobfuscate("Software Engineer Intern") == "Software Engineer Intern"


def test_board_key():
    assert sources.board_key({"ats": "greenhouse", "name": "Acme", "token": "acme"}) == \
        "greenhouse:acme"
    assert sources.board_key({"ats": "workday", "name": "Acme", "tenant": "acme"}) == \
        "workday:acme"


# ----------------------------------------------------------------- adapters

def test_greenhouse_parsing():
    payload = {"jobs": [{
        "id": 123, "title": "Software Engineer Intern",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
        "location": {"name": "Seattle, WA"},
        "updated_at": "2026-08-01T00:00:00Z",
        "content": "<p>Build things</p>",
    }]}
    jobs = run(sources.fetch_greenhouse(client_returning(payload), "Acme", "acme"))
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "Software Engineer Intern"
    assert j["location"] == "Seattle, WA"
    assert j["external_id"] == "123"
    assert j["source"] == "greenhouse"


def test_greenhouse_missing_location_does_not_crash():
    payload = {"jobs": [{"id": 1, "title": "X", "absolute_url": "u", "location": None}]}
    assert run(sources.fetch_greenhouse(client_returning(payload), "A", "a"))[0]["location"] == ""


def test_lever_parsing():
    payload = [{
        "id": "abc", "text": "SWE Intern", "hostedUrl": "https://jobs.lever.co/acme/abc",
        "categories": {"location": "New York"}, "createdAt": 1700000000,
        "descriptionPlain": "Do work",
    }]
    j = run(sources.fetch_lever(client_returning(payload), "Acme", "acme"))[0]
    assert j["title"] == "SWE Intern" and j["location"] == "New York"
    assert j["source"] == "lever"


def test_ashby_skips_unlisted():
    payload = {"jobs": [
        {"id": "1", "title": "Listed", "jobUrl": "u", "isListed": True},
        {"id": "2", "title": "Hidden", "jobUrl": "u", "isListed": False},
    ]}
    jobs = run(sources.fetch_ashby(client_returning(payload), "Acme", "acme"))
    assert [j["title"] for j in jobs] == ["Listed"]


def test_smartrecruiters_builds_location_and_url():
    payload = {"content": [{
        "id": "77", "name": "DE Intern",
        "location": {"city": "Boston", "region": "MA", "country": "us"},
    }]}
    j = run(sources.fetch_smartrecruiters(client_returning(payload), "Acme", "acme"))[0]
    assert j["location"] == "Boston, MA, us"
    assert j["url"].endswith("/acme/77")


def test_workday_parsing_and_url():
    payload = {"jobPostings": [{
        "title": "SWE Intern", "externalPath": "/job/Seattle/SWE-Intern_JR1",
        "bulletFields": ["JR1"], "locationsText": "Seattle, WA",
        "postedOn": "Posted 3 Days Ago",
    }]}
    j = run(sources.fetch_workday(client_returning(payload), "Acme", "acme",
                                  "Careers", "wd5", max_pages=1))[0]
    assert j["external_id"] == "JR1"
    assert j["url"] == "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Seattle/SWE-Intern_JR1"


def test_workday_handles_list_locations():
    payload = {"jobPostings": [{"title": "T", "externalPath": "/p",
                                "locations": ["Austin", "Dallas"], "jobReqId": "R1"}]}
    j = run(sources.fetch_workday(client_returning(payload), "A", "a", "S", max_pages=1))[0]
    assert j["location"] == "Austin, Dallas"


def test_eightfold_parsing():
    payload = {"positions": [{
        "id": 99, "name": "SWE Intern", "location": "Los Gatos, CA",
        "display_job_id": "JR9", "t_create": 1776988800, "job_description": "<p>hi</p>",
    }], "count": 1}
    j = run(sources.fetch_eightfold(client_returning(payload), "Netflix",
                                    "explore.jobs.netflix.net", "netflix.com",
                                    max_pages=1))[0]
    assert j["title"] == "SWE Intern" and j["external_id"] == "JR9"


def test_amazon_pins_accept_encoding():
    """Regression: httpx's default Accept-Encoding makes amazon.jobs 500."""
    seen = []
    payload = {"jobs": [{"title": "SDE Intern", "job_path": "/en/jobs/1",
                         "normalized_location": "Seattle, WA", "id_icims": "1"}]}
    jobs = run(sources.fetch_amazon(client_returning(payload, capture=seen),
                                    "Amazon", "intern", max_pages=1))
    assert jobs[0]["url"] == "https://www.amazon.jobs/en/jobs/1"
    assert seen[0].headers["accept-encoding"] == "gzip"


def test_apple_parsing():
    payload = {"res": {"totalRecords": 1, "searchResults": [{
        "positionId": "200",
        "postingTitle": "Software Undergrad Engineering Internships",
        "transformedPostingTitle": "software-undergrad-engineering-internships",
        "locations": [{"name": "Cupertino"}], "postingDate": "Aug 1, 2026",
        "reqId": "REQ200", "jobSummary": "Come build",
    }]}}
    j = run(sources.fetch_apple(client_returning(payload), max_pages=1))[0]
    assert j["external_id"] == "REQ200"
    assert j["url"] == ("https://jobs.apple.com/en-us/details/200/"
                        "software-undergrad-engineering-internships")


def test_apple_defaults_to_internship_query():
    """'intern' returns Apple's whole corpus; 'internship' actually filters."""
    seen = []
    run(sources.fetch_apple(client_returning({"res": {"searchResults": []}},
                                             capture=seen), max_pages=1))
    assert json.loads(seen[0].content)["query"] == "internship"


def test_google_reads_ids_and_slugs_from_html():
    html = '''<a href="jobs/results/12345-software-engineering-intern-2027?q=intern">
              <span>x</span></a>
              <a href="jobs/results/12345-software-engineering-intern-2027">dup</a>'''
    jobs = run(sources.fetch_google(client_returning(html, content_type="text/html"),
                                    max_pages=1))
    assert len(jobs) == 1, "duplicate ids must collapse"
    assert jobs[0]["title"] == "software engineering intern 2027"
    assert jobs[0]["external_id"] == "12345"


def test_janestreet_deobfuscates_and_folds_in_availability():
    payload = [{
        "id": 1, "position": "ꓟachine ꓡearning ꓣesearcher",
        "availability": "Summer Internship", "city": "NYC", "overview": "<p>hi</p>",
    }]
    j = run(sources.fetch_janestreet(client_returning(payload)))[0]
    assert j["title"] == "Machine Learning Researcher - Summer Internship"
    assert j["location"] == "NYC"


def test_html_link_watcher_falls_back_to_slug():
    html = '<a href="/careers/position/swe-intern-summer-2027"><div></div></a>'
    j = run(sources.fetch_html_links(client_returning(html, content_type="text/html"),
                                     "Acme", "https://acme.com/careers",
                                     "/careers/position/", "https://acme.com"))[0]
    assert j["title"] == "swe intern summer 2027"
    assert j["url"] == "https://acme.com/careers/position/swe-intern-summer-2027"


# ------------------------------------------------------------------- meta

class FakeCache:
    def __init__(self, seed=None):
        self.data = dict(seed or {})

    def cache_get(self, key):
        return self.data.get(key)

    def cache_put(self, key, title, location="", description=""):
        self.data[key] = {"title": title, "location": location,
                          "description": description}


SITEMAP = """<urlset>
<url><loc>https://www.metacareers.com/profile/job_details/111/</loc>
<lastmod>2026-08-17T12:00:00-07:00</lastmod></url>
<url><loc>https://www.metacareers.com/profile/job_details/222/</loc>
<lastmod>2026-08-16T12:00:00-07:00</lastmod></url>
</urlset>"""

JOB_PAGE = ('<html><script>{"@type":"JobPosting","title":"Software Engineer Intern"}'
            '</script><meta name="description" content="Build things"></html>')


def meta_client(capture=None):
    def handler(request):
        if capture is not None:
            capture.append(str(request.url))
        if "sitemap" in str(request.url):
            return httpx.Response(200, text=SITEMAP,
                                  headers={"content-type": "application/xml"})
        return httpx.Response(200, text=JOB_PAGE,
                              headers={"content-type": "text/html"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_meta_hydrates_unseen_jobs():
    cache = FakeCache()
    jobs = run(sources.fetch_meta(meta_client(), cache=cache, max_hydrate=5))
    assert len(jobs) == 2
    assert jobs[0]["title"] == "Software Engineer Intern"
    assert jobs[0]["external_id"] == "111", "newest lastmod must come first"
    assert cache.cache_get("meta:111") is not None


def test_meta_uses_cache_and_stops_refetching():
    cache = FakeCache({"meta:111": {"title": "Cached Intern", "description": ""},
                       "meta:222": {"title": "Other", "description": ""}})
    urls = []
    jobs = run(sources.fetch_meta(meta_client(urls), cache=cache, max_hydrate=5))
    assert [j["title"] for j in jobs] == ["Cached Intern", "Other"]
    assert len(urls) == 1, "steady state must be one sitemap request"


def test_meta_respects_hydrate_budget():
    cache = FakeCache()
    jobs = run(sources.fetch_meta(meta_client(), cache=cache, max_hydrate=1))
    assert len(jobs) == 1, "budget caps detail fetches per poll"


# ---------------------------------------------------------------- dispatch

def test_unknown_ats_raises():
    with pytest.raises(ValueError):
        run(sources.fetch_board(client_returning({}), {"ats": "nope", "name": "X"}))


def test_microsoft_raises_unsupported():
    """Must fail loudly, not quietly return zero jobs."""
    with pytest.raises(sources.UnsupportedBoard):
        run(sources.fetch_board(client_returning({}),
                                {"ats": "microsoft", "name": "Microsoft"}))
