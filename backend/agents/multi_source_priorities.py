"""UC-17 multi-source priorities plugin.

Replaces the mechanical-rules `SalesHubspotPlugin` as the Sales producer.
Per rep:
  1. Collect Signals from all five sources (HubSpot, Gmail, Slack, Calendar,
     Transcripts).
  2. Correlate cross-source by deal_id → contact_email → company fuzzy.
  3. Load the rep's EOS alignment context (Rocks, Issues, Scorecard,
     To-Dos) — the same block chat injects into the supervisor — and
     pass it to the Gemini judge as system-level "company direction".
  4. Ask the judge which groups belong in front of the rep tomorrow; the
     judge ranks alignment-first but lets genuinely urgent unaligned
     signals through with an `aligned: false` marker.
  5. Translate `include=True` decisions into `Recommendation`s, carrying
     the alignment marker through `evidence` so the UI / audit can show
     why each rec ranked where it did.

A failing source for one rep is recorded and skipped — it does not poison
the rep's other sources or other reps. The plugin returns whatever it got;
the orchestrator's persistence + dedup machinery handles the rest.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, selectinload

from backend.agents.base import Recommendation
from backend.signals import Signal, SignalSource
from backend.signals.calendar import CalendarSignalSource
from backend.signals.correlator import CorrelationGroup, Correlator
from backend.signals.drive import DriveSignalSource
from backend.signals.gmail import GmailSignalSource
from backend.signals.hubspot import HubspotSignalSource
from backend.signals.judge import GeminiJudge, JudgeDecision
from backend.signals.slack import SlackSignalSource
from backend.signals.transcript import TranscriptSignalSource
from rubik_core.agents.alignment_context import (
    active_rock_descriptions_for,
    build_alignment_context,
)
from rubik_core.company.domain import workspace_domains
from rubik_core.database.models import User
from rubik_core.features import feature_allows
from rubik_core.logsafe import safe_error
from rubik_core.priorities.batch import is_eligible_priorities_email

logger = logging.getLogger(__name__)


DEFAULT_LOOKBACK = timedelta(hours=24)

#: Given the roster, returns the emails of people who have left.
DeactivatedLookup = Callable[[Iterable[str]], set[str]]


def _default_sources() -> list[SignalSource]:
    return [
        HubspotSignalSource(),
        GmailSignalSource(),
        SlackSignalSource(),
        CalendarSignalSource(),
        TranscriptSignalSource(),
        DriveSignalSource(),
    ]


class MultiSourcePrioritiesPlugin:
    department = "sales"
    name = "multi_source_priorities"

    def __init__(
        self,
        *,
        sources: Sequence[SignalSource] | None = None,
        correlator: Correlator | None = None,
        judge: GeminiJudge | None = None,
        lookback: timedelta = DEFAULT_LOOKBACK,
        deactivated_lookup: DeactivatedLookup | None = None,
    ):
        self._sources = list(sources) if sources is not None else _default_sources()
        self._correlator = correlator or Correlator()
        self._judge = judge or GeminiJudge()
        self._lookback = lookback
        self._deactivated_lookup = deactivated_lookup

    def run(self, db: Session) -> list[Recommendation]:
        users = _audience(db, deactivated_lookup=self._deactivated_lookup)
        if not users:
            logger.info(
                "[multi_source_priorities] nobody on the roster — nothing to do"
            )
            return []

        now = datetime.now(timezone.utc)
        since = now - self._lookback

        all_groups: list[CorrelationGroup] = []
        alignment_by_user: dict[str, str] = {}
        for user_email in users:
            signals = self._collect_signals(user_email, since=since)
            if not signals:
                continue
            groups = self._correlator.correlate(signals)
            # Boost calendar items whose title matches an active
            # rock. The bump from medium → high is enough to bias the
            # judge's prior without forcing inclusion — the judge still
            # gets the final say.
            rocks = active_rock_descriptions_for(user_email, db)
            if rocks:
                _boost_calendar_signals_matching_rocks(groups, rocks)
            logger.info(
                f"[multi_source_priorities] user={user_email} "
                f"signals={len(signals)} groups={len(groups)} "
                f"active_rocks={len(rocks)}"
            )
            all_groups.extend(groups)
            # Only fetch alignment for users we'll actually judge.
            # `build_alignment_context` swallows its own exceptions and
            # returns "" — the judge handles missing context gracefully.
            alignment_by_user[user_email] = build_alignment_context(user_email, db)

        if not all_groups:
            return []

        decisions = self._judge.judge(
            all_groups,
            alignment_context_by_user=alignment_by_user,
            display_name_by_user=_display_names(db, list(alignment_by_user)),
            now=now,
        )
        groups_by_id = {g.correlation_id: g for g in all_groups}
        return [
            _decision_to_recommendation(d, groups_by_id[d.correlation_id])
            for d in decisions
            if d.include and d.correlation_id in groups_by_id
        ]

    def _collect_signals(self, user_email: str, *, since: datetime) -> list[Signal]:
        out: list[Signal] = []
        for src in self._sources:
            try:
                out.extend(src.fetch(user_email, since=since))
            except Exception as e:
                logger.warning(
                    f"[multi_source_priorities] source={getattr(src, 'name', type(src).__name__)} "
                    f"user={user_email} failed: {e}"
                )
        return out


#: Who gets overnight AI Priorities: a registry feature (Admin → Feature
#: Access), open to everyone in the workspace by default.
FEATURE_KEY = "ai_priorities"


def _default_deactivated(emails: Iterable[str]) -> set[str]:
    # Imported when called: it reaches Slack and Google, and tests stub it.
    from backend.services.directory_status import deactivated_priorities_emails

    return deactivated_priorities_emails(list(emails))


def _audience(
    db: Session, *, deactivated_lookup: DeactivatedLookup | None = None
) -> list[str]:
    """Everyone the ``ai_priorities`` roster admits, as emails (one per user).

    The verdict is ``feature_allows`` on each ``users`` row, so an admin's
    Feature Access row decides it, and a person with no department is on it
    like anyone else while the feature ships open. The briefing these cards
    land in is a copilot surface, so the copilot roster has to admit them too.
    An unreadable verdict for one person leaves that person out.

    "Everyone" means everyone in the domain. The two guards every sibling
    roster applies (``loop_closure.roster``, ``priorities.batch``) come first
    and last, because each person here costs Gmail, Calendar and Drive calls
    through Domain-Wide Delegation every night, and an address with no
    Workspace mailbox fails every one of them with ``invalid_grant``:

    * ``is_eligible_priorities_email`` drops seed and impersonation fixtures,
      and anyone off the company's Workspace domain when one is set.
    * ``deactivated_lookup`` (default: Slack and Google) drops anyone who has
      left. It fails open: an outage reads one leaver's mailbox one more night,
      which is cheaper than reading nobody's.
    """
    domains = workspace_domains()
    out: list[str] = []
    for user in (
        db.query(User)
        .filter(User.is_platform_support.is_(False))
        .options(selectinload(User.roles))
        .all()
    ):
        if not is_eligible_priorities_email(user.email, domains):
            continue
        try:
            if feature_allows("copilot", user, db) and feature_allows(
                FEATURE_KEY, user, db
            ):
                out.append(user.email)
        except Exception as exc:  # noqa: BLE001 — a gate that fails is closed
            logger.warning(
                "[multi_source_priorities] roster check failed for one user: %s",
                safe_error(exc),
            )
    if not out:
        return []

    lookup = deactivated_lookup or _default_deactivated
    try:
        gone = {(e or "").strip().lower() for e in (lookup(list(out)) or ())}
    except Exception as exc:  # noqa: BLE001 — detection must never empty the roster
        logger.warning(
            "[multi_source_priorities] deactivated-user lookup failed; reading "
            "the full roster: %s",
            safe_error(exc),
        )
        return out
    kept = [e for e in out if e.strip().lower() not in gone]
    if len(kept) < len(out):
        logger.info(
            "[multi_source_priorities] %d deactivated user(s) left out",
            len(out) - len(kept),
        )
    return kept


def _display_names(db: Session, emails: list[str]) -> dict[str, str]:
    """email → display name for the judge, so it addresses the reader as
    "you" instead of naming them as a third party in the reason text. Only
    non-empty names are returned; the judge falls back to email otherwise."""
    if not emails:
        return {}
    rows = (
        db.query(User.email, User.name)
        .filter(User.email.in_(emails), User.is_platform_support.is_(False))
        .all()
    )
    return {email: name.strip() for email, name in rows if (name or "").strip()}


def _decision_to_recommendation(
    decision: JudgeDecision, group: CorrelationGroup
) -> Recommendation:
    return Recommendation(
        user_email=decision.user_email,
        type=f"{group.primary_entity_kind}_priority",
        entity_id=group.primary_entity_ref,
        entity_name=_derive_entity_name(group),
        text=decision.reason,
        evidence={
            "aligned": decision.aligned,
            "alignment_note": decision.alignment_note,
        },
        confidence=max(0.0, min(1.0, decision.importance / 5.0)),
        source={"judge": "gemini", "primary_entity_kind": group.primary_entity_kind},
        sources=[s.to_dict() for s in group.signals],
        correlation_id=decision.correlation_id,
        next_action=decision.next_action or None,
    )


def _derive_entity_name(group: CorrelationGroup) -> str | None:
    """First non-empty company_name in any signal's entity_refs; falls back to
    the primary_entity_ref so the UI always has something to render."""
    for sig in group.signals:
        name = sig.entity_refs.get("company_name")
        if name:
            return name
    return group.primary_entity_ref


# ── calendar ↔ rock matching ─────────────────────────────────────


# Minimum word length we consider when matching a calendar title against
# a rock description. "Roadmap" / "Implementation" survive; "the", "and",
# "of", "a" don't. Empirically this keeps the matcher from firing on
# every meeting that contains a common stopword that also lives in a
# rock title.
_FT17_MIN_TOKEN_LEN = 5

# Tokens that pass the length filter but are still too generic to count
# as a real rock signal. Keep small and obvious — adding noise here
# silently hides legitimate matches.
_FT17_STOPWORDS_LONG = frozenset(
    {
        "about",
        "after",
        "again",
        "could",
        "every",
        "first",
        "going",
        "great",
        "their",
        "there",
        "these",
        "thing",
        "those",
        "thru",
        "which",
        "while",
        "would",
        "weekly",
        "review",
        "meeting",
    }
)


def _rock_tokens(rock_description: str) -> set[str]:
    """Tokenize a rock description into the words we'll match calendar
    titles against. Lowercased; long enough to be specific; not in the
    common-stopword set."""
    tokens: set[str] = set()
    current = ""
    for ch in rock_description.lower():
        if ch.isalnum():
            current += ch
        else:
            if (
                len(current) >= _FT17_MIN_TOKEN_LEN
                and current not in _FT17_STOPWORDS_LONG
            ):
                tokens.add(current)
            current = ""
    if len(current) >= _FT17_MIN_TOKEN_LEN and current not in _FT17_STOPWORDS_LONG:
        tokens.add(current)
    return tokens


def _calendar_title_for(signal: Signal) -> str | None:
    """Best-effort extraction of the human-readable event title from a
    calendar signal. Returns None for non-calendar signals so the caller
    can skip cheaply.

    Note: ``payload`` is a free-form dict (defined by the source plugin),
    so we tolerate missing keys rather than asserting a schema."""
    if signal.source != "calendar":
        return None
    title = (
        signal.payload.get("event_title") if isinstance(signal.payload, dict) else None
    )
    if isinstance(title, str) and title.strip():
        return title
    return None


def _boost_calendar_signals_matching_rocks(
    groups: list[CorrelationGroup], rock_descriptions: list[str]
) -> None:
    """When a calendar signal's title shares a meaningful token
    with any active rock, bump its ``severity_hint`` to ``high`` and
    drop a ``ft17_rock_match`` breadcrumb in the payload so the judge
    + downstream UI / audit can see why it was boosted.

    Mutates the signals in place. Idempotent — running twice on the
    same group with the same rock list is a no-op.
    """
    if not rock_descriptions:
        return

    # Precompute the token sets per rock so we only walk each
    # description once even when many groups need matching.
    rock_token_sets: list[tuple[str, set[str]]] = [
        (desc, _rock_tokens(desc)) for desc in rock_descriptions
    ]
    rock_token_sets = [pair for pair in rock_token_sets if pair[1]]
    if not rock_token_sets:
        return

    for group in groups:
        for sig in group.signals:
            title = _calendar_title_for(sig)
            if not title:
                continue
            title_tokens = _rock_tokens(title)
            if not title_tokens:
                continue
            matched_rocks: list[str] = []
            for rock_desc, rock_tokens in rock_token_sets:
                # One shared meaningful token is enough — the goal is
                # "could plausibly relate to a rock", not exact match.
                if title_tokens & rock_tokens:
                    matched_rocks.append(rock_desc)
            if not matched_rocks:
                continue
            # Bias the judge's prior toward including this signal.
            if sig.severity_hint != "high":
                sig.severity_hint = "high"
            # Breadcrumb for the judge prompt + audit log. Mutating the
            # payload dict in place is intentional — the Signal
            # dataclass is dict-backed for `to_dict()`, so this survives
            # the recommendation persistence path.
            if isinstance(sig.payload, dict):
                sig.payload["ft17_rock_match"] = matched_rocks
