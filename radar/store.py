"""SQLite-backed dedupe store. The whole point is: never ping you twice."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    fingerprint   TEXT PRIMARY KEY,
    company       TEXT NOT NULL,
    tier          INTEGER NOT NULL DEFAULT 3,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    location      TEXT,
    source        TEXT,
    roles         TEXT,
    year_signal   TEXT,
    confidence    REAL,
    posted_at     TEXT,
    first_seen    REAL NOT NULL,
    notified_at   REAL,
    applied       INTEGER NOT NULL DEFAULT 0,
    dismissed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_first_seen  ON seen(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_notified    ON seen(notified_at);
CREATE INDEX IF NOT EXISTS idx_tier        ON seen(tier);

CREATE TABLE IF NOT EXISTS board_health (
    board_key     TEXT PRIMARY KEY,
    last_ok       REAL,
    last_attempt  REAL,
    last_error    TEXT,
    fail_streak   INTEGER NOT NULL DEFAULT 0,
    jobs_last_run INTEGER NOT NULL DEFAULT 0
);

-- Detail pages we've already hydrated (Meta's sitemap flow). Keeps the
-- steady-state cost of a board that has no search API at one request.
CREATE TABLE IF NOT EXISTS job_cache (
    key         TEXT PRIMARY KEY,
    title       TEXT,
    location    TEXT,
    description TEXT,
    fetched_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    val TEXT
);
"""

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def fingerprint(company: str, title: str, url: str, external_id: str = "") -> str:
    """
    Stable ID for a posting. Uses the external req ID when available (best),
    otherwise company+normalized title+url path. Normalizing the title stops
    "Software Engineer Intern (Summer 2027)" and "Software Engineer Intern -
    Summer 2027" from double-firing.
    """
    t = _PUNCT.sub(" ", (title or "").lower())
    t = _WS.sub(" ", t).strip()
    key = f"{company.lower()}|{external_id}" if external_id else f"{company.lower()}|{t}|{url}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


class Store:
    def __init__(self, path: str | Path = "radar.db"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- postings ---------------------------------------------------------

    def is_new(self, fp: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE fingerprint = ?", (fp,))
        return cur.fetchone() is None

    def record(self, posting: dict) -> bool:
        """Insert if unseen. Returns True if this is a genuinely new posting."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO seen
                    (fingerprint, company, tier, title, url, location,
                     source, roles, year_signal, confidence, posted_at,
                     first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                posting["fingerprint"],
                posting["company"],
                posting.get("tier", 3),
                posting["title"],
                posting["url"],
                posting.get("location", ""),
                posting.get("source", ""),
                json.dumps(posting.get("roles", [])),
                posting.get("year_signal", "unknown"),
                posting.get("confidence", 0.0),
                posting.get("posted_at", ""),
                time.time(),
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def mark_notified(self, fps: Iterable[str]) -> None:
        now = time.time()
        self.conn.executemany(
            "UPDATE seen SET notified_at = ? WHERE fingerprint = ?",
            [(now, fp) for fp in fps],
        )
        self.conn.commit()

    def pending_notifications(self, max_tier: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM seen WHERE notified_at IS NULL AND dismissed = 0"
        args: list = []
        if max_tier is not None:
            q += " AND tier <= ?"
            args.append(max_tier)
        q += " ORDER BY tier ASC, confidence DESC, first_seen DESC"
        return list(self.conn.execute(q, args))

    def recent(self, limit: int = 50, tier: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM seen WHERE dismissed = 0"
        args: list = []
        if tier is not None:
            q += " AND tier <= ?"
            args.append(tier)
        q += " ORDER BY first_seen DESC LIMIT ?"
        args.append(limit)
        return list(self.conn.execute(q, args))

    def mark_applied(self, fp_prefix: str) -> int:
        cur = self.conn.execute(
            "UPDATE seen SET applied = 1 WHERE fingerprint LIKE ?", (fp_prefix + "%",)
        )
        self.conn.commit()
        return cur.rowcount

    def stats(self) -> dict:
        c = self.conn
        row = c.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN tier <= 1 THEN 1 ELSE 0 END) top_tier,
                      SUM(applied) applied,
                      SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END) last_24h
               FROM seen WHERE dismissed = 0""",
            (time.time() - 86400,),
        ).fetchone()
        return dict(row)

    # -- detail-page cache ------------------------------------------------

    def cache_get(self, key: str) -> dict | None:
        r = self.conn.execute(
            "SELECT title, location, description FROM job_cache WHERE key = ?", (key,)
        ).fetchone()
        return dict(r) if r else None

    def cache_put(self, key: str, title: str, location: str = "",
                  description: str = "") -> None:
        self.conn.execute(
            """INSERT INTO job_cache (key, title, location, description, fetched_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 title=excluded.title, location=excluded.location,
                 description=excluded.description, fetched_at=excluded.fetched_at""",
            (key, title, location, description, time.time()),
        )
        self.conn.commit()

    # -- board health -----------------------------------------------------

    def board_ok(self, key: str, n_jobs: int) -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO board_health (board_key, last_ok, last_attempt, last_error,
                                         fail_streak, jobs_last_run)
               VALUES (?,?,?,NULL,0,?)
               ON CONFLICT(board_key) DO UPDATE SET
                 last_ok=excluded.last_ok, last_attempt=excluded.last_attempt,
                 last_error=NULL, fail_streak=0, jobs_last_run=excluded.jobs_last_run""",
            (key, now, now, n_jobs),
        )
        self.conn.commit()

    def board_fail(self, key: str, err: str) -> int:
        now = time.time()
        self.conn.execute(
            """INSERT INTO board_health (board_key, last_ok, last_attempt, last_error,
                                         fail_streak, jobs_last_run)
               VALUES (?,NULL,?,?,1,0)
               ON CONFLICT(board_key) DO UPDATE SET
                 last_attempt=excluded.last_attempt,
                 last_error=excluded.last_error,
                 fail_streak=board_health.fail_streak + 1""",
            (key, now, err[:400]),
        )
        self.conn.commit()
        r = self.conn.execute(
            "SELECT fail_streak FROM board_health WHERE board_key = ?", (key,)
        ).fetchone()
        return r["fail_streak"] if r else 1

    def unhealthy_boards(self, min_streak: int = 3) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM board_health WHERE fail_streak >= ? ORDER BY fail_streak DESC",
                (min_streak,),
            )
        )

    def all_board_health(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM board_health ORDER BY fail_streak DESC, board_key")
        )

    def close(self) -> None:
        self.conn.close()
