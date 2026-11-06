"""UC-47 configuration — attendee-intelligence budgets, TTL, model routing.

Every tunable lives here (mirroring ``xray/config.py``) so the cost and
wall-clock envelope of person research is auditable from one file. This work runs
inside the same ``asyncio.gather`` as Gmail and the transcript RAG (in the
background build, ``meeting_prep/background.py``), so its
budget is a hard ceiling on how long the reader waits for the brief, not a hope.
"""

from __future__ import annotations

from rubik_core.config import settings
from rubik_core.tenancy.secrets import workspace_secret

# ── Model routing ────────────────────────────────────────────────────────────
# Read ids from settings — never hardcode a model string (docs/ENGINEERING_NOTES.md). Grounded
# search is Flash (the only grounded path in the repo); the persona synthesis is
# also Flash — it summarizes supplied evidence rather than reasoning across a
# whole account, and Pro's cost here would be paid once per attendee per meeting.
MODEL_RESEARCH: str = settings.VERTEX_LLM_MODEL_FLASH
MODEL_PERSONA: str = settings.VERTEX_LLM_MODEL_FLASH
# Measured on the two outside guests of one meeting, one grounded
# call with no retries: model-default thinking 65-183 s (22-23 searches), "low"
# thinking 44-151 s (11-14 searches), "low" plus the search cap below 28-32 s
# (8-9 searches), every variant still tying identity and title. The search
# count is what drives the time; "low" (the lowest level 3.7+ accepts) helps by
# searching less. The work is search-then-summarize, not reasoning.
RESEARCH_THINKING_LEVEL: str = "low"
# Searches the research prompt allows per person (see persona._SEARCH_BUDGET).
MAX_SEARCHES: int = 5

# ── Budgets ──────────────────────────────────────────────────────────────────
# One wall-clock ceiling per attendee (seconds), enforced with asyncio.wait_for.
# Attendees are researched concurrently, so this is also roughly the ceiling the
# whole step adds to a prep build.
#
# Was 35 s when UC-47 shipped on Gemini 3.6 Flash. On 3.8 Flash, uncapped, one
# grounded call takes 44-183 s, so at 35 s every lookup timed out. With the
# search cap above it usually takes 30-45 s, but a run of the same two people
# can still pass 90 s for both, so the ceiling sits well above the typical case.
# The same number is passed to the call as its own HTTP timeout
# (web_search.py), because the client's 120 s default cuts a slow call off and
# retries it from scratch. Builds run in the background (20-minute ceiling),
# so the reader is not watching a spinner for it.
PER_ATTENDEE_BUDGET_S: float = 180.0
# Total ceiling for the step regardless of attendee count. Attendees run
# concurrently, so this only bites when one straggles; it keeps a large external
# invite from holding a build open indefinitely.
TOTAL_BUDGET_S: float = 200.0
# Most external attendees on one invite that get researched, in invite order.
# Beyond this the remainder are skipped (and `skipped_attendees` says so — a
# silent truncation would read as "these people have no LinkedIn presence").
MAX_ATTENDEES: int = 6
# How many activity items survive into the bundle, newest-first.
MAX_ACTIVITY_ITEMS: int = 8

# ── Cache ────────────────────────────────────────────────────────────────────
# A person's role and posting themes move on a scale of weeks, and one prospect
# is prepped by several reps for several meetings, so the cache is SHARED across
# users (like an X-Ray snapshot) rather than per-reader. 14 days keeps a card
# fresh enough to quote in a meeting while cutting the steady-state cost of
# repeat prep on the same account to zero.
CACHE_TTL_DAYS: int = 14
# A row whose research failed is cached for far less time — a transient provider
# outage should not blank an attendee's card for a fortnight.
ERROR_CACHE_TTL_HOURS: int = 6


def enabled() -> bool:
    """Feature flag. Default ON: with no LinkedIn API key configured this runs
    on grounded public-web search, which needs no new credential or vendor."""
    return bool(getattr(settings, "ATTENDEE_INTEL_ENABLED", True))


def workspace_domain() -> str:
    """The company's own email domain. Its people are colleagues, never research
    subjects. The one resolver every domain check reads
    (:func:`rubik_core.company.domain.workspace_domain`); unset, the reader's own
    domain still marks colleagues."""
    from rubik_core.company.domain import workspace_domain as resolve

    return resolve()


def linkedin_api_key() -> str:
    """This workspace's licensed LinkedIn-data API key, or "" when unbound.

    Empty is the normal, supported state — `sources/` falls back to grounded
    public-web search. See ``docs/uc47_attendee_intelligence.md`` for why we do
    not scrape linkedin.com directly.
    """
    return (workspace_secret("linkedin", "data_api_key") or "").strip()


def linkedin_api_base() -> str:
    """Base URL of the licensed provider (Proxycurl-compatible by default)."""
    return str(
        getattr(settings, "LINKEDIN_DATA_API_BASE", "") or "https://nubela.co/proxycurl"
    ).rstrip("/")


def api_timeout_s() -> float:
    return float(getattr(settings, "LINKEDIN_DATA_API_TIMEOUT", 20.0))
