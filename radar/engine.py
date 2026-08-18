"""
The polling engine.

Two-lane scheduling is the whole trick:

  HOT lane  (tier 0-1: FAANG, top AI labs, top quant) polled every ~3 min.
  COLD lane (tier 2-3: everything else)               polled every ~30 min.

You cannot poll 300 boards every three minutes without being rude and getting
rate-limited. You *can* poll the 40 boards you actually care about that often.
That gap - minutes instead of the hours an aggregator takes to scrape, commit,
and broadcast - is the entire competitive advantage.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

from . import notify, sources
from .matching import classify, location_ok
from .store import Store, fingerprint


@dataclass
class PollResult:
    fetched: int = 0
    matched: int = 0
    new: int = 0
    boards_ok: int = 0
    boards_failed: int = 0
    errors: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, config: dict, companies: list[dict], store: Store):
        self.cfg = config
        self.companies = companies
        self.store = store
        f = config.get("filters", {})
        self.allow_offseason = f.get("allow_offseason", False)
        self.allow_unknown_year = f.get("allow_unknown_year", True)
        self.min_confidence = f.get("min_confidence", 0.45)
        self.regions = f.get("allowed_regions") or None
        self.roles_wanted = set(f.get("roles", ["MLE", "SWE", "DE"]))
        self.concurrency = config.get("polling", {}).get("concurrency", 8)
        self.timeout = config.get("polling", {}).get("timeout_seconds", 25)

    # ---------------------------------------------------------------- lanes

    def lane(self, name: str) -> list[dict]:
        if name == "hot":
            return [c for c in self.companies
                    if c.get("tier", 3) <= 1 and c.get("enabled", True)]
        if name == "cold":
            return [c for c in self.companies
                    if c.get("tier", 3) >= 2 and c.get("enabled", True)]
        return [c for c in self.companies if c.get("enabled", True)]

    # ------------------------------------------------------------- fetching

    async def _fetch_one(self, client, spec, sem) -> tuple[dict, list[dict] | None, str]:
        key = sources.board_key(spec)
        async with sem:
            # tiny jitter so 300 boards don't fire on the exact same tick
            await asyncio.sleep(random.uniform(0, 0.4))
            try:
                jobs = await sources.fetch_board(client, spec, cache=self.store)
                self.store.board_ok(key, len(jobs))
                return spec, jobs, ""
            except sources.UnsupportedBoard as e:
                # Not a transient failure - there is no endpoint to retry.
                spec["enabled"] = False
                self.store.board_fail(key, f"unsupported: {e}")
                return spec, None, f"unsupported: {e}"
            except httpx.HTTPStatusError as e:
                err = f"HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                err = f"network: {type(e).__name__}"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            streak = self.store.board_fail(key, err)
            # Auto-quarantine a board that's been dead a long time so it
            # stops burning a slot every cycle.
            if streak >= 20:
                spec["enabled"] = False
            return spec, None, err

    async def poll(self, lane: str = "all", verbose: bool = False) -> PollResult:
        specs = self.lane(lane)
        res = PollResult()
        if not specs:
            return res

        sem = asyncio.Semaphore(self.concurrency)
        limits = httpx.Limits(max_connections=self.concurrency * 2,
                              max_keepalive_connections=self.concurrency)
        async with httpx.AsyncClient(timeout=self.timeout, limits=limits,
                                     follow_redirects=True, http2=False) as client:
            tasks = [self._fetch_one(client, s, sem) for s in specs]
            for coro in asyncio.as_completed(tasks):
                spec, jobs, err = await coro
                if jobs is None:
                    res.boards_failed += 1
                    res.errors.append(f"{spec['name']}: {err}")
                    if verbose:
                        print(f"  x {spec['name']:<24} {err}")
                    continue
                res.boards_ok += 1
                res.fetched += len(jobs)
                hits = self._process(spec, jobs)
                res.matched += hits[0]
                res.new += hits[1]
                if verbose and hits[1]:
                    print(f"  + {spec['name']:<24} {hits[1]} NEW")
        return res

    # ------------------------------------------------------------- matching

    def _process(self, spec: dict, jobs: list[dict]) -> tuple[int, int]:
        matched = new = 0
        tier = spec.get("tier", 3)
        for j in jobs:
            m = classify(
                j.get("title", ""), j.get("description", ""), j.get("location", ""),
                allow_offseason=self.allow_offseason,
                allow_unknown_year=self.allow_unknown_year,
            )
            if not m.matched:
                continue
            if not self.roles_wanted.intersection(m.roles):
                continue
            if m.confidence < self.min_confidence:
                continue
            if not location_ok(j.get("location", ""), allowed_regions=self.regions):
                continue

            matched += 1
            fp = fingerprint(j["company"], j["title"], j.get("url", ""),
                             j.get("external_id", ""))
            rec = {
                "fingerprint": fp, "company": j["company"], "tier": tier,
                "title": j["title"], "url": j.get("url", ""),
                "location": j.get("location", ""), "source": j.get("source", ""),
                "roles": m.roles, "year_signal": m.year_signal,
                "confidence": round(m.confidence, 3),
                "posted_at": str(j.get("posted_at", "")),
            }
            if self.store.record(rec):
                new += 1
        return matched, new

    # --------------------------------------------------------- notification

    def flush_notifications(self, urgent_only: bool = False) -> int:
        """
        Tier 0-1 always fires immediately and loudly.
        Tier 2-3 is held for the digest so your phone isn't a slot machine.
        """
        n_cfg = self.cfg.get("notify", {})
        urgent_max_tier = n_cfg.get("urgent_max_tier", 1)

        pending = self.store.pending_notifications()
        urgent = [dict(r) for r in pending if r["tier"] <= urgent_max_tier]
        rest = [dict(r) for r in pending if r["tier"] > urgent_max_tier]

        sent = 0
        if urgent:
            errs = notify.dispatch(n_cfg, urgent, urgent=True)
            for e in errs:
                print(f"  ! notify error {e}")
            self.store.mark_notified([p["fingerprint"] for p in urgent])
            sent += len(urgent)

        if rest and not urgent_only:
            errs = notify.dispatch(n_cfg, rest, urgent=False)
            for e in errs:
                print(f"  ! notify error {e}")
            self.store.mark_notified([p["fingerprint"] for p in rest])
            sent += len(rest)

        return sent

    # -------------------------------------------------------------- run loop

    async def watch(self, verbose: bool = True) -> None:
        p = self.cfg.get("polling", {})
        hot_s = p.get("hot_interval_seconds", 180)
        cold_s = p.get("cold_interval_seconds", 1800)
        digest_s = p.get("digest_interval_seconds", 3600)

        last_cold = 0.0
        last_digest = time.time()

        print(f"radar watching {len(self.lane('hot'))} hot / "
              f"{len(self.lane('cold'))} cold boards")
        print(f"hot every {hot_s}s, cold every {cold_s}s, digest every {digest_s}s\n")

        while True:
            cycle_start = time.time()
            now = time.time()

            r = await self.poll("hot", verbose=False)
            if verbose:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] hot: {r.fetched} jobs / {r.boards_ok} ok / "
                      f"{r.boards_failed} fail / {r.new} NEW", flush=True)
            if r.new:
                self.flush_notifications(urgent_only=True)

            if now - last_cold >= cold_s:
                rc = await self.poll("cold", verbose=False)
                last_cold = now
                if verbose:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[{ts}] cold: {rc.fetched} jobs / {rc.boards_ok} ok / "
                          f"{rc.boards_failed} fail / {rc.new} NEW", flush=True)
                if rc.new:
                    self.flush_notifications(urgent_only=True)

            if now - last_digest >= digest_s:
                n = self.flush_notifications(urgent_only=False)
                last_digest = now
                if verbose and n:
                    print(f"  digest: {n} posting(s) sent", flush=True)

            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(5.0, hot_s - elapsed))
