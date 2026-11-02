"""Dataclasses for the multi-agent AI Priorities pipeline.

These shapes flow strictly downstream:

    ChannelSignal (raw, from a single source item)
        ↓ grouped per channel
    ChannelDigest (a channel agent's verdict)
        ↓ merged across channels
    ConsolidatedDigest (summarizer output)
        ↓ ranked
    PriorityCandidate[] (decision-maker output)
        ↓ scored
    CritiqueReport
        ↓ (if needs_revision, decision-maker re-runs once)
    PrioritiesBundle (final, delivered to the supervisor)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _new_id() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChannelKind(str, Enum):
    GMAIL = "gmail"
    CALENDAR = "calendar"
    DRIVE = "drive"
    MEET = "meet"
    HUBSPOT = "hubspot"
    # EOS (Traction) — internal Rocks / To-Dos / Issues / Milestones source.
    EOS = "eos"
    # Offboard handoff — open items a departed teammate left behind, now
    # owned by this user (see rubik_core/offboarding). Deterministic reader
    # over ``offboard_items``; no LLM, no external API.
    INHERITED = "inherited"
    # My To-Dos — the user's own promises ledger (rubik_core/promises).
    # Deterministic reader over ``promises``; no LLM, no external API.
    PROMISES = "promises"
    SLACK = "slack"
    # Stubs for future expansion — agents not yet implemented
    CLICKUP = "clickup"


class PriorityKind(str, Enum):
    """Which meta-rule fired to produce a priority.

    UC-25 keys off this to auto-resolve the right way (e.g. ``PREP_DEBT`` /
    ``UPCOMING_PREP`` clears when meeting prep is run; ``FOLLOW_UP_DEBT``
    clears when an outbound email lands on the same thread).

    Forward-looking kinds (the dreamer's primary output — predict what's
    coming) and the retained backward "debt" kinds (kept, but re-framed as
    forward action and never allowed to claim high urgency without an
    upcoming anchor — see decision_maker + the orchestrator post-filter).
    """

    # ── Forward-looking (predict the next 24–72h) ────────────────────────
    UPCOMING_PREP = "upcoming_prep"  # future meeting needs prep
    APPROACHING_DEADLINE = "approaching_deadline"  # future deadline, no slot

    # ── A dated commitment whose date stopped being believable ───────────
    # Its own kind, not a reuse of APPROACHING_DEADLINE, for two reasons the
    # repo has already paid for once each:
    #   1. APPROACHING_DEADLINE is in FORWARD_PRIORITY_KINDS, so a card whose
    #      only anchor is a close date ALREADY IN THE PAST would be dropped
    #      outright by _drop_past_anchored — and "your close date passed and
    #      the deal is still open" is the single most common case in a
    #      leader's pipeline emails.
    #   2. ONE kind per SITUATION: the UC-25 memory key is (user, entity_kind,
    #      entity_id, priority_kind) and ``resolved`` never expires. A deal is
    #      a long-lived entity that passes through several distinct states, and
    #      sharing a kind means resolving one permanently blacklists the
    #      others for that deal.
    # Deliberately NOT in FORWARD_PRIORITY_KINDS.
    CLOSE_DATE_AT_RISK = "close_date_at_risk"
    PRE_TOUCHPOINT_FOLLOWUP = "pre_touchpoint_followup"  # act before next touch

    # ── Backward "debt" (retained; legacy + re-framed forward) ───────────
    FOLLOW_UP_DEBT = "follow_up_debt"  # action without response
    COMMITMENT_DEBT = "commitment_debt"  # promise without fulfillment
    PREP_DEBT = (
        "prep_debt"  # future obligation, no prep (legacy alias of UPCOMING_PREP)
    )
    SCHEDULING_DEBT = "scheduling_debt"  # deadline, no calendar match
    NEGLECT_DEBT = "neglect_debt"  # high-value, silent state

    # ── EOS (Traction) commitments — internal execution work ─────────────
    # Deliberately kept OUT of FORWARD_PRIORITY_KINDS so an OVERDUE Rock /
    # To-Do / Milestone stays surfaced (overdue = more urgent), never dropped
    # as "past". Auto-resolved by the EOS completion hooks when the underlying
    # entity is marked done/solved (see backend/routers/eos + priorities.memory).
    EOS_TODO_DUE = "eos_todo_due"  # a To-Do due-soon or overdue
    EOS_MILESTONE_DUE = "eos_milestone_due"  # a Rock milestone due-soon/overdue
    EOS_ROCK_ATTENTION = "eos_rock_attention"  # Rock off-track or missing an update
    EOS_ISSUE_OPEN = "eos_issue_open"  # open IDS issue needing resolution

    # ── Out-of-office hand-off ────────────────────────────────────────────
    # "You're out next week and these things land while you're gone." Injected
    # deterministically (see orchestrator._handoff_card), never authored by the
    # decision-maker, because it has a hard external deadline — the day you
    # leave — and a card that arrives after that is worthless.
    OOO_HANDOFF = "ooo_handoff"

    # ── Inherited from a departed teammate ────────────────────────────────
    # "This was X's open commitment; you inherited it when they left." Fed by
    # the ``inherited`` channel (offboard_items rows). Debt-shaped: it never
    # stops mattering because a date passed — the person is not coming back.
    INHERITED = "inherited"

    # ── My To-Dos (the promises ledger) ───────────────────────────────────
    # "You said you would do this, for them, by then." Fed by the ``promises``
    # channel. Debt-shaped like INHERITED: an overdue promise is MORE urgent,
    # never dropped as "past". Auto-resolved when the owner marks it kept or
    # drops it (rubik_core/promises/priority_sync.py).
    PROMISE_DUE = "promise_due"
    # "You said you would do this, and there is no sign it happened." A
    # promise Cubie HEARD (a meeting, an L10, sent email, Slack) whose date has
    # passed — or, undated, that is a few days old — with no evidence it was
    # kept (rubik_core/promises/accountability.py). Its own kind, not
    # PROMISE_DUE, because it is a different situation on the same row: "due
    # Friday" and "Friday came and went" must resolve independently.
    PROMISE_UNKEPT = "promise_unkept"


# Kinds that assert an UPCOMING moment to PREPARE FOR — the orchestrator
# drops these when every cited anchor date is past/elapsed (a "prep for a
# meeting that already happened" is the canonical bug). The backward debts
# are never dropped on date, only down-ranked when they claim high urgency
# with nothing imminent.
#
# PRE_TOUCHPOINT_FOLLOWUP is intentionally NOT here: even though it points at
# a future touchpoint, its resolving ACTION (send the follow-up) stays valid
# after a past meeting, so it is filtered like a debt (kept, down-ranked) —
# never dropped just because its triggering event is behind us.
FORWARD_PRIORITY_KINDS: frozenset[PriorityKind] = frozenset(
    {
        PriorityKind.UPCOMING_PREP,
        PriorityKind.APPROACHING_DEADLINE,
        PriorityKind.PREP_DEBT,
        PriorityKind.SCHEDULING_DEBT,
    }
)


# EOS (Traction) execution kinds — surfaced from the ``eos`` channel. Treated
# as debts (kept, never date-dropped) AND exempt from the temporal backstop's
# "a 'high' needs an upcoming anchor" down-rank: an OVERDUE Rock / To-Do /
# Milestone is MORE urgent, so a past due date must not demote it. See
# orchestrator._drop_past_anchored.
EOS_PRIORITY_KINDS: frozenset[PriorityKind] = frozenset(
    {
        PriorityKind.EOS_TODO_DUE,
        PriorityKind.EOS_MILESTONE_DUE,
        PriorityKind.EOS_ROCK_ATTENTION,
        PriorityKind.EOS_ISSUE_OPEN,
    }
)


def is_eos_priority_card(card: dict) -> bool:
    """Recognize persisted EOS cards, including serialized enum values."""
    kind = card.get("priority_kind")
    if isinstance(kind, str) and kind in EOS_PRIORITY_KINDS:
        return True
    if card.get("entity_kind") in {"todo", "milestone", "rock", "issue"}:
        return True
    channels = card.get("source_channels") or []
    return isinstance(channels, (list, tuple, set)) and any(
        isinstance(channel, str) and channel.lower() == "eos" for channel in channels
    )


# Kinds allowed to hold ``high`` urgency without an upcoming anchor date the
# temporal backstop can see. The EOS kinds qualify because overdue is *more*
# urgent; OOO_HANDOFF qualifies because it's injected rather than derived from
# signals, so it cites no signal for ``_drop_past_anchored`` to read dates off —
# its anchor (the day the user leaves) lives on the card itself, and without
# this exemption every handoff card would silently be demoted to medium.
# INHERITED qualifies like the EOS kinds: an inherited commitment whose due
# date is behind us is MORE urgent (the owner is gone), so the temporal
# backstop must never demote it for lacking a future anchor.
HIGH_URGENCY_EXEMPT_KINDS: frozenset[PriorityKind] = EOS_PRIORITY_KINDS | {
    PriorityKind.OOO_HANDOFF,
    PriorityKind.INHERITED,
    # A promise past its date is a broken promise, not stale news.
    PriorityKind.PROMISE_DUE,
    PriorityKind.PROMISE_UNKEPT,
}


# Kinds the "What's On Your Plate" feature (registry key ``open_commitments``)
# additionally exempts from the high→medium demotion, and ONLY while that
# feature is on for the reader.
#
# The demotion exists so a forward-looking card cannot claim urgency it has no
# date to justify. Applied to backward debt it does something else: it
# guarantees that "the client has been silent for three weeks" can never
# outrank "you have a meeting at 2pm", because only the meeting carries a
# future anchor. A reader then sees the calendar always win, while the thing
# that actually needs a decision sits below it.
#
# The argument is the one already made for PROMISE_DUE above: an obligation
# past its date is not stale news, it is the most urgent thing on the list.
# Kept as a separate, feature-gated set rather than folded into
# HIGH_URGENCY_EXEMPT_KINDS so that a reader whose department is not onboarded
# sees byte-identical ranking to today.
# Kinds whose card actions may NEVER write a terminal state.
#
# Each describes a RECURRING condition on a long-lived entity: it can be true
# in September, be fixed, and be true again in November. `resolved` never
# expires, and reopen_entity_everywhere deliberately SKIPS `manual_resolve`
# evidence to preserve a user's manual completion — so a durable resolve on a
# recurring kind is the one suppression in Cubie that nothing can ever revive.
# One click would permanently blind the reader to that situation on that
# entity. That is the read-filter poisoning that can zero a real feed;
# this states the rule once, by kind.
#
# Both the check mark and the x on these kinds write a BOUNDED SNOOZE instead.
# Wanting it gone for good is what Settings → AI Personalization is for, and
# that list is reversible.
SNOOZE_ONLY_PRIORITY_KINDS: frozenset[PriorityKind] = frozenset(
    {PriorityKind.CLOSE_DATE_AT_RISK}
)


# Kinds that EXIST ONLY for readers on "What's On Your Plate". The
# decision-maker is never offered these unless the feature is on, and the
# orchestrator drops them deterministically if one appears anyway — a prompt
# is a request, not a gate.
#
# Distinct from PLATE_HIGH_URGENCY_EXEMPT_KINDS below: those kinds ship to
# everyone and the feature only changes how they RANK; these do not exist at
# all without it.
PLATE_ONLY_PRIORITY_KINDS: frozenset[PriorityKind] = frozenset(
    {PriorityKind.CLOSE_DATE_AT_RISK}
)


PLATE_HIGH_URGENCY_EXEMPT_KINDS: frozenset[PriorityKind] = frozenset(
    {
        PriorityKind.FOLLOW_UP_DEBT,
        PriorityKind.COMMITMENT_DEBT,
        PriorityKind.NEGLECT_DEBT,
        # A close date that has already passed is the strongest form of this
        # argument: there is no future anchor by definition, and that is
        # precisely what makes it worth saying out loud.
        PriorityKind.CLOSE_DATE_AT_RISK,
    }
)


class EntityKind(str, Enum):
    """Canonical entity types referenced by signals so the decision-maker
    can join signals across channels deterministically.

    The ``entity_id`` convention per kind:
      PERSON     → lowercase email address
      COMPANY    → lowercase domain
      DEAL       → HubSpot deal id (string)
      EVENT      → Google Calendar event id
      THREAD     → Gmail thread id
      DOCUMENT   → Drive file id
      TRANSCRIPT → Meet Recordings document id
    """

    PERSON = "person"
    COMPANY = "company"
    DEAL = "deal"
    EVENT = "event"
    THREAD = "thread"
    DOCUMENT = "document"
    TRANSCRIPT = "transcript"
    # One inherited offboard item (entity_id = offboard_items.id). Its own
    # kind ON PURPOSE: dismiss/resolve memory keys on (kind, id, priority_kind),
    # and keying inherited cards to their item id means resolving one can never
    # blacklist the underlying thread/deal for the successor's OWN future
    # priorities (the read-filter-poisoning shape).
    OFFBOARD_ITEM = "offboard_item"
    # One My To-Dos promise (entity_id = promises.id). Its own kind for the
    # same reason as OFFBOARD_ITEM: keying on the promise row means keeping
    # one can never blacklist the thread / deal / person it was about.
    PROMISE = "promise"
    # EOS (Traction) entities — entity_id is the eos_* row UUID (as a string).
    # MILESTONE's entity_id is the eos_rock_milestones row UUID (now that
    # milestones are a persistent, rock-linked table).
    ROCK = "rock"
    ISSUE = "issue"
    TODO = "todo"
    MILESTONE = "milestone"


@dataclass
class ChannelSignal:
    """One raw observation from a channel (an email, an event, a doc).

    UC-22 added entity tags + a typed ``attrs`` dict. ``metadata`` is kept
    as a backwards-compatible alias for ``attrs`` so any caller still
    writing to ``metadata`` continues to work — both fields point to the
    same dict via ``__post_init__``.
    """

    id: str
    channel: ChannelKind
    title: str
    snippet: str = ""
    link: str | None = None
    timestamp: str | None = None

    # UC-22 — entity reference for cross-channel joins.
    entity_kind: EntityKind | None = None
    entity_id: str | None = None
    entity_label: str | None = None

    # UC-22 — typed per-channel observations (see uc22 spec for the
    # contract per channel). Replaces the old free-form ``metadata``.
    attrs: dict[str, Any] = field(default_factory=dict)

    # Kept for back-compat with pre-UC-22 callers (e.g. tests that still
    # write ``metadata={...}``). Aliased to ``attrs`` after init.
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # If both attrs and metadata supplied, attrs wins; else use whichever
        # is non-empty. Then keep them pointed at the same object.
        if self.attrs and self.metadata and self.attrs is not self.metadata:
            # Caller passed both — prefer attrs but merge metadata in
            # without overwriting.
            for k, v in self.metadata.items():
                self.attrs.setdefault(k, v)
        elif self.metadata and not self.attrs:
            self.attrs = self.metadata
        self.metadata = self.attrs

    @property
    def entity_key(self) -> str | None:
        """Stable join key for cross-channel correlation, e.g. ``"deal:1234"``.

        Returns None when this signal hasn't been tagged with an entity.
        """
        if self.entity_kind is None or not self.entity_id:
            return None
        return f"{self.entity_kind.value}:{self.entity_id}"


@dataclass
class ChannelDigest:
    """A single channel agent's verdict.

    UC-20 had ``highlights`` (a list of items the channel "thought were
    important"). UC-22 removes that — channel agents emit observation
    only, no judgment. ``summary`` is a single factual sentence
    describing what the channel saw, used by the summarizer.

    ``highlights`` is retained as an empty default for back-compat with
    older callers but is no longer populated by the new extractors.
    """

    channel: ChannelKind
    narrative: str = ""
    summary: str = ""  # UC-22 — single-sentence factual description
    highlights: list[str] = field(default_factory=list)  # legacy
    signals: list[ChannelSignal] = field(default_factory=list)
    error: str | None = None
    # An optional personal mailbox failed after being selected for this run.
    # Its address can otherwise reach the summarizer through the error even
    # when it yielded no personal signal.
    private_gmail_context: bool = False
    generated_at: str = field(default_factory=_now_iso)


@dataclass
class ConsolidatedDigest:
    """Summarizer output: one cross-channel view of "what's going on."""

    narrative: str
    cross_channel_themes: list[str] = field(default_factory=list)
    per_channel_summaries: dict[str, str] = field(default_factory=dict)
    all_signals: list[ChannelSignal] = field(default_factory=list)


# ── what a signal's attrs may put on a CARD ──────────────────────────────────
#
# A ChannelSignal's ``attrs`` bag is the channel agent's full working set. A
# card is a different thing: whatever is in here is written to
# ``home_cards.metadata_json``, projected by the read endpoint, and rendered in
# somebody's browser. So the crossing is an ALLOW-LIST, never a passthrough.
#
# Today only My To-Dos cards carry any: a "You said you would…" card is not
# honest without the words, when and where they were said, and what Cubie
# checked before saying it has not happened. Everything in here is written by
# Cubie from the reader's own ledger (an enum, a label, an ISO date, a quote the
# reader said). The card's title reaches it through the ``entity_label`` path
# and must not get a second, unchecked one.
CARD_ATTR_ALLOWLIST: frozenset[str] = frozenset(
    {
        # My To-Dos: when it is due, who is waiting, and where it stands.
        "due_date",
        "counterparty_label",
        "promise_status",
        # Promise accountability: the words, where and when they were said,
        # and what Cubie looked at before saying it has not happened.
        "promise_quote",
        "promise_said_on",
        "promise_where",
        "promise_checked",
        "promise_link",
        # Gmail provenance is stamped by the orchestrator, never trusted from
        # model output. A run that saw personal mail stays private to its owner.
        "gmail_mailbox",
        "gmail_account_id",
        "personal_gmail",
        "private_gmail_context",
    }
)

# Longest string any single allow-listed attr may carry onto a card. Generous
# for the longest real value (a promise quote or the list of what was checked)
# and short enough that a surprise never becomes a wall of
# text in a card, a Slack block, or a Vertex prompt.
CARD_ATTR_MAX_CHARS = 400
# Longest list an allow-listed attr may carry.
CARD_ATTR_MAX_ITEMS = 8


def _clean_card_attr(value: Any) -> Any | None:
    """One attr value, clamped to a JSON-safe scalar (or list of them).

    Returns ``None`` for anything that is not a plain scalar/list-of-scalars —
    a nested dict or object has no renderer, so persisting one would only widen
    what crosses to the browser.
    """
    # ``bool`` is a subclass of ``int``, so the numeric arm covers True/False.
    if isinstance(value, str):
        return value[:CARD_ATTR_MAX_CHARS]
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        items = []
        for item in list(value)[:CARD_ATTR_MAX_ITEMS]:
            if isinstance(item, str):
                items.append(item[:CARD_ATTR_MAX_CHARS])
            elif isinstance(item, (int, float)):
                items.append(item)
        return items
    return None


def filter_card_attrs(attrs: Any) -> dict[str, Any]:
    """The subset of ``attrs`` a card may carry — see CARD_ATTR_ALLOWLIST.

    Applied at BOTH ends of persistence (writing ``metadata_json`` and reading
    it back), so a row written by an older build, edited by hand, or produced
    by some future path that sets ``PriorityCandidate.attrs`` directly still
    cannot put an un-allow-listed key in front of a user. ``None`` values are
    dropped: an absent key and an explicit null read identically to every
    consumer, and dropping them keeps the stored blob small.
    """
    if not isinstance(attrs, dict):
        return {}
    out: dict[str, Any] = {}
    for key in sorted(attrs):
        if key not in CARD_ATTR_ALLOWLIST:
            continue
        cleaned = _clean_card_attr(attrs[key])
        if cleaned is None:
            continue
        out[key] = cleaned
    return out


@dataclass
class PriorityCandidate:
    """A single priority surfaced by the decision-maker."""

    id: str = field(default_factory=_new_id)
    title: str = ""
    why_it_matters: str = ""
    urgency: str = "medium"  # "high" | "medium" | "low"
    source_channels: list[ChannelKind] = field(default_factory=list)
    source_links: list[str] = field(default_factory=list)
    suggested_action: str = ""
    signal_ids: list[str] = field(default_factory=list)

    # UC-24 — which correlation meta-rule fired (used by UC-25 to
    # auto-resolve the right way).
    priority_kind: PriorityKind | None = None

    # UC-24 — the canonical entity this priority is about. Lets UC-25's
    # priority_memory key off (user, entity_kind, entity_id, priority_kind)
    # so the same prep-debt for the same event doesn't refire daily.
    entity_kind: EntityKind | None = None
    entity_id: str | None = None
    entity_label: str | None = None

    # EVERY entity this one card speaks for, when it aggregates several of the
    # same kind — e.g. "Follow up with Jordan on overdue To-Dos" covering both of
    # their overdue to-dos. ``entity_id`` above is only the FIRST of them, and
    # which one lands there is decided by the order the decision-maker happened
    # to cite its signals. That made suppression leak: snoozing the card wrote a
    # row for to-do A, the next run cited to-do B first, and a card identical to
    # the eye came straight back — so a manager who had just Slack-nudged their
    # report saw the nudge "not stick". Actions write a memory row for every id
    # in here, and ``memory.apply_post_filter`` only drops the card when all of
    # them are suppressed, so a genuinely NEW sibling item still resurfaces it.
    # Always includes ``entity_id`` itself; empty for single-entity priorities.
    covered_entity_ids: list[str] = field(default_factory=list)
    # Display-only names for those same entities, in the same order. The Slack
    # follow-up needs to tell the recipient WHICH of their items are being
    # chased — ``title``/``why_it_matters`` are written for the manager reading
    # the card ("Jordan Hayes has two overdue To-Dos"), so quoting them at the
    # report reads like they were sent someone else's mail.
    covered_entity_labels: list[str] = field(default_factory=list)
    # Does the READER own each of those same entities — again in the same
    # order, one slot per ``covered_entity_ids`` entry. Siblings are grouped on
    # (entity_kind, priority_kind) alone, so ownership is NOT part of the
    # grouping key and one card can legitimately mix the reader's item with a
    # colleague's. The card-level answer is derived from the PRIMARY entity's
    # ``target_email``, which says nothing about a sibling.
    # ``None`` in a slot means the channel does not decide ownership; it means
    # UNKNOWN, and must never be flattened into False. Empty for
    # single-entity cards, exactly like the two lists above.
    covered_entity_is_own: list[bool | None] = field(default_factory=list)

    # The SERIES this entity belongs to, when it has one. A recurring calendar
    # event gives every occurrence its own instance id
    # (``<seriesId>_20260803T160000Z``), so a dismissal keyed on ``entity_id``
    # only ever silences the occurrence in front of you — a "no preparation is
    # needed for this meeting in the future" would come back the next week.
    # Stamped from the cited calendar signal's ``recurring_event_id`` so the
    # user can suppress the whole series, and so the post-filter can honor
    # that. None for one-off events and every non-calendar entity.
    series_entity_id: str | None = None

    # For team-scoped items surfaced to a manager/exec (e.g. a direct
    # report's EOS to-do): who the item BELONGS to, so the card can offer a
    # "Slack follow-up" action targeting that person. None for the user's own
    # items. ``target_email`` is a lowercase user email.
    target_email: str | None = None
    target_label: str | None = None

    # The handful of facts from the PRIMARY cited signal that the card itself
    # renders — see CARD_ATTR_ALLOWLIST above for what may be in here and why
    # it is a closed list. Stamped by the orchestrator's promises re-attach
    # pass (and only there today), filtered again on the way into and out of
    # ``metadata_json``. Empty for every other channel.
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class CritiqueReport:
    """Critic's structured judgment of a decision draft."""

    overall_score: float = 0.0  # 0-10
    needs_revision: bool = False
    missing_signals: list[str] = field(default_factory=list)
    weak_rationales: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    raw_feedback: str = ""
    # True when the critic LLM call failed and no holistic judgment exists.
    # Callers must NOT treat overall_score as a real health metric in that case
    # (only the deterministic UC-24 blocker check ran).
    critic_failed: bool = False


# How a bundle ended up with its priorities list. "no_signals" is the only
# state in which an EMPTY list is legitimate (a genuinely quiet day);
# "empty_with_signals" means the decision stage produced nothing despite
# evidence — the batch treats that as a per-user FAILURE, never a deliverable.
GENERATION_OK = "ok"
GENERATION_NO_SIGNALS = "no_signals"
GENERATION_EMPTY_WITH_SIGNALS = "empty_with_signals"
# The decision stage DID produce items, but the deterministic post-filters
# (temporal backstop, UC-25 "already handled" memory) dropped them all — e.g.
# the user resolved/snoozed everything open. A legitimate empty: persisted and
# delivered, never treated as a generation failure.
GENERATION_FILTERED_EMPTY = "filtered_empty"
# The user is on PTO today (see rubik_core.availability.pto). An empty (or tiny)
# list here is the CORRECT, expected result — the run deliberately suppresses
# routine work-day prep. Like "no_signals"/"filtered_empty" it is a deliverable
# empty, NOT a generation failure: the delivery gates must persist it and the
# Slack/manager paths must treat it as intentional, never fire empty-bundle ops
# alarms. Checked BEFORE the empty-with-signals branch so a full day off never
# looks like a failed generation.
GENERATION_ON_PTO = "on_pto"


@dataclass
class PrioritiesBundle:
    """Final output handed to the supervisor.

    `audit_trail` is the per-stage breadcrumb so we can debug poor runs
    without re-running the whole pipeline.
    """

    user_email: str
    # Server-stamped scope of every HubSpot signal the LLM could read. Cached
    # prose is unavailable unless all these deals remain visible in this
    # workspace. None denotes an untrusted or pre-v2 bundle.
    crm_provenance: dict[str, Any] | None = None
    # True for the whole run whenever any personal Gmail signal entered the
    # model context, including runs that finish with no priority cards.
    private_gmail_context: bool = False
    generated_at: str = field(default_factory=_now_iso)
    priorities: list[PriorityCandidate] = field(default_factory=list)
    headline: str = ""  # one-sentence framing the supervisor can quote
    revision_count: int = 0
    last_critique_score: float | None = None
    audit_trail: list[str] = field(default_factory=list)
    generation_status: str = GENERATION_OK
    # Out-of-office banner payload when the user is on PTO today, else None.
    # Shape: {"on_pto": True, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
    # "return_date": "YYYY-MM-DD"}. Rendered by the home UI + Slack DM.
    pto: dict | None = None
    # Upcoming invites the user never answered, else None. They are barred from
    # producing prep cards (an unanswered invite isn't a commitment) and
    # reported here once instead. Shape: {"unanswered_count": int, "invites":
    # [{"title", "when_local", "link"}]} — `invites` is capped, so it can be
    # shorter than `unanswered_count`. Rendered by the home UI + Slack DM.
    calendar_hygiene: dict | None = None

    # Pre-vacation hand-off brief — set only when the user's next OOO period
    # starts inside the lead window (see availability.handoff). Shape is
    # ``HandoffBrief.as_dict()``: absence dates, days_until, an AI summary, and
    # the itemised list with suggested delegates. Rides on the bundle so the
    # home UI and the Slack DM can render the banner without re-deriving it,
    # exactly like ``pto`` above.
    handoff: dict | None = None


@dataclass
class PrioritiesRequest:
    """Pipeline input."""

    user_email: str
    focus: str | None = None  # optional user intent ("today's deals", etc.)
    max_priorities: int = 5
    # Forward-looking horizon: the dreamer predicts what matters within this
    # many hours. None on tz_name → org default (priorities.temporal).
    lookahead_hours: int = 72
    tz_name: str | None = None
    # "daily" (everyday briefing) or "next_week" (Friday look-ahead run). The
    # orchestrator forwards this to resolve_user_now, which for "next_week"
    # overrides lookahead_hours to span the next calendar week.
    report_kind: str = "daily"
    channels: list[ChannelKind] = field(
        default_factory=lambda: [
            ChannelKind.GMAIL,
            ChannelKind.CALENDAR,
            ChannelKind.MEET,
            ChannelKind.HUBSPOT,
            ChannelKind.DRIVE,
            ChannelKind.EOS,
            # The nightly batch never passes ``channels``, so a channel missing
            # from THIS list silently never runs in production.
            # Open items inherited from departed teammates (offboard handoff).
            # Deterministic + cheap: no-ops for everyone who isn't a successor.
            ChannelKind.INHERITED,
            # The user's own promises (My To-Dos). Deterministic + cheap:
            # no-ops for everyone with an empty ledger or without the feature.
            ChannelKind.PROMISES,
            ChannelKind.SLACK,
        ]
    )
