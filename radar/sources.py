"""
Async adapters for public job-board APIs.

Every adapter returns a list of normalized dicts:
    {company, title, url, location, posted_at, external_id, description, source}

All of these are the same unauthenticated JSON endpoints the companies' own
careers pages call from the browser. No auth is bypassed, nothing is logged
into. Be polite: the concurrency cap and per-host delay in engine.py exist
for a reason, and you should keep them.

Adapter status (verified live against real payloads, Aug 2026):
    greenhouse / lever / ashby / smartrecruiters / workable   JSON, stable
    workday                                                   JSON, stable
    amazon                                                    JSON, stable
    apple                                                     JSON, /api/v1/search
    eightfold                                                 JSON (Netflix)
    google                                                    server-rendered HTML
    meta                                                      sitemap + JSON-LD
    html                                                      generic link differ
    microsoft                                                 NO PUBLIC API (see below)
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from typing import Any

import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Full browser-ish header set. Some of these boards (Meta especially) return
# 400 for a bare curl-style request and 200 for this.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

JSON_HEADERS = {"User-Agent": UA, "Accept": "application/json"}

_TAG_RE = re.compile(r"<[^>]+>")

# Some boards (Jane Street) swap Latin capitals for visually identical Lisu
# codepoints to break naive scrapers - "Machine Learning" arrives as
# "ꓟachine ꓡearning". Fold them back or the matcher never fires.
HOMOGLYPHS = {
    "ꓐ": "B", "ꓑ": "P", "ꓓ": "D", "ꓔ": "T", "ꓗ": "K",
    "ꓙ": "J", "ꓚ": "C", "ꓜ": "Z", "ꓝ": "F", "ꓟ": "M",
    "ꓠ": "N", "ꓡ": "L", "ꓢ": "S", "ꓣ": "R", "ꓦ": "V",
    "ꓧ": "H", "ꓨ": "G", "ꓪ": "W", "ꓫ": "X", "ꓬ": "Y",
    "ꓮ": "A", "ꓰ": "E", "ꓲ": "I", "ꓳ": "O", "ꓴ": "U",
}
_HOMOGLYPH_TABLE = str.maketrans(HOMOGLYPHS)


def deobfuscate(s: str) -> str:
    return (s or "").translate(_HOMOGLYPH_TABLE)


def strip_html(s: str | None, limit: int = 4000) -> str:
    if not s:
        return ""
    return html.unescape(_TAG_RE.sub(" ", s))[:limit]


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return default


def _slug_to_title(slug: str) -> str:
    """'software-engineering-phd-intern-2027' -> 'software engineering phd intern 2027'"""
    s = re.sub(r"[-_]+", " ", slug)
    return re.sub(r"\s+", " ", s).strip()


# ==========================================================================
# Greenhouse   https://boards-api.greenhouse.io/v1/boards/{token}/jobs
# ==========================================================================

async def fetch_greenhouse(client: httpx.AsyncClient, company: str, token: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = await client.get(url, headers=JSON_HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "posted_at": j.get("updated_at", "") or j.get("first_published", ""),
            "external_id": str(j.get("id", "")),
            "description": strip_html(j.get("content")),
            "source": "greenhouse",
        })
    return out


# ==========================================================================
# Lever   https://api.lever.co/v0/postings/{company}?mode=json
# ==========================================================================

async def fetch_lever(client: httpx.AsyncClient, company: str, token: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = await client.get(url, headers=JSON_HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        out.append({
            "company": company,
            "title": j.get("text", ""),
            "url": _first(j, "hostedUrl", "applyUrl"),
            "location": cats.get("location", "") or "",
            "posted_at": str(j.get("createdAt", "")),
            "external_id": str(j.get("id", "")),
            "description": strip_html(j.get("descriptionPlain") or j.get("description")),
            "source": "lever",
        })
    return out


# ==========================================================================
# Ashby   https://api.ashbyhq.com/posting-api/job-board/{token}
# ==========================================================================

async def fetch_ashby(client: httpx.AsyncClient, company: str, token: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    r = await client.get(url, headers=JSON_HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "url": _first(j, "jobUrl", "applyUrl"),
            "location": j.get("location", "") or "",
            "posted_at": j.get("publishedAt", ""),
            "external_id": str(_first(j, "id", "jobId")),
            "description": strip_html(j.get("descriptionPlain") or j.get("descriptionHtml")),
            "source": "ashby",
        })
    return out


# ==========================================================================
# SmartRecruiters
# ==========================================================================

async def fetch_smartrecruiters(client: httpx.AsyncClient, company: str, token: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while offset < 400:  # safety cap
        url = (f"https://api.smartrecruiters.com/v1/companies/{token}"
               f"/postings?limit=100&offset={offset}")
        r = await client.get(url, headers=JSON_HEADERS)
        r.raise_for_status()
        data = r.json()
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            loc_s = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
            out.append({
                "company": company,
                "title": j.get("name", ""),
                "url": f"https://jobs.smartrecruiters.com/{token}/{j.get('id','')}",
                "location": loc_s,
                "posted_at": j.get("releasedDate", ""),
                "external_id": str(j.get("id", "")),
                "description": "",
                "source": "smartrecruiters",
            })
        if len(items) < 100:
            break
        offset += 100
    return out


# ==========================================================================
# Workable
# ==========================================================================

async def fetch_workable(client: httpx.AsyncClient, company: str, token: str) -> list[dict]:
    url = f"https://www.workable.com/api/accounts/{token}?details=true"
    r = await client.get(url, headers=JSON_HEADERS, follow_redirects=True)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "company": company,
            "title": j.get("title", ""),
            "url": j.get("url", "") or f"https://apply.workable.com/j/{j.get('shortcode','')}",
            "location": j.get("location", "") or "",
            "posted_at": j.get("published_on", ""),
            "external_id": str(j.get("shortcode", "")),
            "description": strip_html(j.get("description")),
            "source": "workable",
        })
    return out


# ==========================================================================
# Workday CXS  -  POST /wday/cxs/{tenant}/{site}/jobs
# Covers most Fortune-500 tech (Nvidia, Salesforce, Adobe, Intuit, ...).
# We pass searchText="intern" so we pull ~20 rows instead of ~4000.
# ==========================================================================

async def fetch_workday(
    client: httpx.AsyncClient,
    company: str,
    tenant: str,
    site: str,
    dc: str = "wd1",
    search_text: str = "intern",
    max_pages: int = 5,
) -> list[dict]:
    base = f"https://{tenant}.{dc}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "en-US",
        "Referer": f"{base}/en-US/{site}",
        "Origin": base,
    }
    out: list[dict] = []
    for page in range(max_pages):
        payload = {"appliedFacets": {}, "limit": 20, "offset": page * 20,
                   "searchText": search_text}
        r = await client.post(api, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        posts = data.get("jobPostings", [])
        for j in posts:
            ext = j.get("externalPath", "")
            # bulletFields is a list like ["JR123"]; jobReqId is a bare string.
            bullets = j.get("bulletFields") or []
            req_id = (bullets[0] if isinstance(bullets, list) and bullets
                      else j.get("jobReqId") or ext)
            loc = j.get("locationsText") or j.get("locations") or ""
            if isinstance(loc, list):
                loc = ", ".join(str(x) for x in loc[:3])
            out.append({
                "company": company,
                "title": j.get("title", ""),
                "url": f"{base}/en-US/{site}{ext}" if ext else base,
                "location": loc,
                # Workday gives relative strings like "Posted 3 Days Ago"
                "posted_at": j.get("postedOn", ""),
                "external_id": str(req_id),
                "description": "",
                "source": "workday",
            })
        if len(posts) < 20:
            break
        await asyncio.sleep(0.6)   # Workday sits behind Akamai - go gently
    return out


# ==========================================================================
# Eightfold  -  /api/apply/v2/jobs?domain=...
# Netflix and a growing number of large employers run on this. The API caps
# at 10 results per request no matter what `num` you pass, so we page.
# ==========================================================================

async def fetch_eightfold(
    client: httpx.AsyncClient,
    company: str,
    host: str,
    domain: str,
    query: str = "intern",
    max_pages: int = 6,
) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(max_pages):
        url = (f"https://{host}/api/apply/v2/jobs?domain={domain}"
               f"&query={query}&start={page * 10}&num=10&sort_by=timestamp")
        r = await client.get(url, headers=JSON_HEADERS)
        r.raise_for_status()
        data = r.json()
        positions = data.get("positions") or []
        for j in positions:
            pid = str(j.get("id", ""))
            if pid in seen:
                continue
            seen.add(pid)
            locs = j.get("locations") or ([j["location"]] if j.get("location") else [])
            out.append({
                "company": company,
                "title": _first(j, "name", "posting_name"),
                "url": j.get("canonicalPositionUrl") or
                       f"https://{host}/careers/job/{pid}",
                "location": ", ".join(str(x) for x in locs[:3]),
                "posted_at": str(j.get("t_create", "")),
                "external_id": str(_first(j, "display_job_id", "ats_job_id", "id")),
                "description": strip_html(j.get("job_description")),
                "source": "eightfold",
            })
        if len(positions) < 10:
            break
        await asyncio.sleep(0.4)
    return out


# ==========================================================================
# Big-tech custom endpoints
# These are the highest-value and the most likely to drift. `radar doctor`
# tells you the moment one starts returning nothing.
# ==========================================================================

async def fetch_amazon(client: httpx.AsyncClient, company: str = "Amazon",
                       query: str = "intern", max_pages: int = 4) -> list[dict]:
    """
    amazon.jobs search.json. We deliberately do NOT pin a category facet:
    Amazon files MLE roles under machine-learning-science and DE roles under
    business-intelligence, so a software-development filter silently drops
    two of the three role families you care about. Sorted newest-first.
    """
    out: list[dict] = []
    # amazon.jobs double-encodes its response body; httpx's default
    # Accept-Encoding (which advertises br/zstd) then dies with
    # "cannot use a decompressobj multiple times". Pinning gzip fixes it.
    headers = {**JSON_HEADERS, "Accept-Encoding": "gzip"}
    for page in range(max_pages):
        url = ("https://www.amazon.jobs/en/search.json"
               f"?base_query={query}&result_limit=100&offset={page*100}&sort=recent")
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        jobs = data.get("jobs", [])
        for j in jobs:
            out.append({
                "company": company,
                "title": j.get("title", ""),
                "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                "location": _first(j, "normalized_location", "location"),
                "posted_at": _first(j, "posted_date", "updated_time"),
                "external_id": str(_first(j, "id_icims", "id")),
                "description": strip_html(_first(j, "description", "basic_qualifications")),
                "source": "amazon",
            })
        if len(jobs) < 100:
            break
        await asyncio.sleep(0.5)
    return out


async def fetch_apple(client: httpx.AsyncClient, company: str = "Apple",
                      query: str = "internship", max_pages: int = 6) -> list[dict]:
    """
    jobs.apple.com/api/v1/search (the old /api/role/search 301s to a 404 page).

    Query it with "internship", NOT "intern". Apple's search treats "intern"
    as too-common and hands back the entire 1,900-role corpus newest-first -
    which is all retail - while "internship" narrows to ~116 real reqs
    including the "Software Undergrad Engineering Internships" and
    "Machine Learning and AI Undergrad Internships" umbrella postings.
    """
    out: list[dict] = []
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://jobs.apple.com/en-us/search",
    }
    for page in range(1, max_pages + 1):
        payload = {
            "query": query,
            "filters": {"postingpostLocation": []},
            "page": page,
            "locale": "en-us",
            "sort": "newest",
            "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
        }
        r = await client.post("https://jobs.apple.com/api/v1/search",
                              json=payload, headers=headers)
        r.raise_for_status()
        res = (r.json() or {}).get("res") or {}
        roles = res.get("searchResults") or []
        for j in roles:
            pid = j.get("positionId", "")
            slug = j.get("transformedPostingTitle", "")
            locs = j.get("locations") or []
            out.append({
                "company": company,
                "title": j.get("postingTitle", ""),
                "url": (f"https://jobs.apple.com/en-us/details/{pid}/{slug}" if pid
                        else "https://jobs.apple.com/en-us/search"),
                "location": ", ".join(l.get("name", "") for l in locs[:3]),
                "posted_at": j.get("postingDate", ""),
                "external_id": str(_first(j, "reqId", "positionId")),
                "description": strip_html(j.get("jobSummary")),
                "source": "apple",
            })
        if len(roles) < 20:
            break
        await asyncio.sleep(0.5)
    return out


# --------------------------------------------------------------------------
# Google - the careers results page is server-rendered, so no JS needed.
# The anchors wrap nested markup rather than plain text, so the reliable
# title source is the URL slug itself:
#   jobs/results/133355062935069382-software-engineering-phd-intern-2027
# --------------------------------------------------------------------------

_GOOGLE_JOB_RE = re.compile(r'href="(?:[^"]*?)jobs/results/(?P<id>\d+)-(?P<slug>[a-z0-9\-]+)')


async def fetch_google(client: httpx.AsyncClient, company: str = "Google",
                       query: str = "intern", max_pages: int = 3) -> list[dict]:
    base = "https://www.google.com/about/careers/applications"
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        url = (f"{base}/jobs/results/?q={query}"
               f"&target_level=INTERN_AND_APPRENTICE&page={page}")
        r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True)
        r.raise_for_status()
        found = 0
        for m in _GOOGLE_JOB_RE.finditer(r.text):
            jid, slug = m.group("id"), m.group("slug")
            if jid in seen:
                continue
            seen.add(jid)
            found += 1
            out.append({
                "company": company,
                "title": _slug_to_title(slug),
                "url": f"{base}/jobs/results/{jid}-{slug}",
                "location": "",
                "posted_at": "",
                "external_id": jid,
                "description": "",
                "source": "google",
            })
        if not found:
            break
        await asyncio.sleep(0.4)
    return out


# --------------------------------------------------------------------------
# Meta - no public search API, but metacareers.com/jobs/sitemap.xml lists
# every live req with a <lastmod>. So: diff the sitemap, and hydrate only the
# reqs we've never seen, newest first, reading the schema.org JobPosting
# JSON-LD off the detail page.
#
# Steady state that's ONE request per poll. The first few hours backfill the
# existing ~840 reqs at `max_hydrate` per cycle, then it goes quiet.
# --------------------------------------------------------------------------

_META_SITEMAP = "https://www.metacareers.com/jobs/sitemap.xml"
_META_URL_RE = re.compile(
    r"<loc>[^<]*?/job_details/(?P<id>\d+)/?</loc>\s*<lastmod>(?P<mod>[^<]+)</lastmod>"
)
_JSONLD_RE = re.compile(r'"@type"\s*:\s*"JobPosting"\s*,\s*"title"\s*:\s*"(?P<title>[^"]+)"')
_META_DESC_RE = re.compile(r'<meta[^>]*name="description"[^>]*content="([^"]*)"')


async def fetch_meta(client: httpx.AsyncClient, company: str = "Meta",
                     cache=None, max_hydrate: int = 25) -> list[dict]:
    r = await client.get(_META_SITEMAP, headers=BROWSER_HEADERS, follow_redirects=True)
    r.raise_for_status()

    entries = [(m.group("id"), m.group("mod")) for m in _META_URL_RE.finditer(r.text)]
    entries.sort(key=lambda e: e[1], reverse=True)   # newest lastmod first

    out: list[dict] = []
    budget = max_hydrate
    for jid, mod in entries:
        key = f"meta:{jid}"
        hit = cache.cache_get(key) if cache is not None else None
        if hit is None:
            if budget <= 0:
                continue
            budget -= 1
            try:
                title, desc = await _hydrate_meta_job(client, jid)
            except Exception:
                continue
            if cache is not None:
                cache.cache_put(key, title, "", desc[:1500])
            await asyncio.sleep(0.3)
        else:
            title, desc = hit["title"], hit.get("description", "")
        if not title:
            continue
        out.append({
            "company": company,
            "title": title,
            "url": f"https://www.metacareers.com/jobs/{jid}/",
            "location": "",
            "posted_at": mod,
            "external_id": jid,
            "description": desc,
            "source": "meta",
        })
    return out


async def _hydrate_meta_job(client: httpx.AsyncClient, jid: str) -> tuple[str, str]:
    url = f"https://www.metacareers.com/jobs/{jid}/"
    r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True)
    r.raise_for_status()
    m = _JSONLD_RE.search(r.text)
    title = html.unescape(m.group("title")) if m else ""
    if not title:
        m2 = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
        title = html.unescape(m2.group(1)) if m2 else ""
        if title.strip().lower() == "meta careers":
            title = ""
    d = _META_DESC_RE.search(r.text)
    desc = html.unescape(d.group(1)) if d else ""
    return title, desc


# --------------------------------------------------------------------------
# Jane Street - their open-roles page is JS-rendered, but the page's JS pulls
# from /jobs/main.json, which is the whole role list with ids, cities and
# overviews. Titles come back homoglyph-obfuscated; deobfuscate() fixes that.
# The season lives in `availability` ("Summer Internship"), not the title, so
# we fold it in - otherwise the intern gate never fires.
# --------------------------------------------------------------------------

async def fetch_janestreet(client: httpx.AsyncClient, company: str = "Jane Street") -> list[dict]:
    r = await client.get("https://www.janestreet.com/jobs/main.json",
                         headers=JSON_HEADERS, follow_redirects=True)
    r.raise_for_status()
    out = []
    for j in r.json():
        position = deobfuscate(j.get("position", ""))
        availability = j.get("availability", "")
        title = f"{position} - {availability}" if availability else position
        jid = str(j.get("id", ""))
        out.append({
            "company": company,
            "title": title,
            "url": f"https://www.janestreet.com/join-jane-street/position/{jid}/",
            "location": j.get("city", ""),
            "posted_at": "",
            "external_id": jid,
            "description": strip_html(j.get("overview")),
            "source": "janestreet",
        })
    return out


# ==========================================================================
# Generic HTML-link watcher - catch-all for boards with no JSON API
# (Jane Street, D. E. Shaw, university portals, etc.)
# It diffs the set of job-detail links on a page. Crude but it never
# silently dies the way a hand-written parser does.
# ==========================================================================

_LINK_RE = re.compile(
    r'href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<text>.{0,300}?)</a>', re.I | re.S
)


async def fetch_html_links(
    client: httpx.AsyncClient,
    company: str,
    url: str,
    link_pattern: str,
    base: str = "",
) -> list[dict]:
    r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True)
    r.raise_for_status()
    pat = re.compile(link_pattern, re.I)
    out, seen = [], set()
    for m in _LINK_RE.finditer(r.text):
        href = m.group("href")
        if not pat.search(href):
            continue
        full = href if href.startswith("http") else (base.rstrip("/") + "/" + href.lstrip("/"))
        if full in seen:
            continue
        seen.add(full)
        # Anchor text when it's real text; otherwise fall back to the slug,
        # which on these boards is a dasherized copy of the title.
        text = html.unescape(_TAG_RE.sub(" ", m.group("text"))).strip()
        text = re.sub(r"\s+", " ", text)
        if len(text) < 8:
            tail = href.rstrip("/").split("/")[-1].split("?")[0]
            text = _slug_to_title(tail)
        out.append({
            "company": company, "title": text, "url": full, "location": "",
            "posted_at": "", "external_id": href, "description": "",
            "source": "html",
        })
    return out


# ==========================================================================
# Dispatch
# ==========================================================================

ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
}


class UnsupportedBoard(RuntimeError):
    """Raised for a board type that has no working public endpoint."""


async def fetch_board(client: httpx.AsyncClient, spec: dict[str, Any], cache=None) -> list[dict]:
    """`spec` is one entry from companies.yaml."""
    ats = spec["ats"]
    name = spec["name"]

    if ats in ADAPTERS:
        return await ADAPTERS[ats](client, name, spec["token"])
    if ats == "workday":
        return await fetch_workday(
            client, name, spec["tenant"], spec["site"],
            spec.get("dc", "wd1"), spec.get("search", "intern"),
        )
    if ats == "eightfold":
        return await fetch_eightfold(client, name, spec["host"], spec["domain"],
                                     spec.get("search", "intern"))
    if ats == "amazon":
        return await fetch_amazon(client, name, spec.get("search", "intern"))
    if ats == "apple":
        return await fetch_apple(client, name, spec.get("search", "intern"))
    if ats == "google":
        return await fetch_google(client, name, spec.get("search", "intern"))
    if ats == "meta":
        return await fetch_meta(client, name, cache=cache,
                                max_hydrate=spec.get("max_hydrate", 25))
    if ats == "janestreet":
        return await fetch_janestreet(client, name)
    if ats == "html":
        return await fetch_html_links(client, name, spec["url"],
                                      spec["link_pattern"], spec.get("base", ""))
    if ats == "microsoft":
        # Microsoft retired gcsservices.careers.microsoft.com (its TLS cert no
        # longer covers the hostname) and moved to an Eightfold tenant whose
        # API answers "Not authorized for PCSX" to anonymous callers. There is
        # currently no public JSON board. Fail loudly rather than pretend.
        raise UnsupportedBoard(
            "Microsoft has no public job API since the apply.careers.microsoft.com "
            "migration; use their email job alerts instead"
        )
    raise ValueError(f"unknown ats type: {ats}")


def board_key(spec: dict) -> str:
    ats = spec["ats"]
    ident = spec.get("token") or spec.get("tenant") or spec.get("url") or spec["name"]
    return f"{ats}:{ident}"
