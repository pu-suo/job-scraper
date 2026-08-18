"""Engine tests: lane splitting, filtering, and notification routing."""

import pytest

from radar import notify
from radar.engine import Engine
from radar.store import Store

CONFIG = {
    "filters": {"roles": ["MLE", "SWE", "DE"], "min_confidence": 0.45,
                "allow_unknown_year": True, "allow_offseason": False},
    "polling": {"concurrency": 4, "timeout_seconds": 10},
    "notify": {"urgent_max_tier": 1, "console": {"enabled": False}},
}

COMPANIES = [
    {"name": "Faang", "ats": "greenhouse", "token": "f", "tier": 0, "enabled": True},
    {"name": "Big", "ats": "greenhouse", "token": "b", "tier": 1, "enabled": True},
    {"name": "Mid", "ats": "greenhouse", "token": "m", "tier": 2, "enabled": True},
    {"name": "Tail", "ats": "greenhouse", "token": "t", "tier": 3, "enabled": True},
    {"name": "Off", "ats": "greenhouse", "token": "o", "tier": 0, "enabled": False},
]


@pytest.fixture()
def engine(tmp_path):
    store = Store(tmp_path / "e.db")
    yield Engine(CONFIG, [dict(c) for c in COMPANIES], store)
    store.close()


def job(title, **kw):
    base = {"company": "Faang", "title": title, "url": "https://x/1",
            "location": "Seattle, WA", "description": "", "external_id": "",
            "posted_at": "", "source": "greenhouse"}
    base.update(kw)
    return base


# --------------------------------------------------------------------- lanes

def test_hot_lane_is_tier_0_and_1(engine):
    assert {c["name"] for c in engine.lane("hot")} == {"Faang", "Big"}


def test_cold_lane_is_tier_2_and_3(engine):
    assert {c["name"] for c in engine.lane("cold")} == {"Mid", "Tail"}


def test_disabled_boards_are_excluded_from_every_lane(engine):
    for lane in ("hot", "cold", "all"):
        assert "Off" not in {c["name"] for c in engine.lane(lane)}


def test_all_lane_covers_enabled_boards(engine):
    assert len(engine.lane("all")) == 4


# ------------------------------------------------------------------ filtering

def test_matching_posting_is_recorded(engine):
    matched, new = engine._process(COMPANIES[0], [job("Software Engineer Intern, Summer 2027")])
    assert (matched, new) == (1, 1)


def test_second_pass_is_not_new(engine):
    spec, jobs = COMPANIES[0], [job("Software Engineer Intern, Summer 2027", external_id="R1")]
    engine._process(spec, jobs)
    assert engine._process(spec, jobs) == (1, 0)


def test_non_matching_posting_is_dropped(engine):
    assert engine._process(COMPANIES[0], [job("Senior Software Engineer")]) == (0, 0)


def test_foreign_location_is_dropped(engine):
    assert engine._process(
        COMPANIES[0],
        [job("Software Engineer Intern, Summer 2027", location="London, UK")]) == (0, 0)


def test_low_confidence_is_dropped(engine):
    """min_confidence 0.45 filters the unyeared, weak-signal postings."""
    engine.min_confidence = 0.95
    assert engine._process(COMPANIES[0], [job("Software Engineer Intern")]) == (0, 0)


def test_unwanted_role_family_is_dropped(engine):
    engine.roles_wanted = {"DE"}
    assert engine._process(COMPANIES[0],
                           [job("Software Engineer Intern, Summer 2027")]) == (0, 0)


def test_tier_is_carried_onto_the_record(engine):
    engine._process(COMPANIES[2], [job("Software Engineer Intern, Summer 2027")])
    assert engine.store.recent()[0]["tier"] == 2


# --------------------------------------------------------------- notifications

@pytest.fixture()
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "dispatch",
                        lambda cfg, postings, urgent: calls.append((urgent, list(postings))) or [])
    return calls


def test_urgent_only_sends_tier_0_1(engine, captured):
    engine._process(COMPANIES[0], [job("Software Engineer Intern, Summer 2027")])
    engine._process(COMPANIES[2], [job("Data Engineer Intern, Summer 2027", company="Mid")])
    sent = engine.flush_notifications(urgent_only=True)
    assert sent == 1
    assert captured[0][0] is True
    assert [p["company"] for p in captured[0][1]] == ["Faang"]


def test_digest_sends_the_rest(engine, captured):
    engine._process(COMPANIES[0], [job("Software Engineer Intern, Summer 2027")])
    engine._process(COMPANIES[2], [job("Data Engineer Intern, Summer 2027", company="Mid")])
    assert engine.flush_notifications(urgent_only=False) == 2
    assert [c[0] for c in captured] == [True, False]


def test_notified_postings_do_not_resend(engine, captured):
    engine._process(COMPANIES[0], [job("Software Engineer Intern, Summer 2027")])
    engine.flush_notifications()
    captured.clear()
    assert engine.flush_notifications() == 0
    assert captured == []


def test_urgent_max_tier_is_configurable(engine, captured):
    engine.cfg = {**CONFIG, "notify": {"urgent_max_tier": 3}}
    engine._process(COMPANIES[3], [job("Software Engineer Intern, Summer 2027", company="Tail")])
    engine.flush_notifications(urgent_only=True)
    assert captured[0][0] is True
