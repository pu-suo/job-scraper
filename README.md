# intern-radar

Low-latency monitor for **Summer 2027 SWE / MLE / DE internships**, weighted toward FAANG and big tech.

## Why this beats the shared lists

Simplify, `vanshb03/Summer2027-Internships`, `speedyapply`, Zero2Sudo — they all work the same way: a scraper runs on a cron, commits to a repo or database, and *then* broadcasts to thousands of people simultaneously. By the time you see the Discord ping, so did 4,000 other people, and the recruiter's queue already has 800 applications in it.

This tool skips the middleman. It polls **the companies' own ATS JSON endpoints** — the same URLs their careers pages call from your browser — so a posting reaches you when it goes live, not when an aggregator's next cron fires and everyone gets it at once.

Two lanes make that affordable:

| Lane | Boards | Interval |
|---|---|---|
| **Hot** — tier 0–1 (FAANG, frontier AI labs, top quant) | 68 | **3 min** |
| **Cold** — tier 2–3 (everything else) | 70 | 30 min |

Tier 0–1 hits fire an **instant, loud** notification. Tier 2–3 gets batched into an hourly digest, so you get coverage without your phone becoming a slot machine.

**138 working boards ship configured**, every one verified against its live API on 2026-08-17: 80 Greenhouse, 30 Ashby, 14 Workday, 5 Lever, plus SmartRecruiters, Workable, and custom adapters for Amazon, Apple, Google, Meta, Netflix, and Jane Street.

A representative hot-lane poll: **68 boards, 14,437 postings fetched, 109 matches in 33 seconds.**

---

## Setup (5 minutes)

```bash
pip install -r requirements.txt

# 1. Confirm the boards still answer (they drift constantly)
python -m radar verify

# 2. Set up phone push (see below), then:
python -m radar run -v          # one poll
python -m radar watch           # run forever
```

### Board health is the thing that rots

Tokens in `companies.yaml` were all live as of the date above, but companies switch ATS mid-cycle and a board that silently 404s is exactly the failure that costs you the job. `verify` hits every one and prints a status table:

```
TIER  COMPANY                    ATS                JOBS  MATCH  STATUS
T0    Apple                      apple               116     17  ok
T1    Optiver                    greenhouse          177     16  ok
T1    Citadel                    greenhouse            0      0  disabled (still dead: HTTP 404)
```

Re-run it every couple of weeks, and whenever `python -m radar doctor` shows a board going quiet. For anything broken:

```bash
python -m radar discover citadelsecurities
```

Or read the token off the company's apply-button URL:

| URL you see | Config |
|---|---|
| `boards.greenhouse.io/TOKEN` | `ats: greenhouse, token: TOKEN` |
| `jobs.lever.co/TOKEN` | `ats: lever, token: TOKEN` |
| `jobs.ashbyhq.com/TOKEN` | `ats: ashby, token: TOKEN` |
| `jobs.smartrecruiters.com/TOKEN` | `ats: smartrecruiters, token: TOKEN` |
| `TENANT.wd5.myworkdayjobs.com/en-US/SITE` | `ats: workday, tenant: TENANT, dc: wd5, site: SITE` |

`verify` keeps probing boards marked `enabled: false` and shouts if one starts answering again, so a company reopening its API doesn't go unnoticed.

### Notifications

**ntfy.sh is the fastest path to a phone alert** — no account, no webhook:

1. Install the ntfy app (iOS/Android)
2. Subscribe to a long random topic, e.g. `intern-radar-8f3k2n9qx4`
3. Export the same string — **don't put it in `config.yaml`**:

```bash
export RADAR_NTFY_TOPIC='intern-radar-8f3k2n9qx4'
python -m radar run -v
```

The topic string is your only secret — anyone who guesses it sees your alerts, so make it random. `config.yaml` is tracked by git, so in a public repo writing the topic there publishes it. Setting `RADAR_NTFY_TOPIC` (or `RADAR_DISCORD_WEBHOOK`) turns the channel on automatically and keeps the secret off disk. Put the same values in **Settings → Secrets → Actions** for the scheduled workflow.

Discord, Slack, Telegram, email, and desktop are also supported; see `config.yaml`.

---

## Running it 24/7

Latency is the entire product, so where you run it matters:

- **Best — always-on machine.** A $5/mo VPS, a Raspberry Pi, or an old laptop: `python -m radar watch` gives you the real 3-minute hot loop. Wrap it in `systemd` or `tmux` so it survives reboots.
- **Free fallback — GitHub Actions.** `.github/workflows/radar.yml` is included; add `RADAR_NTFY_TOPIC` (and/or `RADAR_DISCORD_WEBHOOK`) as repo secrets. Two caveats. GitHub's cron minimum is 5 minutes and is routinely delayed 5–15 more under load, so this is backup, not the fast path. And the billing asymmetry matters: Actions minutes are **free and unlimited on public repos**, while a private repo gets 2,000 minutes/month — a `*/5` cron burns roughly 8,600, so on a private repo you'd need to slow the schedule to about every 30 minutes to stay inside the free tier.
- **Laptop-only** works but only catches postings while you're awake and open — which is most of them, since recruiters publish during business hours.

---

## Daily use

```bash
python -m radar list --tier 1        # recent FAANG/tier-1 finds
python -m radar applied b6b1d5e3     # mark one applied (id from `list`)
python -m radar doctor               # which boards are drifting
python -m radar test "SWE Intern Summer 2027"   # tune the filters
```

## Tuning

`config.yaml` → `filters.min_confidence` is the main dial:

- `0.75` — only explicit "Software Engineer Intern, Summer 2027" titles
- `0.45` — **default.** Catches nearly everything real, filters most noise
- `0.20` — firehose; you'll see off-year and ambiguous postings

The matcher is deliberately biased toward false positives. A ping you dismiss in two seconds costs nothing; a missed Amazon SDE req that fills in 48 hours costs you the cycle. It keeps "Summer Internship" postings with no year attached, because during the Aug 2026–Feb 2027 window those are almost always 2027.

It only trusts a year in the *body* when that year is attached to a season ("Summer 2027"). A bare `2026` in a description is nearly always a copyright line, and treating it as the posting's cycle silently throws away live reqs.

Use `radar test` to see exactly why any title passed or failed before you change anything.

---

## The adapters that aren't just "call the JSON API"

Four of the tier-0 targets have no ordinary board, and each needed its own approach:

- **Apple** — query the search API with `"internship"`, never `"intern"`. `"intern"` is treated as too common and hands back the entire 1,900-role corpus (all retail); `"internship"` narrows to ~116 real reqs.
- **Google** — the careers results page is server-rendered, but the anchors wrap nested markup, so titles come from the URL slug (`...-software-engineering-phd-intern-2027`).
- **Meta** — no search API at all. The adapter diffs `metacareers.com/jobs/sitemap.xml` (~840 live reqs, one request) and opens only reqs it has never seen, reading the schema.org `JobPosting` block. It backfills 25 reqs per poll on first run, then settles to a single request per cycle.
- **Jane Street** — the role list lives at `/jobs/main.json`, with titles obfuscated using Lisu codepoints that look identical to Latin capitals (`ꓟachine ꓡearning`). The adapter folds them back, and merges the `availability` field into the title so the season is visible to the matcher.

## Known coverage gaps

Honest list of what this does **not** reach, all left in `companies.yaml` as `enabled: false` so they're documented rather than silently missing:

- **Microsoft — the big one.** They retired `gcsservices.careers.microsoft.com` (its TLS cert no longer covers the hostname) and moved to an Eightfold tenant that answers `"Not authorized for PCSX"` to anonymous callers. There is no public JSON board. Set up Microsoft's own email job alerts as the stopgap.
- **Citadel / Citadel Securities** — own portal, no public JSON board found under any slug tried. Worth checking by hand; they close fast.
- **AMD, Yelp, Redfin, Rivian** (iCIMS) and **Cisco** (Phenom) — neither platform exposes an anonymous JSON search endpoint.
- **Intuit, Qualcomm, Visa, Bloomberg, Goldman Sachs, American Express** — Workday tenants whose site coordinates are stale; every tenant/dc/site combination tried returns 401/422. Fixable if you read the real `TENANT.wdN.myworkdayjobs.com/en-US/SITE` off their careers page.
- **Groq, Retool, Rippling, Weights & Biases, Unity, Snyk, Grammarly, Nutanix, Cohesity, dbt Labs** — client-rendered careers pages, token not in the HTML and not on any of the five public JSON ATSs under any slug tried.

---

## Two honest caveats

**Speed helps, but it isn't the whole game.** Rolling review is real, and being in the first tranche genuinely matters — but the biggest companies mostly batch-review and gate on OA performance and referrals rather than pure submission order. Google in particular opens a 2–4 week window and closes it. Treat this tool as removing one failure mode (never hearing about a posting until it's stale), not as the strategy itself. The hours you save not refreshing GitHub are better spent on referrals and OA prep.

**Be polite with the endpoints.** These are unauthenticated JSON APIs that companies publish for their own careers pages; nothing here bypasses auth or solves CAPTCHAs. But `concurrency: 8` and the built-in jitter and per-page delays exist for a reason. Raising them gets you rate-limited or IP-banned, which is strictly worse than being 30 seconds slower. Workday in particular sits behind Akamai and will drop you if you hammer it.

---

## Layout

```
radar/matching.py   role/year classifier      (50 unit tests)
radar/sources.py    13 ATS adapters           (25 tests against captured payloads)
radar/engine.py     two-lane scheduler, dedupe, notification routing (15 tests)
radar/store.py      SQLite dedupe + board health (17 tests)
radar/notify.py     ntfy / Discord / Slack / Telegram / email / desktop (15 tests)
radar/cli.py        run · watch · verify · discover · doctor · list · applied · test
companies.yaml      162 boards (138 live, 24 documented as dead)
config.yaml         filters, cadence, notification channels
```

```bash
python -m pytest tests/ -q     # 125 passed
```

The suite is fully offline — adapters are tested against captured payload shapes through `httpx.MockTransport` — so it never fails because a job board is down.
