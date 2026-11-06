"""UC-45 configuration — churn weights/bands, agent budgets, model routing.

Every tunable lives here so the churn score is auditable and the Deep Research
fan-out has a single wall-clock budget knob (mirroring priorities/config.py's
``per_channel_timeout_seconds``).
"""

from __future__ import annotations

from rubik_core.config import AppMode, settings
from rubik_core.sales.model_config import model_for

# ── Churn scoring ────────────────────────────────────────────────────────────
# Score is 0–100 RISK (higher = more at-risk). Each factor returns points in
# [0, 100]; the final score is the weighted average of the factors that HAVE
# data, so a factor whose evidence is missing (e.g. no renewal date, or Fast
# runs skipping meeting sentiment) is dropped and the remaining weights are
# renormalized — never silently scored as 0. Weights are relative; they do not
# need to sum to 1.
CHURN_WEIGHTS: dict[str, float] = {
    "engagement_recency": 0.22,
    "engagement_trend": 0.18,
    "renewal_exposure": 0.20,
    "relationship_breadth": 0.12,
    "deal_momentum": 0.10,
    "client_status": 0.13,
    "meeting_sentiment": 0.05,  # Deep-only; Fast skips it and renormalizes.
}

# Band thresholds on the 0–100 risk score (spec §4).
CHURN_BANDS: tuple[tuple[int, str], ...] = (
    (40, "Healthy"),  # 0–39
    (70, "Watch"),  # 40–69
    (101, "At-risk"),  # 70–100
)


def band_for_score(score: float) -> str:
    """Map a 0–100 risk score to its band label."""
    for ceiling, label in CHURN_BANDS:
        if score < ceiling:
            return label
    return "At-risk"


# ── Deep Research orchestration ──────────────────────────────────────────────
# One wall-clock budget per channel agent (seconds), the same single knob the
# priorities orchestrator uses as both as_completed(timeout=) and the drain.
DEEP_PER_AGENT_BUDGET_S: float = 90.0
DEEP_ANALYST_BUDGET_S: float = 90.0
DEEP_CRITIC_BUDGET_S: float = 45.0
# Critic quality gate + single-retry contract (mirrors priorities: threshold
# 7.0, max_revisions 1 — one extra analyst call, not an iterate-until loop).
DEEP_CRITIC_THRESHOLD: float = 7.0
DEEP_MAX_REVISIONS: int = 1

# Which channels a Deep run fans out over, in display order.
DEEP_CHANNELS: tuple[str, ...] = ("hubspot", "gmail", "meetings", "docs", "web")

# ── Model routing ────────────────────────────────────────────────────────────
# Read ids from the admin store — never hardcode a model string (docs/ENGINEERING_NOTES.md), and
# never freeze one into a module constant. These were constants until the sales
# model store shipped; a constant is evaluated once at import, so an admin
# swapping a model could not take effect until the next deploy — which defeated
# the point of having a setting at all. They are functions so every Deep run
# resolves the current value.
#
# Defaults are unchanged: channel extractors are cheap (Flash); the analyst
# reasons over everything (Pro); the web agent needs Google Search grounding
# (Flash, the only grounded path today, which is why its dropdown is narrowed).


def model_channel() -> str:
    """Model for the per-channel extractors (HubSpot, Gmail, meetings, docs)."""
    return model_for("xray_channel")


def model_analyst() -> str:
    """Model for the analyst that reasons across every channel digest."""
    return model_for("xray_analyst")


def model_critic() -> str:
    """Model for the critic that scores the analyst draft."""
    return model_for("xray_critic")


def model_web() -> str:
    """Model for the Google-Search-grounded web agent."""
    return model_for("xray_web")


def model_fast_synth() -> str:
    """Model for the instant Fast score on the customer page."""
    return model_for("xray_fast_synth")


# ── Fetch bounds ─────────────────────────────────────────────────────────────
MAX_ENGAGEMENTS_PER_TYPE: int = 200  # per activity object, most-recent first
MAX_CONTACTS: int = 100
MAX_DEALS: int = 200
ACTIVITY_TREND_WINDOW_DAYS: int = 90  # last-90d vs prior-90d comparison

# ── Runner ───────────────────────────────────────────────────────────────────
# POST /deep returns 202 immediately; the frontend polls /runs/{id} (spec §6).
# HOW the work runs is chosen by deep_runner_mode() below — the Deep-Meeting-Prep
# pattern: on GCP a CPU-allocated Cloud Run Job ("cubie-xray-deep"), so the multi-agent
# pipeline reliably finishes (Cloud Run throttles detached in-process threads once no
# request is active). Locally / single warm instance, the module-level thread pool below
# is used (no throttling to worry about). The DEEP_RUN_TTL_MINUTES reaper ensures a
# stalled run never permanently 409-blocks. Fast X-Ray always runs in-request
# (run_in_threadpool).
DEEP_RUNNER_MAX_WORKERS: int = 2

# Name of the Cloud Run Job that runs one deep research task (created in GCP,
# same image as the service). Mirrors Deep Meeting Prep's "cubie-deep-prep" job.
XRAY_DEEP_JOB_NAME: str = "cubie-xray-deep"


def deep_runner_mode() -> str:
    """How a deep run executes — the Deep-Meeting-Prep-grade reliability knob.

    * ``"job"``      — launch a Cloud Run Job (CPU always allocated, so the
      multi-agent pipeline reliably finishes; the prod-correct path, exactly
      like Deep Prep).
    * ``"inprocess"`` — run in the app's thread pool. Correct locally and on a
      single warm instance; on multi-instance Cloud Run without
      ``--no-cpu-throttling`` it can be starved (hence the Job for prod).

    ``settings.XRAY_DEEP_RUNNER`` overrides; ``"auto"`` (default) picks ``job``
    on GCP and ``inprocess`` everywhere else, so local testing "just works".
    """
    mode = str(getattr(settings, "XRAY_DEEP_RUNNER", "auto") or "auto").lower()
    if mode in ("job", "inprocess"):
        return mode
    try:
        return "job" if settings.APP_MODE == AppMode.GCP else "inprocess"
    except Exception:  # noqa: BLE001 - never let config selection crash a run
        return "inprocess"


# A pending/running deep row older than this is treated as dead (crashed or
# CPU-throttled) and no longer 409-blocks a fresh run — a self-healing reaper,
# since a detached run has no other watchdog. Comfortably above the ~5-min deep
# budget so a slow-but-live run is never pre-empted.
DEEP_RUN_TTL_MINUTES: int = 20


def enabled() -> bool:
    """Feature flag — default ON in local/dev, overridable via settings."""
    return bool(getattr(settings, "XRAY_ENABLED", True))
