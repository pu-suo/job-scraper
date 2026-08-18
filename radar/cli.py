"""Command line interface: python -m radar <command>"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx
import yaml

from . import sources
from .engine import Engine
from .matching import classify
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


def load(config_path: str, companies_path: str) -> tuple[dict, list[dict]]:
    cfg = yaml.safe_load(Path(config_path).read_text())
    raw = yaml.safe_load(Path(companies_path).read_text())
    companies = raw["companies"] if isinstance(raw, dict) else raw
    for c in companies:
        c.setdefault("tier", 3)
        c.setdefault("enabled", True)
    return cfg, companies


# ------------------------------------------------------------------ commands

def cmd_run(args) -> int:
    cfg, companies = load(args.config, args.companies)
    store = Store(cfg.get("database", "radar.db"))
    eng = Engine(cfg, companies, store)

    t0 = time.time()
    res = asyncio.run(eng.poll(args.lane, verbose=args.verbose))
    dt = time.time() - t0

    print(f"\npolled {res.boards_ok + res.boards_failed} boards in {dt:.1f}s")
    print(f"  {res.fetched} postings fetched")
    print(f"  {res.matched} matched filters")
    print(f"  {res.new} NEW")
    if res.boards_failed:
        print(f"  {res.boards_failed} boards failed (run `doctor` for detail)")

    if not args.no_notify:
        sent = eng.flush_notifications()
        if sent:
            print(f"  {sent} notification(s) sent")
    store.close()
    return 0


def cmd_watch(args) -> int:
    cfg, companies = load(args.config, args.companies)
    store = Store(cfg.get("database", "radar.db"))
    eng = Engine(cfg, companies, store)
    try:
        asyncio.run(eng.watch())
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        store.close()
    return 0


def cmd_verify(args) -> int:
    """
    Hit every board once and report which tokens are wrong or dead.
    Run this FIRST, before you trust the list. Board tokens drift; companies
    switch ATS. A silently-404ing board is the failure mode that costs you
    the job, so surface it loudly.
    """
    cfg, companies = load(args.config, args.companies)
    store = Store(cfg.get("database", "radar.db"))
    eng = Engine(cfg, companies, store)

    async def go():
        sem = asyncio.Semaphore(eng.concurrency)
        rows = []
        async with httpx.AsyncClient(timeout=eng.timeout, follow_redirects=True) as client:
            async def one(spec):
                async with sem:
                    try:
                        jobs = await sources.fetch_board(client, spec, cache=store)
                        n_intern = sum(
                            1 for j in jobs
                            if classify(j.get("title", ""), j.get("description", ""),
                                        allow_unknown_year=True).matched
                        )
                        return (spec, len(jobs), n_intern, None)
                    except Exception as e:
                        return (spec, 0, 0, f"{type(e).__name__}: {str(e)[:80]}")
            for c in asyncio.as_completed([one(s) for s in companies]):
                rows.append(await c)
        return rows

    rows = asyncio.run(go())
    rows.sort(key=lambda r: (r[0].get("tier", 3), r[0]["name"].lower()))

    ok = broken = empty = 0
    off_still_broken = off_recovered = 0
    broken_list, empty_list = [], []
    print(f"{'TIER':<5} {'COMPANY':<26} {'ATS':<16} {'JOBS':>6} {'MATCH':>6}  STATUS")
    print("-" * 84)
    for spec, n, n_match, err in rows:
        tier = spec.get("tier", 3)
        # Boards marked `enabled: false` are known-dead and stay out of the
        # health counts - but we still probe them, because the useful signal
        # is one coming back to life.
        if not spec.get("enabled", True):
            if err or n == 0:
                off_still_broken += 1
                status = f"disabled (still dead: {err or 'zero postings'})"
            else:
                off_recovered += 1
                status = f"disabled but ALIVE AGAIN ({n} jobs) - re-enable it"
        elif err:
            broken += 1
            broken_list.append((spec, err))
            status = f"FAIL {err}"
        elif n == 0:
            empty += 1
            empty_list.append(spec)
            status = "EMPTY zero postings (token likely wrong)"
        else:
            ok += 1
            status = "ok"
        print(f"T{tier:<4} {spec['name'][:25]:<26} {spec['ats'][:15]:<16} "
              f"{n:>6} {n_match:>6}  {status}")

    print("-" * 84)
    print(f"{ok} ok / {empty} empty / {broken} broken "
          f"({off_still_broken} known-dead skipped)")
    if off_recovered:
        print(f"{off_recovered} disabled board(s) are answering again - "
              f"flip enabled: true on them\n")
    else:
        print()

    if (broken_list or empty_list) and args.prune:
        bad = {s["name"] for s, _ in broken_list} | {s["name"] for s in empty_list}
        raw = yaml.safe_load(Path(args.companies).read_text())
        lst = raw["companies"] if isinstance(raw, dict) else raw
        for c in lst:
            if c["name"] in bad:
                c["enabled"] = False
        Path(args.companies).write_text(yaml.safe_dump(raw, sort_keys=False))
        print(f"disabled {len(bad)} bad board(s) in {args.companies}")
    elif broken_list or empty_list:
        print("Fix these by finding the correct token (see `python -m radar discover`),")
        print("or re-run with --prune to disable them.")

    store.close()
    return 0


def cmd_discover(args) -> int:
    """
    Given a careers URL or a company slug guess, figure out which ATS a
    company uses and what its board token is. This is how you keep the
    company list alive without hand-editing YAML for an hour.
    """
    guess = args.query.strip().lower()
    guess = re.sub(r"[^a-z0-9\-.]", "", guess)

    candidates = [
        ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{guess}/jobs"),
        ("lever", f"https://api.lever.co/v0/postings/{guess}?mode=json"),
        ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{guess}"),
        ("smartrecruiters", f"https://api.smartrecruiters.com/v1/companies/{guess}/postings?limit=1"),
        ("workable", f"https://www.workable.com/api/accounts/{guess}?details=true"),
    ]

    async def go():
        found = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for ats, url in candidates:
                try:
                    r = await client.get(url, headers={"User-Agent": sources.UA,
                                                       "Accept": "application/json"})
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    if ats == "greenhouse":
                        n = len(data.get("jobs", []))
                    elif ats == "lever":
                        n = len(data)
                    elif ats == "ashby":
                        n = len(data.get("jobs", []))
                    elif ats == "smartrecruiters":
                        n = data.get("totalFound", 0)
                    else:
                        n = len(data.get("jobs", []))
                    if n:
                        found.append((ats, guess, n))
                except Exception:
                    continue
        return found

    found = asyncio.run(go())
    if not found:
        print(f"No public board found for token '{guess}'.")
        print("\nTips for finding the real token:")
        print("  1. Open the company's careers page")
        print("  2. Look at the apply-button URL. The slug you want is in it:")
        print("       boards.greenhouse.io/<TOKEN>        job-boards.greenhouse.io/<TOKEN>")
        print("       jobs.lever.co/<TOKEN>               jobs.ashbyhq.com/<TOKEN>")
        print("       jobs.smartrecruiters.com/<TOKEN>    apply.workable.com/<TOKEN>")
        print("       <TENANT>.wdN.myworkdayjobs.com/en-US/<SITE>   -> ats: workday")
        print("  3. Or view-source and grep for those domains.")
        return 1

    print(f"Found {len(found)} match(es) for '{guess}':\n")
    for ats, token, n in found:
        print(f"  ats: {ats}\n  token: {token}\n  ({n} live postings)\n")
        print("  YAML to paste into companies.yaml:")
        print(f"    - name: {args.query}\n      ats: {ats}\n      token: {token}\n      tier: 2\n")
    return 0


def cmd_doctor(args) -> int:
    cfg, companies = load(args.config, args.companies)
    store = Store(cfg.get("database", "radar.db"))
    rows = store.all_board_health()
    if not rows:
        print("No health data yet - run `python -m radar run` first.")
        store.close()
        return 0

    bad = [r for r in rows if r["fail_streak"] > 0]
    print(f"{len(rows)} boards tracked / {len(bad)} currently failing\n")
    if bad:
        print(f"{'BOARD':<44} {'STREAK':>7}  LAST ERROR")
        print("-" * 90)
        for r in bad:
            print(f"{r['board_key'][:43]:<44} {r['fail_streak']:>7}  {(r['last_error'] or '')[:40]}")
        print()

    quiet = [r for r in rows if r["fail_streak"] == 0 and r["jobs_last_run"] == 0]
    if quiet:
        print(f"{len(quiet)} board(s) returning ZERO postings (token probably stale):")
        for r in quiet[:25]:
            print(f"  {r['board_key']}")
        print()

    s = store.stats()
    print(f"postings tracked: {s['total']} / tier 0-1: {s['top_tier'] or 0} / "
          f"last 24h: {s['last_24h'] or 0} / marked applied: {s['applied'] or 0}")
    store.close()
    return 0


def cmd_list(args) -> int:
    cfg, _ = load(args.config, args.companies)
    store = Store(cfg.get("database", "radar.db"))
    rows = store.recent(limit=args.limit, tier=args.tier)
    if not rows:
        print("Nothing yet.")
        store.close()
        return 0
    for r in rows:
        roles = "/".join(json.loads(r["roles"]))
        age_h = (time.time() - r["first_seen"]) / 3600
        age = f"{age_h:.0f}h" if age_h >= 1 else f"{age_h*60:.0f}m"
        flag = "[applied]" if r["applied"] else "         "
        print(f"{flag} T{r['tier']} [{roles:<7}] {age:>4} ago  {r['company']}: {r['title']}")
        print(f"        {r['url']}")
        print(f"        id={r['fingerprint'][:8]}  {r['location'] or '-'}")
    store.close()
    return 0


def cmd_applied(args) -> int:
    cfg, _ = load(args.config, args.companies)
    store = Store(cfg.get("database", "radar.db"))
    n = store.mark_applied(args.id)
    print(f"marked {n} posting(s) as applied")
    store.close()
    return 0


def cmd_test(args) -> int:
    """Dry-run the matcher against a title so you can tune the filters."""
    m = classify(args.title, args.description or "")
    print(f"title:      {args.title}")
    print(f"matched:    {m.matched}")
    print(f"roles:      {m.roles}")
    print(f"year:       {m.year_signal}")
    print(f"season:     {m.season}")
    print(f"confidence: {m.confidence:.2f}")
    print(f"reasons:    {m.reasons}")
    return 0


# --------------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="radar",
                                description="2027 SWE/MLE/DE internship radar")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--companies", default=str(ROOT / "companies.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="poll once (use this in cron / GitHub Actions)")
    r.add_argument("--lane", choices=["hot", "cold", "all"], default="all")
    r.add_argument("--no-notify", action="store_true")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    w = sub.add_parser("watch", help="run forever with two-lane scheduling")
    w.set_defaults(func=cmd_watch)

    v = sub.add_parser("verify", help="check every board token works")
    v.add_argument("--prune", action="store_true", help="auto-disable broken boards")
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser("discover", help="find a company's ATS + board token")
    d.add_argument("query")
    d.set_defaults(func=cmd_discover)

    doc = sub.add_parser("doctor", help="board health report")
    doc.set_defaults(func=cmd_doctor)

    ls = sub.add_parser("list", help="show recent finds")
    ls.add_argument("--limit", type=int, default=40)
    ls.add_argument("--tier", type=int, default=None)
    ls.set_defaults(func=cmd_list)

    a = sub.add_parser("applied", help="mark a posting applied")
    a.add_argument("id", help="fingerprint prefix from `list`")
    a.set_defaults(func=cmd_applied)

    t = sub.add_parser("test", help="test the matcher on a title")
    t.add_argument("title")
    t.add_argument("--description", default="")
    t.set_defaults(func=cmd_test)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
