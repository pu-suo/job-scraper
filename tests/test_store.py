"""Dedupe store tests. A dedupe bug means either spam or a missed posting."""

import pytest

from radar.store import Store, fingerprint


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def posting(**kw):
    base = {
        "fingerprint": fingerprint("Acme", "SWE Intern", "https://x/1", "REQ1"),
        "company": "Acme", "tier": 1, "title": "SWE Intern",
        "url": "https://x/1", "location": "Seattle, WA", "source": "greenhouse",
        "roles": ["SWE"], "year_signal": "explicit_2027", "confidence": 0.9,
        "posted_at": "",
    }
    base.update(kw)
    return base


# -------------------------------------------------------------- fingerprint

def test_fingerprint_is_stable():
    a = fingerprint("Acme", "SWE Intern", "https://x/1", "REQ1")
    b = fingerprint("Acme", "SWE Intern", "https://x/1", "REQ1")
    assert a == b


def test_fingerprint_prefers_external_id():
    """Same req, retitled - must not fire twice."""
    a = fingerprint("Acme", "SWE Intern", "https://x/1", "REQ1")
    b = fingerprint("Acme", "Software Engineer Intern", "https://x/2", "REQ1")
    assert a == b


def test_fingerprint_normalises_title_punctuation():
    a = fingerprint("Acme", "Software Engineer Intern (Summer 2027)", "https://x/1")
    b = fingerprint("Acme", "Software Engineer Intern - Summer 2027", "https://x/1")
    assert a == b


def test_fingerprint_distinguishes_companies():
    assert (fingerprint("Acme", "SWE Intern", "https://x/1", "REQ1")
            != fingerprint("Other", "SWE Intern", "https://x/1", "REQ1"))


# ------------------------------------------------------------------ records

def test_first_record_is_new(store):
    assert store.record(posting()) is True


def test_duplicate_record_is_not_new(store):
    store.record(posting())
    assert store.record(posting()) is False


def test_recent_returns_records(store):
    store.record(posting())
    rows = store.recent()
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"


def test_recent_filters_by_tier(store):
    store.record(posting())
    store.record(posting(fingerprint="zzz", tier=3, company="Longtail"))
    assert len(store.recent(tier=1)) == 1
    assert len(store.recent(tier=3)) == 2


def test_mark_applied_by_prefix(store):
    p = posting()
    store.record(p)
    assert store.mark_applied(p["fingerprint"][:8]) == 1
    assert store.recent()[0]["applied"] == 1


# ------------------------------------------------------------ notifications

def test_pending_then_notified(store):
    store.record(posting())
    pending = store.pending_notifications()
    assert len(pending) == 1
    store.mark_notified([p["fingerprint"] for p in pending])
    assert store.pending_notifications() == []


def test_pending_filters_by_tier(store):
    store.record(posting())
    store.record(posting(fingerprint="zzz", tier=3))
    assert len(store.pending_notifications(max_tier=1)) == 1


# ------------------------------------------------------------ board health

def test_board_ok_resets_streak(store):
    store.board_fail("greenhouse:acme", "HTTP 500")
    store.board_fail("greenhouse:acme", "HTTP 500")
    store.board_ok("greenhouse:acme", 12)
    row = store.all_board_health()[0]
    assert row["fail_streak"] == 0
    assert row["jobs_last_run"] == 12
    assert row["last_error"] is None


def test_fail_streak_increments(store):
    assert store.board_fail("greenhouse:acme", "HTTP 404") == 1
    assert store.board_fail("greenhouse:acme", "HTTP 404") == 2
    assert store.board_fail("greenhouse:acme", "HTTP 404") == 3


def test_unhealthy_boards_threshold(store):
    for _ in range(4):
        store.board_fail("greenhouse:dead", "HTTP 404")
    store.board_fail("greenhouse:flaky", "timeout")
    assert [r["board_key"] for r in store.unhealthy_boards(min_streak=3)] == ["greenhouse:dead"]


# -------------------------------------------------------------- job cache

def test_cache_roundtrip(store):
    assert store.cache_get("meta:1") is None
    store.cache_put("meta:1", "SWE Intern", "Menlo Park", "body")
    assert store.cache_get("meta:1")["title"] == "SWE Intern"


def test_cache_put_overwrites(store):
    store.cache_put("meta:1", "Old", "", "")
    store.cache_put("meta:1", "New", "", "")
    assert store.cache_get("meta:1")["title"] == "New"


def test_stats(store):
    store.record(posting())
    s = store.stats()
    assert s["total"] == 1 and s["top_tier"] == 1
