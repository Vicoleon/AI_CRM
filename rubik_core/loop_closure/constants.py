"""Vocabularies, labels and thresholds for Loose Ends.

The vocabularies are defined once, next to the table that enforces them
(``rubik_core/database/loop_closure.py``), and re-exported here under the names
the rest of the feature reads. ``thresholds()`` reads ``settings`` at call time
rather than import time, so a test (or an operator tuning week one) can change
a value without re-importing anything.

The user-facing words — kind labels, hints, empty lines, coverage notes — live
here too, so the page, the chat answer and the digest say the same thing.
"""

from __future__ import annotations

from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.loop_closure import (
    LOOP_CLOSE_REASONS,
    LOOP_DISMISS_REASONS,
    LOOP_KINDS,
    LOOP_RUN_STATUSES,
    LOOP_RUN_TRIGGERS,
    LOOP_SOURCES,
    LOOP_STATUSES,
)

KINDS: tuple[str, ...] = LOOP_KINDS  # ("unlogged", "open_loop", "todo")
SOURCES: tuple[str, ...] = LOOP_SOURCES  # ("gmail", "slack", "promise", "hubspot_task")
STATUSES: tuple[str, ...] = LOOP_STATUSES  # ("open", "closed", "dismissed")
CLOSE_REASONS: tuple[str, ...] = LOOP_CLOSE_REASONS
DISMISS_REASONS: tuple[str, ...] = LOOP_DISMISS_REASONS
RUN_STATUSES: tuple[str, ...] = LOOP_RUN_STATUSES
RUN_TRIGGERS: tuple[str, ...] = LOOP_RUN_TRIGGERS

#: "Not a real miss" can also stop flagging the sender, or their whole domain,
#: for this AE.
MUTE_SCOPES: tuple[str, ...] = ("sender", "domain")

#: AI Cost category (``@attribute_to``) — keep in step with
#: ``backend/routers/admin.py::_FEATURE_LABELS``.
COST_FEATURE = "loop_closure"
#: Feature Access keys (``rubik_core/features/registry.py``).
FEATURE_KEY = "loop_closure"
TEAM_FEATURE_KEY = "loop_closure_team"

#: Close reasons that do NOT mean "the loop really got closed": an item open
#: for 60 days (``aged_out``), and a promise or task whose date was moved later
#: (``rescheduled``). Both leave the list, neither is ever counted as fixed.
#: (Not named ``*CLOSE_REASONS``: looseEnds.test.ts reads the full vocabulary
#: from the first tuple with that suffix.)
UNFIXED_REASONS: tuple[str, ...] = ("aged_out", "rescheduled")

#: Close reasons that mean "the loop really got closed".
FIXED_CLOSE_REASONS: tuple[str, ...] = tuple(
    r for r in CLOSE_REASONS if r not in UNFIXED_REASONS
)

#: A ``running`` scan row younger than this blocks a second run.
RUN_LOCK_MINUTES = 60

# ── Coverage states (README §3) ──────────────────────────────────────────────
#: ``partial``: the mailbox was read, but a lane had more threads in the
#: lookback window than one morning reads (``gmail_reader.MAX_THREADS_PER_LANE``),
#: so the oldest of them were not checked.
GMAIL_STATES: tuple[str, ...] = ("ok", "partial", "unavailable")
SLACK_STATES: tuple[str, ...] = ("ok", "not_connected", "unavailable")
HUBSPOT_STATES: tuple[str, ...] = ("ok", "no_owner", "unavailable", "partial")
AI_STATES: tuple[str, ...] = ("ok", "unavailable")

# ── Words ────────────────────────────────────────────────────────────────────

KIND_LABELS: dict[str, str] = {
    "unlogged": "Not in HubSpot",
    "open_loop": "Waiting on your reply",
    "todo": "You said you would",
}

SOURCE_LABELS: dict[str, str] = {
    "gmail": "Gmail",
    "slack": "Slack",
    "promise": f"a promise {PRODUCT_NAME} heard",
    "hubspot_task": "a HubSpot task",
}

DISMISS_REASON_LABELS: dict[str, str] = {
    "not_a_client": "Not a client",
    "automated": "Automated email",
    "internal": "Internal",
    "handled_elsewhere": "Handled somewhere else",
    "not_a_promise": "Not a promise",
    "other": "Other",
}

#: Free-mail domains a whole-domain mute is refused for: muting "@gmail.com"
#: would silence every client who writes from a personal address.
PUBLIC_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
    }
)


def kind_hint(kind: str, limits: dict | None = None) -> str:
    """The one-line explanation under a group's heading."""
    t = limits or thresholds()
    if kind == "unlogged":
        return (
            "You emailed a client, or told Slack about a client conversation, "
            "and there is no email, call, meeting or note for it in HubSpot."
        )
    if kind == "open_loop":
        days = t["open_loop_business_days"]
        return (
            f"A client wrote to you directly and has been waiting {days} business "
            f"day{'s' if days != 1 else ''} or more for an answer."
        )
    grace, undated = t["todo_grace_days"], t["todo_undated_days"]
    return (
        f"Something you said you would do is {grace} or more days overdue: a "
        f"HubSpot task of yours, or a promise {PRODUCT_NAME} heard ({undated} days "
        "after you said it, if it had no date)."
    )


def kind_empty_line(kind: str, limits: dict | None = None) -> str:
    """What an empty group says — a sentence, never a blank."""
    if kind == "todo":
        grace = (limits or thresholds())["todo_grace_days"]
        return f"Nothing you said you would do is {grace} or more days overdue."
    return {
        "unlogged": f"Every client email and call {PRODUCT_NAME} saw is in HubSpot.",
        "open_loop": "No client is waiting on a reply from you.",
    }.get(kind, "Nothing here.")


def internal_domains() -> tuple[str, ...]:
    """Only this workspace's verified domains count as internal."""
    from rubik_core.company.domain import workspace_domains

    return tuple(sorted(workspace_domains()))


def thresholds() -> dict:
    """Global numeric tuning plus this workspace's Scorecard metric.

    ``open_loop_business_days`` / ``todo_grace_days`` / ``todo_undated_days`` /
    ``escalation_days`` are the four the page shows; the rest steer the scan.
    """
    from rubik_core.company.operations import get_operations_config
    from rubik_core.config import settings

    def _int(name: str, default: int, floor: int = 0) -> int:
        try:
            value = int(getattr(settings, name, default))
        except (TypeError, ValueError):
            value = default
        return max(floor, value)

    return {
        "lookback_days": _int("LOOP_CLOSURE_LOOKBACK_DAYS", 14, 1),
        "open_loop_business_days": _int("LOOP_CLOSURE_OPEN_LOOP_BUSINESS_DAYS", 2),
        "log_grace_business_days": _int("LOOP_CLOSURE_LOG_GRACE_BUSINESS_DAYS", 1),
        "todo_grace_days": _int("LOOP_CLOSURE_TODO_GRACE_DAYS", 5),
        "todo_undated_days": _int("LOOP_CLOSURE_TODO_UNDATED_DAYS", 5),
        "task_lookback_days": _int("LOOP_CLOSURE_TASK_LOOKBACK_DAYS", 30, 1),
        "escalation_days": _int("LOOP_CLOSURE_ESCALATION_DAYS", 7, 1),
        "max_age_days": _int("LOOP_CLOSURE_MAX_AGE_DAYS", 60, 1),
        "internal_domains": internal_domains(),
        "scorecard_metric": get_operations_config().loop_closure_scorecard_metric,
    }


def coverage_notes(coverage: dict | None) -> list[str]:
    """One plain sentence per "could not look" state (README §2).

    A state other than ``ok`` is never an all-clear, so each one says what was
    NOT checked and that nothing was closed on its account.
    """
    if not coverage:
        return [
            f"{PRODUCT_NAME} has not scanned your mail yet. The scan runs on weekday "
            "mornings, so this is not an all-clear."
        ]
    if coverage.get("scan") == "failed":
        # The per-AE scan crashed: every source state is "unavailable" and the
        # counts are absent. One sentence says what happened; the per-source
        # sentences would each blame a source that was not the problem.
        return [
            f"{PRODUCT_NAME} could not finish your scan in the last run, so nothing "
            "was added to or cleared from your list. Items already on it are from "
            "earlier scans."
        ]
    notes: list[str] = []
    if coverage.get("gmail") == "unavailable":
        notes.append(
            f"{PRODUCT_NAME} could not read your mailbox in the last scan, so email "
            "was not checked and nothing from email was closed."
        )
    elif coverage.get("gmail") == "partial":
        notes.append(
            "Your mailbox had more email in the last two weeks than "
            f"{PRODUCT_NAME} reads in one morning, so the oldest threads were not "
            "checked and some loose ends may be missing from this list."
        )
    slack = coverage.get("slack")
    if slack == "not_connected":
        notes.append(
            "Slack is not connected, so Slack messages were not checked. Connect "
            "it in Settings → Slack."
        )
    elif slack == "unavailable":
        notes.append(
            "Slack could not be read in the last scan, so Slack messages were not "
            "checked."
        )
    hubspot = coverage.get("hubspot")
    if hubspot == "unavailable":
        notes.append(
            "HubSpot could not be read in the last scan, so nothing new was "
            "flagged as missing from HubSpot and nothing was closed by HubSpot "
            "activity."
        )
    elif hubspot == "no_owner":
        notes.append(
            f"Your {PRODUCT_NAME} email does not match a HubSpot owner, so your "
            "overdue HubSpot tasks were not checked. An admin can fix the match in "
            "HubSpot."
        )
    elif hubspot == "partial":
        notes.append(
            "HubSpot returned only part of its activity history in the last scan, "
            "so some emails could not be checked against it and were not flagged."
        )
    if coverage.get("ai") == "unavailable":
        notes.append(
            "The AI check was unavailable in the last scan, so new client emails "
            "and Slack messages were not added. Items already on your list were "
            "still re-checked."
        )
    try:
        deferred = int(coverage.get("judge_deferred") or 0)
    except (TypeError, ValueError):
        deferred = 0
    if deferred > 0:
        notes.append(
            f"{deferred} more possible loose end{'s were' if deferred != 1 else ' was'} "
            "waiting than the AI check reads in one morning; "
            f"{'they are' if deferred != 1 else 'it is'} checked on the next scans, "
            "so this list may not be complete yet."
        )
    return notes
