"""
Decides whether a raw posting is a Summer-2027 MLE / SWE / DE internship.

Design bias: FALSE POSITIVES ARE CHEAP, FALSE NEGATIVES ARE EXPENSIVE.
Getting one extra ping you dismiss in two seconds costs nothing. Missing an
Amazon SDE intern req that fills in 48 hours costs you the cycle. So the
matcher is deliberately loose on the year signal and strict only where a
mismatch is unambiguous (wrong seniority, wrong function, wrong country).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TARGET_YEAR = 2027

# --------------------------------------------------------------------------
# Role families
# --------------------------------------------------------------------------
# Ordered by specificity: MLE patterns are checked first so that
# "Machine Learning Software Engineer Intern" lands in MLE, not just SWE.

ROLE_PATTERNS: dict[str, list[str]] = {
    "MLE": [
        r"\bmachine[\s\-]?learning\b",
        r"\bML\b(?!\s*(?:ops\s*)?manager)",
        r"\bMLE\b",
        r"\bdeep[\s\-]?learning\b",
        r"\bapplied\s+scientist\b",
        r"\bresearch\s+(?:engineer|scientist)\b",
        r"\bAI\b(?!\s*(?:product|program|policy)\b)",
        r"\bartificial\s+intelligence\b",
        r"\bcomputer\s+vision\b",
        r"\b(?:NLP|LLM|GenAI|generative\s+AI)\b",
        r"\bperception\b",
        r"\brecommend(?:er|ation)\s+system",
        r"\bspeech\b",
        r"\bMLOps\b",
    ],
    "DE": [
        r"\bdata\s+engineer",
        r"\bdata\s+(?:platform|infrastructure|infra)\b",
        r"\banalytics\s+engineer",
        r"\bbig\s+data\b",
        r"\bETL\b",
        r"\bdata\s+warehouse",
        r"\bdata\s+pipeline",
        r"\bbusiness\s+intelligence\s+engineer",
        r"\bBIE\b",
    ],
    "SWE": [
        r"\bsoftware\s+(?:engineer|developer|development)",
        r"\bSDE\b",
        r"\bSWE\b",
        r"\bengineering\s+intern",
        r"\b(?:back[\s\-]?end|front[\s\-]?end|full[\s\-]?stack)\b",
        r"\bsystems?\s+engineer",
        r"\bplatform\s+engineer",
        r"\binfrastructure\s+engineer",
        r"\bembedded\s+software",
        r"\bdistributed\s+systems\b",
        r"\bsite\s+reliability",
        r"\bSRE\b",
        r"\bdeveloper\s+intern",
        r"\bcompiler\b",
        r"\bsecurity\s+engineer",
        r"\bquantitative\s+(?:developer|technologist|researcher)",
        r"\bsoftware\s+design\b",
    ],
}

# --------------------------------------------------------------------------
# Internship signals
# --------------------------------------------------------------------------

INTERN_PATTERNS = [
    r"\bintern(?:ship)?\b",
    r"\bco[\s\-]?op\b",
    r"\bsummer\s+analyst\b",          # quant/finance naming
    r"\bindustrial\s+placement\b",
    r"\bapprentice(?:ship)?\b",
    r"\bstudent\s+(?:program|opportunit)",
    r"\buniversity\s+(?:program|graduate|talent)\b",
    r"\bcampus\b",
]

# The unambiguous subset. "campus" and "university program" are weak signals
# that quant shops also hang on their new-grad reqs.
STRONG_INTERN_PATTERNS = [
    r"\bintern(?:ship)?\b",
    r"\bco[\s\-]?op\b",
    r"\bsummer\s+analyst\b",
    r"\bindustrial\s+placement\b",
]

# "Campus Software Engineer (Full-Time)" and "Campus Full Time 2027" are new
# grad roles, not internships. Only reject when nothing in the title actually
# says intern - plenty of real listings read "Full-Time Summer Internship".
FULLTIME_RE = re.compile(r"\bfull[\s\-]?time\b", re.I)

# Reject outright - wrong seniority or wrong function.
HARD_EXCLUDE = [
    r"\b(?:senior|staff|principal|lead|distinguished|fellow)\b",
    r"\b(?:manager|director|head\s+of|VP|vice\s+president|chief)\b",
    r"\bsales\b", r"\brecruit(?:er|ing)\b", r"\bmarketing\b",
    r"\baccount\s+executive\b", r"\bcustomer\s+success\b",
    r"\bhuman\s+resources\b", r"\bHR\b", r"\blegal\b", r"\bcounsel\b",
    r"\bfinance\s+intern\b", r"\baccounting\b", r"\baudit\b",
    r"\bmechanical\s+engineer", r"\bcivil\s+engineer",
    r"\bchemical\s+engineer", r"\bindustrial\s+engineer",
    r"\bmanufacturing\s+engineer",
    r"\bnurse\b", r"\bteacher\b",
    r"\btechnical\s+program\s+manager\b", r"\bTPM\b",
    # drop the next line if you also want APM roles
    r"\bproduct\s+manage(?:r|ment)\b",
    r"\bMBA\b",                 # "2027 MBA Leadership Development Program Intern"
    r"\bUX\b", r"\bdesigner\b", r"\bproduct\s+design\b",
]

# Soft exclude - usually not what you want, but keep at low priority
# rather than dropping, because titles lie.
SOFT_EXCLUDE = [
    r"\bPhD\b", r"\bdoctoral\b", r"\bpost[\s\-]?doc",
    r"\breturning\s+intern\b", r"\bconversion\b",
]

# --------------------------------------------------------------------------
# Year / season signals
# --------------------------------------------------------------------------

YEAR_RE = re.compile(r"\b(20\d{2})\b")
SUMMER_RE = re.compile(r"\bsummer\b", re.I)
OFFSEASON_RE = re.compile(r"\b(?:fall|winter|spring|autumn)\b", re.I)

# A year that is actually attached to a season, e.g. "Summer 2027" or
# "2027 Summer Internship". Bare years in a body are worthless - every job
# description on earth contains a copyright year - so description-based year
# reasoning only trusts this pattern.
SEASON_YEAR_RE = re.compile(
    r"\b(?:summer|fall|autumn|winter|spring)\s+(?:of\s+)?(20\d{2})\b"
    r"|\b(20\d{2})\s+(?:summer|fall|autumn|winter|spring)\b",
    re.I,
)

# Cycles that are definitively over. Anything anchored only to these is stale.
DEAD_YEARS = {2023, 2024, 2025, 2026}


@dataclass
class MatchResult:
    matched: bool
    roles: list[str] = field(default_factory=list)
    year_signal: str = "unknown"      # explicit_2027 | summer_no_year | unknown | stale
    season: str = "unknown"           # summer | offseason | unknown
    confidence: float = 0.0           # 0..1
    reasons: list[str] = field(default_factory=list)


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _matching_roles(text: str) -> list[str]:
    hits = []
    for role, pats in ROLE_PATTERNS.items():
        if _any(pats, text):
            hits.append(role)
    return hits


def classify_year(text: str) -> tuple[str, str]:
    """Return (year_signal, season) for a piece of text where bare years count."""
    years = {int(y) for y in YEAR_RE.findall(text)}
    is_summer = bool(SUMMER_RE.search(text))
    is_offseason = bool(OFFSEASON_RE.search(text))
    season = "summer" if is_summer else ("offseason" if is_offseason else "unknown")

    if TARGET_YEAR in years:
        return "explicit_2027", season

    # A year is present, and it's only dead cycles -> stale posting.
    if years and years.issubset(DEAD_YEARS):
        return "stale", season

    # Future beyond target (2028+) - not your cycle.
    if years and all(y > TARGET_YEAR for y in years):
        return "stale", season

    if is_summer:
        # "Summer Internship" with no year, posted during the 2027 hiring
        # window, is almost always Summer 2027. Keep it.
        return "summer_no_year", season

    return "unknown", season


def season_years(text: str) -> set[int]:
    """Years that are explicitly attached to a season word."""
    out: set[int] = set()
    for a, b in SEASON_YEAR_RE.findall(text):
        out.add(int(a or b))
    return out


def _year_from_description(description: str) -> tuple[str | None, str]:
    """
    Description-derived year signal. Only season-anchored years are trusted:
    a bare '2026' in a body is nearly always a copyright line or a graduation
    requirement, and treating it as the posting's cycle throws away real
    Summer-2027 reqs.
    """
    body = description[:4000]
    yrs = season_years(body)
    season = "summer" if SUMMER_RE.search(body) else (
        "offseason" if OFFSEASON_RE.search(body) else "unknown")
    if TARGET_YEAR in yrs:
        return "explicit_2027", season
    if yrs and yrs.issubset(DEAD_YEARS):
        return "stale", season
    if yrs and all(y > TARGET_YEAR for y in yrs):
        return "stale", season
    return None, season


def classify(
    title: str,
    description: str = "",
    location: str = "",
    *,
    allow_offseason: bool = False,
    allow_unknown_year: bool = True,
) -> MatchResult:
    """
    Classify a posting. `title` is weighted heavily; `description` is used
    only to rescue year/intern signals that the title omitted.
    """
    title = title or ""
    description = description or ""

    res = MatchResult(matched=False)

    # --- seniority / function gate (title only; descriptions mention
    #     "reports to a senior engineer" etc. and would cause false drops)
    if _any(HARD_EXCLUDE, title):
        res.reasons.append("hard_exclude_title")
        return res

    # --- internship gate
    if not _any(INTERN_PATTERNS, title):
        # Rescue: some boards title it "Software Engineer - University Grad"
        # and only say "internship" in the body.
        if not _any(INTERN_PATTERNS, description[:2000]):
            res.reasons.append("not_an_internship")
            return res
        res.reasons.append("intern_signal_from_description")

    # --- explicit full-time gate
    if FULLTIME_RE.search(title) and not _any(STRONG_INTERN_PATTERNS, title):
        res.reasons.append("explicitly_full_time")
        return res

    # --- role gate
    role_from_desc = False
    roles = _matching_roles(title)
    if not roles:
        roles = _matching_roles(description[:1500])
        if roles:
            role_from_desc = True
            res.reasons.append("role_from_description")
    if not roles:
        res.reasons.append("no_target_role")
        return res
    res.roles = roles

    # --- year / season
    # The title is authoritative. The description only gets a vote when the
    # title is silent, and only for season-anchored years.
    year_signal, season = classify_year(title)
    if year_signal in ("unknown", "summer_no_year"):
        d_signal, d_season = _year_from_description(description)
        if d_signal is not None:
            year_signal = d_signal
            res.reasons.append("year_from_description")
        if season == "unknown" and d_season != "unknown":
            season = d_season
    res.year_signal, res.season = year_signal, season

    if year_signal == "stale":
        res.reasons.append("stale_cycle")
        return res
    if year_signal == "unknown" and not allow_unknown_year:
        res.reasons.append("no_year_signal")
        return res
    if season == "offseason" and not allow_offseason:
        res.reasons.append("offseason")
        return res

    # --- confidence scoring
    conf = 0.35
    if year_signal == "explicit_2027":
        conf += 0.40
    elif year_signal == "summer_no_year":
        conf += 0.15
    if season == "summer":
        conf += 0.10
    if _any(INTERN_PATTERNS, title):
        conf += 0.10
    if "MLE" in roles or "SWE" in roles:
        conf += 0.05
    if role_from_desc:
        # The role came from body text, which on AI-company boards is often
        # boilerplate ("we build AI-powered X") rather than the actual job.
        conf -= 0.10
    if _any(SOFT_EXCLUDE, title):
        conf -= 0.25
        res.reasons.append("soft_exclude")

    res.confidence = max(0.0, min(1.0, conf))
    res.matched = True
    return res


# --------------------------------------------------------------------------
# Location filtering
# --------------------------------------------------------------------------

US_CA_HINTS = [
    "united states", "usa", "u.s.", "canada", "remote",
    # common metros / state codes that appear without a country
    "seattle", "bellevue", "redmond", "new york", "nyc", "san francisco",
    "bay area", "mountain view", "sunnyvale", "palo alto", "menlo park",
    "cupertino", "san jose", "santa clara", "austin", "boston", "cambridge",
    "chicago", "atlanta", "denver", "boulder", "los angeles", "san diego",
    "portland", "pittsburgh", "raleigh", "durham", "dallas", "houston",
    "washington", "arlington", "reston", "toronto", "vancouver", "montreal",
    "waterloo", "ottawa",
]

_STATE_RE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    r"MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
    r"VT|VA|WA|WV|WI|WY|DC|ON|BC|QC|AB)\b"
)


def location_ok(location: str, *, allowed_regions: list[str] | None = None) -> bool:
    """Loose US/Canada/Remote filter. Unknown locations pass (fail-open)."""
    if not location:
        return True
    loc = location.lower()
    if allowed_regions:
        return any(r.lower() in loc for r in allowed_regions)
    if _STATE_RE.search(location):
        return True
    return any(h in loc for h in US_CA_HINTS)
