"""Matcher tests. These encode the decisions that cost you a job if wrong."""

import pytest

from radar.matching import classify, classify_year, location_ok, season_years


# --------------------------------------------------------------- happy path

@pytest.mark.parametrize("title", [
    "Software Engineer Intern, Summer 2027",
    "2027 Summer Internship - Software Engineering",
    "SDE Intern - Summer 2027",
    "Machine Learning Engineer Intern (Summer 2027)",
    "Data Engineer Intern - Summer 2027",
    "Summer 2027 Data Engineering Co-op",
    "Software Development Engineer Internship - Summer 2027",
    "Quantitative Developer Intern - Summer 2027",
])
def test_obvious_targets_match(title):
    m = classify(title)
    assert m.matched, m.reasons
    assert m.year_signal == "explicit_2027"
    assert m.confidence >= 0.75


def test_roles_are_detected():
    assert classify("Machine Learning Intern Summer 2027").roles == ["MLE"]
    assert classify("Data Engineer Intern Summer 2027").roles == ["DE"]
    assert classify("Software Engineer Intern Summer 2027").roles == ["SWE"]


def test_ml_swe_hybrid_gets_both_families():
    m = classify("Machine Learning Software Engineer Intern, Summer 2027")
    assert "MLE" in m.roles and "SWE" in m.roles


# ------------------------------------------------------------- year signals

def test_summer_without_year_is_kept():
    """The whole point: an unyeared 'Summer Internship' is almost surely 2027."""
    m = classify("Software Engineer Intern - Summer Internship")
    assert m.matched
    assert m.year_signal == "summer_no_year"


def test_unknown_year_can_be_required():
    m = classify("Software Engineer Intern", allow_unknown_year=False)
    assert not m.matched
    assert "no_year_signal" in m.reasons


def test_dead_cycle_is_dropped():
    m = classify("Software Engineer Intern, Summer 2025")
    assert not m.matched
    assert "stale_cycle" in m.reasons


def test_future_cycle_is_dropped():
    assert not classify("Software Engineer Intern, Summer 2029").matched


def test_copyright_year_in_body_does_not_make_it_stale():
    """
    Regression: a bare '2026' in body text (copyright line, graduation
    requirement) used to mark a live req stale and silently drop it.
    """
    m = classify(
        "Software Engineering Internship",
        "Join our 12-week program. Copyright 2026 Acme Inc. All rights reserved.",
    )
    assert m.matched, m.reasons
    assert m.year_signal != "stale"


def test_season_anchored_year_in_body_is_trusted():
    m = classify("Software Engineering Internship",
                 "This is our Summer 2027 cohort for engineering students.")
    assert m.matched
    assert m.year_signal == "explicit_2027"


def test_season_anchored_dead_year_in_body_is_stale():
    m = classify("Software Engineering Internship",
                 "Applications for our Summer 2025 program are now open.")
    assert not m.matched
    assert "stale_cycle" in m.reasons


def test_season_years_extraction():
    assert season_years("Summer 2027 Internship") == {2027}
    assert season_years("2027 Summer Analyst") == {2027}
    assert season_years("Copyright 2026 Acme") == set()


def test_classify_year_primitive():
    assert classify_year("Summer 2027 Intern") == ("explicit_2027", "summer")
    assert classify_year("Summer Intern") == ("summer_no_year", "summer")
    assert classify_year("Fall 2024 Intern")[0] == "stale"
    assert classify_year("Intern") == ("unknown", "unknown")


# ---------------------------------------------------------------- offseason

def test_offseason_dropped_by_default():
    m = classify("Software Engineer Intern - Fall 2027")
    assert not m.matched
    assert "offseason" in m.reasons


def test_offseason_kept_when_allowed():
    m = classify("Software Engineer Intern - Fall 2027", allow_offseason=True)
    assert m.matched


# --------------------------------------------------------------- exclusions

@pytest.mark.parametrize("title", [
    "Senior Software Engineer",
    "Engineering Manager, Platform",
    "Sales Development Intern",
    "Marketing Intern Summer 2027",
    "Mechanical Engineering Intern Summer 2027",
    "Product Manager Intern, Summer 2027",
    "Product Management Intern (Summer 2027)",
    "2027 MBA Leadership Development Program (MLDP) Intern",
    "Product Design Intern for AI and Human Agents Platform",
    "UX Designer Intern",
    "Technical Program Manager Intern",
])
def test_hard_excluded_titles(title):
    m = classify(title)
    assert not m.matched
    assert "hard_exclude_title" in m.reasons


def test_non_internship_is_dropped():
    m = classify("Software Engineer, Backend")
    assert not m.matched
    assert "not_an_internship" in m.reasons


def test_wrong_role_family_is_dropped():
    m = classify("Finance Intern, Summer 2027")
    assert not m.matched


def test_phd_soft_excluded_but_still_matches():
    m = classify("Software Engineer PhD Intern, Summer 2027")
    assert m.matched
    assert "soft_exclude" in m.reasons
    assert m.confidence < classify("Software Engineer Intern, Summer 2027").confidence


# ------------------------------------------------- full-time / campus gates

@pytest.mark.parametrize("title", [
    "Campus Full Time 2027 - Software Developer",
    "Campus Software Engineer (Full-Time)",
])
def test_explicit_full_time_campus_roles_dropped(title):
    m = classify(title)
    assert not m.matched
    assert "explicitly_full_time" in m.reasons


def test_full_time_internship_still_matches():
    """'Full-Time Summer Internship' is a real internship, not a new grad req."""
    assert classify("Software Engineer Intern, Full-Time Summer 2027").matched


def test_campus_intern_still_matches():
    assert classify("Campus Software Engineer (Intern)").matched


# ------------------------------------------------------- description rescue

def test_intern_signal_rescued_from_description():
    m = classify("Software Engineer - University Graduate",
                 "This is a summer internship for students graduating in 2028.")
    assert m.matched


def test_role_from_description_is_penalised():
    with_title = classify("Software Engineer Intern")
    from_desc = classify("Production Technician Intern",
                         "We build AI-powered software engineering systems.")
    assert "role_from_description" in from_desc.reasons
    assert from_desc.confidence < with_title.confidence


# ---------------------------------------------------------------- locations

@pytest.mark.parametrize("loc", [
    "Seattle, WA", "New York, NY", "San Francisco Bay Area", "Remote - US",
    "Toronto, ON", "United States", "Austin, TX",
])
def test_us_canada_locations_pass(loc):
    assert location_ok(loc)


@pytest.mark.parametrize("loc", ["London, UK", "Bangalore, India", "Tokyo, JPN"])
def test_foreign_locations_rejected(loc):
    assert not location_ok(loc)


def test_empty_location_fails_open():
    """Unknown location must pass - dropping it would lose real postings."""
    assert location_ok("")


def test_allowed_regions_narrow_the_filter():
    assert location_ok("Seattle, WA", allowed_regions=["Seattle"])
    assert not location_ok("Austin, TX", allowed_regions=["Seattle"])
