"""Decision-maker agent — UC-24 cross-signal correlator.

The decision-maker takes the consolidated digest plus a pre-built
entity index plus the active department playbook and applies the five
domain-agnostic correlation meta-rules to surface priorities.

Five meta-rules (engine, never edited by admins):

  1. Action without response       → follow_up_debt
  2. Commitment without fulfillment → commitment_debt
  3. Future obligation, no prep    → prep_debt
  4. Deadline mention, no calendar → scheduling_debt
  5. High-value entity, silent     → neglect_debt

Department playbook (UC-23) tells the model *what counts as high-value
in this department* — it's the ONLY department-specific text in the
prompt, so the same engine works for Sales, Marketing, Ops, etc.
"""

from __future__ import annotations

import logging

from rubik_core.ai_providers.registry import IncompatibleModelRoute
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.brand import PRODUCT_NAME
from rubik_core.priorities.agents.base import LLMJsonClient
from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.models import (
    ChannelKind,
    ConsolidatedDigest,
    CritiqueReport,
    EntityKind,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.temporal import DEFAULT_LOOKAHEAD_HOURS, TemporalContext

logger = logging.getLogger(__name__)


# How much of each signal's snippet reaches the model. Named because channel
# agents have to write to it: a snippet composed to a larger budget silently
# loses its tail here, and the tail is where an agent naturally puts the
# caveats that stop the model over-claiming.
SIGNAL_SNIPPET_CHARS = 200

# How much of each signal's attrs preview reaches the model. Named for the same
# reason as the snippet budget: channel agents choose their attr ORDER against
# it (see `_format_attrs_preview`).
ATTRS_PREVIEW_CHARS = 500


# Every attr key the decision-maker renders into its prompt, in render order.
# `_format_attrs_preview` is the ONLY place a signal's attrs reach the model, so
# this tuple is the complete list of attr names the prompt may name.
#
# It lives at module level rather than inside that function so a test can hold
# the prompt against it: a rule that names a key the preview never renders is
# an instruction to read something that appears nowhere in the model's input.
ATTRS_PREVIEW_KEYS: tuple[str, ...] = (
    "direction",
    # Which Gmail mailbox supplied a signal. A personal mailbox label must
    # survive model ranking and the deterministic card provenance pass.
    "gmail_mailbox",
    # What the reader's newest message DID (email only). `direction` alone
    # cannot tell "I asked and they went quiet" from "they asked, I answered";
    # these three are how the WHO SPOKE LAST rule below tells them apart.
    "reader_last_message_kind",
    "reader_replied",
    "addressed_to_reader_last_message",
    # Who the item actually belongs to. These come FIRST after direction
    # because misattributing ownership is worse than losing an enrichment
    # detail if the 500-char cap below bites.
    "addressed_to_reader",
    "asks_of_reader",
    "request_owner",
    "counterparties",
    "presumed_dead",
    "thread_idle_days",
    "when_local",
    "mentioned_dates",
    "mentioned_amounts",
    "external_domains",
    "external_attendee_count",
    "stage",
    "stage_age_days",
    "days_since_engagement",
    # The client company's newer logged contact. `days_since_engagement` is the
    # DEAL's own "Last Contacted", and an email to someone at the client who is
    # not on the deal is logged on their company instead — so a deal can read
    # "82 days" while the client was emailed 13 days earlier. Present only
    # when newer than the deal's.
    "client_days_since_engagement",
    "client_name",
    # The date somebody else is relying on. Rule 2 has always named
    # `close_date`, but nothing populated it, so the branch was dead and the
    # key sat on the test's known-unrendered exemption list. Now that the
    # signal source emits it, the model must actually SEE it — "close date
    # 9/30, last client contact 24 days ago" is the whole card.
    "close_date",
    # The plate rules rank deal cards by value and nearness and state whether
    # the date has slipped — none of which the model can do unless it can SEE
    # these. A rule naming an attr the preview never renders is the exact
    # drift this tuple's header comment memorialises.
    "days_to_close",
    "close_date_passed",
    "deal_amount",
    "signal_types",
    "commitments_made_by_user",
    "open_questions",
    # Promise accountability. `promise_priority_kind` is the kind a promises
    # signal MUST take (promise_due vs promise_unkept); the rest is what the
    # "You said you would…" card is written from.
    "promise_priority_kind",
    "promise_said_on",
    "promise_where",
    "promise_checked",
    # Where an EOS item stands, and whose it is. `is_own` decides whether the
    # card tells the reader to act or to follow up with a direct report.
    "status",
    "is_own",
    # The owner's NAME, not their address: the model writes prose, and
    # "check in with Sam" is the sentence we want. Whom to actually message is
    # stamped deterministically downstream from `owner_email`, so the model
    # never needs to see an address.
    "owner_name",
    # Inherited (offboard) items. `departed_name` lets the model keep the
    # "[Inherited from <name>]" attribution; `due` is the item's own clock
    # (a past one makes it MORE urgent — the owner is gone); the two
    # orig_entity keys are how it folds an inherited item together with the
    # forwarded thread / transferred doc appearing in the reader's own
    # channels. `item_kind` says what shape of thing was inherited.
    "departed_name",
    "item_kind",
    "due",
    "orig_entity_kind",
    "orig_entity_id",
)


class DecisionLLMUnavailable(RuntimeError):
    """The decision-maker LLM call failed (after retries).

    Raised instead of silently returning zero priorities so callers can tell
    an LLM outage apart from a genuinely quiet day — an empty briefing
    persisted as success is exactly this ambiguity.
    """


# Stable system-prompt text. Tests assert the meta-rule headings + the
# structural sections survive future edits. ``{product}`` is filled with the
# product name once, at import, so the constant is always fully rendered.
_META_RULES_PROMPT = """\
You are the Decision-Maker agent — the user's forward-looking "dreamer".
Read the TEMPORAL CONTEXT above. Your job is NOT to recap what already
happened; it is to PREDICT what this person must PREPARE FOR or ACT ON in
the next {lookahead}h so nothing important is missed. Rank by imminence ×
consequence of an UPCOMING moment. You do not invent rules; you only fire
the ones below, and you emit at most one priority per (entity, priority_kind).

## FORWARD RULES (these define what belongs on the homepage)

  1. **Upcoming-meeting prep** (`upcoming_prep`)
     A calendar event whose start_iso is in the FUTURE and within the
     lookahead horizon, AND no prep artifact / recent prep thread is
     attached. HARD CONSTRAINT: start_iso MUST be after the TEMPORAL
     CONTEXT time. A meeting that already started or ended is NEVER
     upcoming_prep. A transcript (channel=meet) proves the meeting is
     OVER and can never justify prep — at most it is a
     pre_touchpoint_followup about its action items. When meeting-prep
     has already been run, UC-25 auto-resolves this — do not re-emit it.
     HARD CONSTRAINT: the event's `your_rsvp` must NOT be `needs_action`.
     An invite the user never answered is not a commitment — it is usually
     a call they don't need to be on — and prep for it drowns out the
     meetings they did accept. The user is told about unanswered invites
     separately, as one calendar-hygiene line. (`tentative` DOES prep:
     a maybe still means they may have to show up.)

  2. **Approaching deadline** (`approaching_deadline`)
     A concrete FUTURE date in `mentioned_dates[].resolved` (tense=future),
     or a deal `close_date` in the future, that falls inside the horizon
     AND has no completed work / calendar slot. Ignore any date whose
     tense is past or today-already-elapsed. An email thread with
     `direction=i_sent_last` and `reader_last_message_kind=answer` (or
     `reader_last_message_kind=fyi`) HAS its completed work: the reader's
     reply was the action, and an "ACTION NEEDED by <date>" subject was
     met by replying. Never re-open such a thread as a deadline.

  3. **Follow-up needed before the next touchpoint** (`pre_touchpoint_followup`)
     A past signal shows silence (direction=i_sent_last, thread_idle_days
     >= 2) or an unkept first-person commitment ("I'll send…") AND there is
     an UPCOMING moment for that entity (next meeting, future close_date,
     renewal) that the silence threatens. Frame it as "act now BECAUSE X is
     coming". If there is NO upcoming moment, this is `low` urgency at most.

## RE-FRAMED BACKWARD SIGNALS (kept, but only as forward action)

  4. **Silence threatening a relationship** (`follow_up_debt`)
     Outbound thread with `direction=i_sent_last`,
     `reader_last_message_kind=ask` and `thread_idle_days >= 2`: the
     reader asked for something and nobody has answered. Surface ONLY
     when framed as a concrete next-touch action that cites the coming
     consequence. Pure "you went quiet" with no forward hook is NOT a
     priority. When `reader_last_message_kind=answer` (or
     `reader_last_message_kind=fyi`) the reader CLOSED the loop — they
     answered somebody else's request and nothing is owed to them. That
     thread is handled: it is never follow_up_debt, never a deadline, and
     never a "confirm the plan with stakeholders" nudge. Leave it out.

  5. **Commitment to deliver before next contact** (`commitment_debt`)
     A transcript/thread first-person promise with no fulfilling signal
     since. Frame as "deliver X before your next contact with <entity>".

  6. **High-value entity going cold** (`neglect_debt`)
     A playbook high-value entity (see DEPARTMENT PLAYBOOK) with no signal
     in its idle window — "re-engage before the relationship lapses".
     Lowest urgency unless a future date makes it timely.

## EOS EXECUTION COMMITMENTS (channel=eos — always surface when present)

These are the user's own (or, for a manager/exec, their TEAM's) internal
Traction commitments. They are already curated by the user, so they are
inherently worth surfacing — do NOT filter them out, and an OVERDUE one is
MORE urgent, never omitted because its date is past. When a signal's
`is_own` attr is false the item belongs to a direct report: frame it as a
manager follow-up ("nudge <owner> — …") so the homepage can offer to message
them.

  7. **EOS To-Do due** (`eos_todo_due`)
     A Traction to-do overdue or due soon. Own → "knock out X before <date>";
     a report's → "follow up with <owner> on X (due <date>)".
  8. **EOS Milestone due** (`eos_milestone_due`)
     A Rock milestone overdue/due soon — frame around the parent Rock.
  9. **EOS Rock needs attention** (`eos_rock_attention`)
     A Rock off-track or missing its weekly update — "get <rock> back on
     track / log this week's update".
  10. **EOS Issue open** (`eos_issue_open`)
     An open IDS issue to resolve — frame around resolving it at the next L10.

## INHERITED WORK AND THE READER'S OWN PROMISES (always surface when present)

  11. **Inherited from a departed teammate** (`inherited`)
     A signal from the `inherited` channel: an open item somebody who LEFT the
     company handed to the reader (their named successor). The title already
     carries the attribution ("[Inherited from <name>] …") — keep that prefix
     verbatim in your title so the reader knows at a glance why a stranger's
     work is on their list. These are pre-vetted by the offboard sweep: never
     drop one for being stale (the owner is gone; nobody else will pick it
     up), and treat a past `due` as MORE urgent, not less. When another
     channel shows the same underlying thread/deal/doc (check
     `orig_entity_kind`/`orig_entity_id` in attrs against your other signals),
     fold them into ONE card that cites both. Suggested action: the concrete
     next move that either closes the item or explicitly re-homes it — the
     reader may also mark it done or dismiss it from the card itself.

  12. **A promise the reader made that has not happened** (`promise_unkept`)
     A promises-channel signal with `promise_priority_kind=promise_unkept`:
     {product} heard the reader commit to something (in a meeting, an L10, an
     email they sent or a Slack message — `promise_where`), its date has
     passed (or, undated, a few days have), and the reader's own activity
     shows no sign of it (`promise_checked` names exactly what was looked
     at). These are pre-vetted: surface at least the most consequential one
     whenever any are present, and treat an older one as MORE urgent.
     Write it in the second person, plainly and without scolding:
     title like "You told <who> you'd <thing>" (or "You said you'd <thing>"
     with no counterparty); `why_it_matters` quotes their words from the
     snippet, says when and where (`promise_said_on`, `promise_where`), and
     says "no sign of it in <promise_checked>" — never "you didn't do it",
     because calls and other tools are invisible to {product}. Suggested action:
     do it today, or send <who> a one-line heads-up with a new date. When a
     transcript/thread signal (`commitments_made_by_user`) describes the same
     promise, fold it in: ONE `promise_unkept` card citing both, never a
     separate `commitment_debt`.

## WHOSE ITEM IS IT (check before emitting anything)

You are writing FOR the reader named in READER below, in the second person.
Two mistakes here have shipped to real users; neither is acceptable.

- **Never name the reader as their own counterparty.** For a calendar event,
  the only source for who the meeting is with is the signal's
  `counterparties` attr — it already excludes the reader. Do NOT take the
  name from the event title: an internal 1:1 is titled with BOTH names
  ("Alex / Blair 1:1"), and reading the first one produced "Prepare for your
  1:1 with Alex" *for Alex*. If `counterparties` is empty, describe the
  meeting without naming anyone.
- **A thread you were only copied on is not your action.** Read
  `addressed_to_reader`: `direct` means the reader is on To:; `cc` and `list`
  mean somebody else was asked. When it is `cc`/`list` AND `asks_of_reader`
  is false, do NOT emit an action the reader must perform — either skip the
  thread or frame it as awareness of what `request_owner` owes, at `low`
  urgency. "ACTION NEEDED" in a subject line is the SENDER's emphasis, not
  evidence that the reader is the one being asked.
- **A long-dead deal is stale data, not a deadline.** A HubSpot signal with
  `presumed_dead=true` has been silent for months; the record is probably
  wrong (deal closed, client acquired, licence not renewed), and the CRM just
  never caught up. NEVER cite it as evidence that anything is urgent or
  critical, and never let it raise another item's urgency. At most, surface it
  as a `low`-urgency ask to verify and clean up the record.
- **A deal's silence is not always its client's.** `days_since_engagement`
  counts days since contact was last logged ON THE DEAL. An email to someone
  at the client who is not on the deal is logged on the client's company
  instead, so when `client_days_since_engagement` is present, `client_name`
  heard from us more recently than the deal shows. Never say the client has
  been quiet for longer than `client_days_since_engagement`; give both
  numbers ("nothing logged on this deal in 82 days; <client_name> was last
  contacted 13 days ago"). That newer contact may have been about another
  deal, so it does not make this deal current, and it never undoes
  `presumed_dead`.

## OUTPUT RULES

- Emit 1..N priorities (fewer is fine; N = max_priorities below).
- An EMPTY list is acceptable ONLY when the SIGNALS section is literally
  "(no signals)". When signals exist you MUST return at least one item:
  pick the 1-3 most useful even if they only merit `low` urgency (the
  nearest upcoming meeting to prepare for, the oldest thread awaiting a
  reply, an approaching date). "Nothing qualifies" is never a valid
  outcome for a briefing — the least-bad candidates, honestly ranked, are.
- Each priority MUST cite the supporting signal_ids from the SIGNALS
  list — never invent IDs.
- Each priority MUST set `priority_kind` to one of:
  `upcoming_prep | approaching_deadline | pre_touchpoint_followup | follow_up_debt | commitment_debt | neglect_debt | eos_todo_due | eos_milestone_due | eos_rock_attention | eos_issue_open | inherited | promise_due | promise_unkept{plate_kinds}`.
  Signals from the `inherited` channel take `inherited` — nothing else does.
  Signals from the promises channel take the kind their
  `promise_priority_kind` attr names — `promise_due` (the reader's own
  To-Dos) or `promise_unkept` (a promise {product} heard, with no sign it was
  kept) — and nothing else takes either. If a promises-channel signal and a
  transcript/thread signal describe the SAME commitment, emit ONE priority
  with the promises kind citing both — never a second `commitment_debt` for
  it.
- Each priority MUST set `entity_kind` + `entity_id` to the canonical
  entity the priority is about. Pick the most specific entity available
  (deal > thread > event > person > company). For an EOS item, use the
  entity_kind + entity_id the signal already carries (rock | issue | todo |
  milestone) — copy the entity_id verbatim. For a My To-Dos item use
  `promise` + the signal's entity_id, verbatim.
- `why_it_matters` must cite specific names / amounts / FUTURE dates from
  the signals — never generic adjectives, never a past date as the reason.
- EMAIL THREADS — WHO SPOKE LAST. `direction` says who sent the newest
  message; `reader_last_message_kind` says what the reader's own newest
  message did. `direction=i_sent_last` + `reader_last_message_kind=answer`
  means the reader has already dealt with the thread: never surface it as
  follow_up_debt or approaching_deadline. Only a first-person promise
  inside that reply ("I'll send the deck Friday") may still cite it, as
  commitment_debt. `direction=they_sent_last` with `reader_replied=true`
  and `addressed_to_reader_last_message=list` is a bulk re-send or a
  reminder that landed AFTER the reader answered — a mailing-list echo, not
  a new ask of them.
- When you state how long a thread has gone quiet, read the number straight
  from its `thread_idle_days` attribute — never derive an idle duration from
  a date mentioned in the body. Body dates are extracted text; only
  `thread_idle_days` reflects the newest message actually in the thread.
  EMAIL THREADS ONLY.
- `suggested_action` is a concrete next step the user can take in
  <5 minutes ("reply to confirm Friday", "block 30 min to read deck X").
- When you state a meeting time, copy the signal's `when_local` string
  EXACTLY. Never recompute or reformat a clock time from an ISO/offset
  string (e.g. `start_iso`) — `when_local` is already the reader's local
  wall clock, and re-deriving it drifts the hour into the wrong timezone.
- `urgency`: 'high' = an upcoming moment within 24h; 'medium' = within the
  lookahead horizon / this week; 'low' = no near-term moment. NEVER mark
  'high' without a future-dated anchor inside the horizon. If your only
  evidence is a PAST date, re-frame around an upcoming moment or omit it.

## OUTPUT SHAPE (raw JSON only)

{
  "priorities": [
    {
      "title": "short noun phrase",
      "why_it_matters": "specific 1-2 sentence rationale citing names/amounts/FUTURE dates",
      "urgency": "high" | "medium" | "low",
      "suggested_action": "concrete next step",
      "priority_kind": "upcoming_prep" | "approaching_deadline" | "pre_touchpoint_followup" | "follow_up_debt" | "commitment_debt" | "neglect_debt" | "eos_todo_due" | "eos_milestone_due" | "eos_rock_attention" | "eos_issue_open" | "inherited" | "promise_due" | "promise_unkept"{plate_kinds_json},
      "entity_kind": "person" | "company" | "deal" | "event" | "thread" | "document" | "transcript" | "rock" | "issue" | "todo" | "milestone" | "promise",
      "entity_id": "...",
      "entity_label": "...",
      "signal_ids": ["id1", "id2"]
    }
  ]
}
""".replace("{product}", PRODUCT_NAME)


_GENERIC_PLAYBOOK_FALLBACK = (
    "(No department-specific playbook configured. Prefer "
    "decision-bearing items over open-ended ones; prefer external "
    "counterparties over internal asks; surface anything carrying a "
    "concrete date or dollar amount.)"
)


class DecisionMakerAgent:
    def __init__(self, config: PrioritiesConfig):
        self.config = config
        self.client = LLMJsonClient(
            model_key=config.models.decision_maker,
            temperature=config.temperatures.decision_maker,
            feature="ai_priorities",
        )

    def decide(
        self,
        digest: ConsolidatedDigest,
        *,
        max_priorities: int = 5,
        focus: str | None = None,
        # UC-24 inputs (added in this revision):
        entity_index: dict[str, list[str]] | None = None,
        department: str | None = None,
        playbook_text: str | None = None,
        # UC-25 — already-handled memory for "do not re-surface"
        memory: dict | None = None,
        previous_attempt: list[PriorityCandidate] | None = None,
        critique: CritiqueReport | None = None,
        # Pinned "now" — anchors the forward-looking rules to today.
        temporal: TemporalContext | None = None,
        # WGLL feedback block (recent user ratings on priorities); "" = no-op.
        wgll_text: str | None = None,
        # AI Personalization — the user's role/focus and muted topics.
        about_me: str | None = None,
        muted_topics: list[str] | None = None,
        # Who this briefing is FOR. Named in the prompt so the model can tell
        # the reader apart from the people they're meeting / cc'd alongside.
        reader_email: str | None = None,
        reader_name: str | None = None,
        # Second chance after a zero-result first pass (see orchestrator).
        zero_repair: bool = False,
        # "What's On Your Plate" (registry key open_commitments), resolved
        # per-reader in the orchestrator. Off → the prompt is byte-identical
        # to today's, so a non-onboarded reader's briefing does not move.
        plate_enabled: bool = False,
    ) -> list[PriorityCandidate]:
        prompt = self._build_prompt(
            digest,
            max_priorities=max_priorities,
            focus=focus,
            entity_index=entity_index or {},
            department=department,
            playbook_text=playbook_text or "",
            plate_enabled=plate_enabled,
            memory=memory or {},
            previous_attempt=previous_attempt,
            critique=critique,
            temporal=temporal,
            wgll_text=wgll_text or "",
            about_me=about_me,
            muted_topics=muted_topics or [],
            reader_email=reader_email,
            reader_name=reader_name,
            zero_repair=zero_repair,
        )
        # STRICT call: an LLM/parse failure must surface as a typed error,
        # never as the empty fallback — "the model was down" and "nothing is
        # important today" are different facts and downstream needs to know
        # which one happened. Two attempts absorb transient 429s/truncation.
        try:
            result = self.client.invoke_json(prompt, fallback=None, attempts=2)
        except (ProviderRequestError, IncompatibleModelRoute):
            # A selected workspace provider must fail the run visibly. The
            # orchestrator's managed-Vertex zero-repair/revision fallbacks
            # intentionally catch only DecisionLLMUnavailable.
            raise
        except Exception as exc:  # noqa: BLE001 — normalize to the typed failure
            raise DecisionLLMUnavailable(
                f"decision-maker LLM call failed after retries: {exc}"
            ) from exc

        raw_items = result.get("priorities", []) if isinstance(result, dict) else []
        if not isinstance(raw_items, list):
            return []

        signal_by_id = {s.id: s for s in digest.all_signals}
        candidates: list[PriorityCandidate] = []
        for item in raw_items[:max_priorities]:
            if not isinstance(item, dict):
                continue

            signal_ids = [str(s) for s in item.get("signal_ids", []) if s]
            channels: list[ChannelKind] = []
            links: list[str] = []
            for sid in signal_ids:
                sig = signal_by_id.get(sid)
                if not sig:
                    continue
                if sig.channel not in channels:
                    channels.append(sig.channel)
                if sig.link and sig.link not in links:
                    links.append(sig.link)

            priority_kind = _coerce_priority_kind(item.get("priority_kind"))
            entity_kind = _coerce_entity_kind(item.get("entity_kind"))

            candidates.append(
                PriorityCandidate(
                    title=str(item.get("title", "")).strip()[:160],
                    why_it_matters=str(item.get("why_it_matters", "")).strip(),
                    urgency=str(item.get("urgency", "medium")).lower(),
                    suggested_action=str(item.get("suggested_action", "")).strip(),
                    source_channels=channels,
                    source_links=links,
                    signal_ids=signal_ids,
                    priority_kind=priority_kind,
                    entity_kind=entity_kind,
                    entity_id=(item.get("entity_id") or None),
                    entity_label=(item.get("entity_label") or None),
                )
            )
        return candidates

    def _build_prompt(
        self,
        digest: ConsolidatedDigest,
        *,
        max_priorities: int,
        focus: str | None,
        entity_index: dict[str, list[str]],
        department: str | None,
        playbook_text: str,
        plate_enabled: bool = False,
        memory: dict | None = None,
        previous_attempt: list[PriorityCandidate] | None = None,
        critique: CritiqueReport | None = None,
        temporal: TemporalContext | None = None,
        wgll_text: str = "",
        about_me: str | None = None,
        muted_topics: list[str] | None = None,
        reader_email: str | None = None,
        reader_name: str | None = None,
        zero_repair: bool = False,
    ) -> str:
        signal_lines = []
        for s in digest.all_signals[:60]:
            attrs_preview = _format_attrs_preview(s.attrs)
            signal_lines.append(
                f"- id={s.id} | channel={s.channel.value} | "
                f"entity={s.entity_kind.value if s.entity_kind else '-'}:{s.entity_id or '-'}\n"
                f"    title: {s.title}\n"
                f"    snippet: {s.snippet[:SIGNAL_SNIPPET_CHARS]}\n"
                f"    attrs: {attrs_preview}"
            )
        signal_block = "\n".join(signal_lines) if signal_lines else "(no signals)"

        # Entity index — highlight cross-channel entities first.
        entity_lines = []
        for entity_key, sig_ids in sorted(
            entity_index.items(), key=lambda kv: -len(kv[1])
        )[:30]:
            cross = "★" if len(sig_ids) >= 2 else " "
            entity_lines.append(f"  {cross} {entity_key:50s} signals: {sig_ids}")
        entity_block = "\n".join(entity_lines) if entity_lines else "  (none)"

        themes_block = (
            "- " + "\n- ".join(digest.cross_channel_themes)
            if digest.cross_channel_themes
            else "(none)"
        )

        playbook_section = (
            f"## DEPARTMENT PLAYBOOK ({department or 'default'})\n"
            f"{playbook_text.strip() if playbook_text.strip() else _GENERIC_PLAYBOOK_FALLBACK}\n"
        )

        # "What's On Your Plate" — appended as its own section rather than
        # edited into rule 4, so a reader whose department is not onboarded
        # gets a byte-identical prompt. Rule 4's "pure 'you went quiet' with
        # no forward hook is NOT a priority" is the clause that keeps an
        # unanswered ask off the list entirely; without lifting it, exempting
        # the same card from the urgency demotion changes nothing, because
        # the card is never emitted in the first place.
        plate_section = (
            (
                "## WHAT'S ON YOUR PLATE (this reader is onboarded)\n"
                "This reader has asked for a briefing about what is WAITING ON\n"
                "THEM, not only what is coming up. For this reader only, these\n"
                "override the wording of rule 4 above:\n"
                "- An EXTERNAL counterparty (see `external_domains`) silent for\n"
                "  >= 7 days on a thread the reader spoke last on IS a priority\n"
                "  in its own right, and MAY carry `high` urgency. It does not\n"
                "  need a forward hook: the silence is the thing that needs a\n"
                "  decision. Still name a concrete next touch as the action.\n"
                "- Say how long it has been, and read that number from\n"
                "  `thread_idle_days` — never infer it from a date in a body.\n"
                "- An obligation already past its date outranks a routine\n"
                "  upcoming meeting. Rank it accordingly.\n"
                "- This does NOT loosen rule 4's closing clause: a thread whose\n"
                "  `reader_last_message_kind` is `answer` or `fyi` is still\n"
                "  handled, and is still left out.\n"
                "\n"
                "`close_date_at_risk` — a dated commitment whose date stopped\n"
                "being believable. Emit it when a HubSpot deal signal carries\n"
                "`close_date` AND either the date has already passed with the\n"
                "deal still open, or the date is near and `days_since_engagement`\n"
                "shows the client has been quiet longer than the deal has left.\n"
                "- State BOTH numbers and read them from the attrs: the close\n"
                "  date, and the days since logged client contact. Never infer\n"
                "  either from prose in a summary.\n"
                "- `days_since_engagement` counts silence from the last CLIENT\n"
                "  CONTACT LOGGED ON THE DEAL. When `client_days_since_engagement`\n"
                "  is present, someone at the client was contacted more recently\n"
                "  than that: give that number too, and never call the client\n"
                "  quiet for longer. If both are absent you do not know how long\n"
                "  the client has been quiet — say the date needs confirming, and\n"
                "  do NOT assert any silence.\n"
                "- The action is to confirm or move the date, not to 'follow up'.\n"
                "- At most THREE deal cards in one briefing, highest value and\n"
                "  nearest date first. This is a five-card list and the reader\n"
                "  has work that is not a deal.\n"
            )
            if plate_enabled
            else ""
        )

        # UC-25 — give the LLM an explicit "do not re-surface" list so it
        # doesn't re-emit priorities the user already acted on. ONLY rows the
        # user actually handled (dismissed / snoozed / resolved) belong here.
        # `active` rows were surfaced by a PRIOR run but NEVER handled by the
        # user, so they must stay eligible to re-surface — the deterministic
        # post-filter keeps them, and labeling them "already handled" here
        # silently drops still-open priorities (a stale but unhandled item
        # would vanish after its first appearance).
        #
        # RESOLVED belongs here too. It didn't used to reach this code at all
        # (load_active filtered it out), so the model kept re-proposing
        # resolved entities under fresh titles and the read-side filter
        # deleted them again — burning a slot per resolved entity and, at
        # worst, emptying a user's whole feed. Telling the model up front
        # means the slot goes to work the user can actually act on.
        from rubik_core.priorities.memory import (
            STATE_DISMISSED,
            STATE_RESOLVED,
            STATE_SNOOZED,
        )

        memory_section = ""
        if memory:
            handled = [
                ((ek, eid, pk), row)
                for (ek, eid, pk), row in memory.items()
                # Deal memory was written under an earlier pipeline assignment
                # and can carry a former workspace's CRM facts into this run.
                if ek != EntityKind.DEAL.value
                if getattr(row, "state", "?")
                in (STATE_DISMISSED, STATE_SNOOZED, STATE_RESOLVED)
                # An ELAPSED snooze is not "already handled" any more, and
                # listing it here is what turns a snooze into a slow resolve.
                # Nothing else can bring a card back: the only revive path runs
                # over cards that SURVIVED a run, so a suppressed card is
                # re-emitted by this model or not at all — and the instruction
                # below says not to, "unless new material evidence has arrived".
                # On a deal whose defining property is that nothing is
                # happening, new evidence never arrives. Both deterministic
                # filters already treat an elapsed snooze as revived; this is
                # the one place that still disagreed.
                and not _snooze_elapsed(row)
            ]
            # Resolved first: it is the only DURABLE state, so a resolved row
            # that falls off the end of the cap costs the user a whole slot —
            # the model proposes it, the read filter deletes it, and nothing
            # takes its place. Dismissed/snoozed can at least resurface.
            handled.sort(
                key=lambda kv: (
                    0 if getattr(kv[1], "state", "?") == STATE_RESOLVED else 1
                )
            )
            handled_lines: list[str] = []
            for (ek, eid, pk), row in handled[:60]:
                state = getattr(row, "state", "?")
                changed = getattr(row, "last_state_change_at", "?")
                durable = " [PERMANENT]" if state == STATE_RESOLVED else ""
                # Name the deadline on a snooze still in force, so the model can
                # tell "quiet until Friday" from "quiet forever".
                until = getattr(row, "snoozed_until", None)
                window = f" until {until}" if state == STATE_SNOOZED and until else ""
                handled_lines.append(
                    f"  - {ek}:{eid} / {pk} → {state} since {changed}{window}{durable}"
                )
            if handled_lines:
                memory_section = (
                    "\n## ALREADY HANDLED (UC-25)\n"
                    "The user has DISMISSED, SNOOZED or RESOLVED the following "
                    "priorities. Do NOT re-emit them unless new material evidence "
                    "has arrived after the state change date. Evidence means "
                    "something that HAPPENED after that date: a new reply on the "
                    "thread, a change to the To-Do, new activity on the deal. A "
                    "meeting time, a due date or any other date still ahead is "
                    "NOT new evidence, and a downstream filter deletes a card "
                    "re-emitted on it.\n"
                    "Entries marked [PERMANENT] were RESOLVED: never re-emit those, "
                    "not even on new evidence — a downstream filter deletes them "
                    "unconditionally, so proposing one wastes a slot and the user "
                    "sees nothing in its place. Spend the slot on other work.\n"
                    + "\n".join(handled_lines)
                    + "\n"
                )

        focus_section = (
            f"\n## USER FOCUS\nThe user explicitly wants priorities tied to: **{focus}**\n"
            if focus
            else ""
        )

        # AI Personalization — who this person is (weight priorities toward
        # their role/focus) and topics they've muted (never surface).
        about_section = (
            f"\n## ABOUT THIS USER\n{about_me.strip()}\n"
            "Weight priorities toward this person's role and stated focus.\n"
            if about_me and about_me.strip()
            else ""
        )
        muted_section = ""
        muted_list = [t.strip() for t in (muted_topics or []) if t and t.strip()]
        if muted_list:
            muted_section = (
                "\n## DO NOT SURFACE\n"
                "The user has muted these topics — never recommend a priority "
                "about them, even if signals are present:\n"
                + "\n".join(f"  - {t}" for t in muted_list)
                + "\n"
            )

        revision_section = ""
        if previous_attempt and critique:
            prev_lines = []
            for p in previous_attempt:
                prev_lines.append(
                    f"  - {p.title} (urgency={p.urgency}; "
                    f"kind={p.priority_kind.value if p.priority_kind else '?'}) "
                    f"— {p.why_it_matters}"
                )
            issues = (
                "\n  - ".join(
                    critique.weak_rationales
                    + critique.missing_signals
                    + critique.suggestions
                )
                or "(none provided)"
            )
            revision_section = (
                "\n## REVISION REQUEST\n"
                "A critic reviewed your previous list and flagged issues. "
                "Address them in this new pass.\n"
                "\n"
                "**This is a REFINEMENT, not a fresh start.** The critic scored "
                "the list, it did not ask you to shorten it. Carry EVERY previous "
                "priority forward — improving its wording, rationale, urgency or "
                "suggested action — and add anything the critic said you missed. "
                f"Return AT LEAST {len(previous_attempt)} "
                f"{'priority' if len(previous_attempt) == 1 else 'priorities'} "
                "unless an item is genuinely invalid.\n"
                "\n"
                "DROP a previous item ONLY when the feedback below shows it does "
                "not belong at all — it duplicates another item, it is about the "
                "wrong person, or its underlying event has passed. A low score is "
                "NOT a reason to drop anything: 'weak rationale' means REWRITE the "
                "rationale, not delete the priority.\n"
                "\n"
                "A shorter list is a WORSE list. The user sees only what you "
                "return here, so an item you quietly drop is work they never hear "
                "about — that has emptied a real user's briefing before.\n"
                "### Previous priorities\n" + "\n".join(prev_lines) + "\n"
                f"### Critic score: {critique.overall_score:.1f}/10\n"
                f"### Issues to address:\n  - {issues}\n"
                f"### Raw feedback: {critique.raw_feedback}\n"
            )

        # Zero-result repair — a prior pass returned an empty list despite
        # signals. Delivering nothing is a product failure (whole departments
        # can get blank briefings while 30-50 signals sit in the prompt), so
        # this pass demands the least-bad picks instead.
        zero_repair_section = ""
        if zero_repair:
            zero_repair_section = (
                "\n## ZERO-RESULT REPAIR\n"
                "Your previous pass returned an EMPTY list even though signals "
                "exist above. That is a product failure — the user opens their "
                "briefing and sees nothing. Re-read the SIGNALS list and return "
                "the 1-3 MOST USEFUL items; `low` urgency is fine (the nearest "
                "upcoming meeting to prepare for, the oldest awaiting-reply "
                "thread, an approaching date). Return an empty list ONLY if "
                "every single signal is pure automated noise with no possible "
                "action for this user.\n"
            )

        lookahead = temporal.lookahead_hours if temporal else DEFAULT_LOOKAHEAD_HOURS
        rules = _META_RULES_PROMPT.replace("{lookahead}", str(lookahead))
        # close_date_at_risk is only a legal answer for a reader whose
        # department is onboarded to "What's On Your Plate". Off, both
        # placeholders resolve to "" and the rules text is byte-identical to
        # what every reader got before the feature existed. The prompt is the
        # first gate; _drop_ungated_kinds in the orchestrator is the real one.
        rules = rules.replace(
            "{plate_kinds}", " | close_date_at_risk" if plate_enabled else ""
        ).replace(
            "{plate_kinds_json}",
            ' | "close_date_at_risk"' if plate_enabled else "",
        )
        temporal_block = f"{temporal.block()}\n\n" if temporal else ""

        # Name the reader so "whose item is it" above has something to compare
        # against. Without this the model cannot tell that the "Alex" in an
        # event title IS the person it is writing to.
        reader_section = ""
        if reader_email or reader_name:
            name_part = (reader_name or "").strip()
            email_part = (reader_email or "").strip()
            who = " ".join(
                p for p in (name_part, f"<{email_part}>" if email_part else "") if p
            )
            reader_section = (
                f"\n## READER\nYou are writing to {who}. Every 'you' in your "
                "output means this person. They are never their own "
                "counterparty.\n"
            )

        return (
            f"{temporal_block}"
            f"{rules}\n"
            f"max_priorities = {max_priorities}\n"
            f"{reader_section}\n"
            f"{playbook_section}\n"
            f"{plate_section}"
            f"{about_section}"
            f"{muted_section}"
            f"{memory_section}"
            f"## CROSS-CHANNEL NARRATIVE\n{digest.narrative}\n\n"
            f"## THEMES\n{themes_block}\n\n"
            f"## ENTITIES (★ marks cross-signal — high-value joins)\n"
            f"{entity_block}\n\n"
            f"## SIGNALS\n{signal_block}\n"
            f"{focus_section}"
            f"{revision_section}"
            f"{zero_repair_section}"
        )


# ── helpers ──────────────────────────────────────────────────────────────────


def _snooze_elapsed(row) -> bool:
    """True when ``row`` is a snooze whose deadline has already passed.

    Mirrors the deterministic filters (``memory._suppression_reason`` and
    ``home._memory_row_keeps``), which both already revive an elapsed snooze.
    Defensive about the type: ``snoozed_until`` is an ISO string on the
    in-memory row and a datetime on the ORM row.
    """
    from datetime import datetime

    from rubik_core.priorities.memory import STATE_SNOOZED

    if getattr(row, "state", None) != STATE_SNOOZED:
        return False
    until = getattr(row, "snoozed_until", None)
    if not until:
        return False
    if isinstance(until, str):
        try:
            until = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            return False
    now = datetime.now(until.tzinfo) if until.tzinfo else datetime.utcnow()
    return until <= now


def _coerce_priority_kind(value) -> PriorityKind | None:
    if not value:
        return None
    try:
        return PriorityKind(value)
    except ValueError:
        return None


def _coerce_entity_kind(value) -> EntityKind | None:
    if not value:
        return None
    try:
        return EntityKind(value)
    except ValueError:
        return None


def _format_attrs_preview(attrs: dict) -> str:
    """Compact one-line preview of the most decision-relevant attrs."""
    if not attrs:
        return "{}"
    # Lift the keys the decision-maker reasons about most often. The list is
    # `ATTRS_PREVIEW_KEYS` (module level) because the system prompt above names
    # these keys and a test pins the two together.
    parts = []
    for k in ATTRS_PREVIEW_KEYS:
        if k in attrs and attrs[k] not in (None, [], "", {}):
            parts.append(f"{k}={attrs[k]}")
    rendered = ", ".join(parts) if parts else "(no decision-relevant attrs)"
    # Raised from 300 when the ownership keys above were added — at 300 a thread
    # with several extracted dates could push `addressed_to_reader` out of the
    # preview entirely, which is the one field that decides whose item it is.
    if len(rendered) <= ATTRS_PREVIEW_CHARS:
        return rendered
    # Over budget. Drop WHOLE key=value pairs rather than slicing mid-value: a
    # raw `rendered[:500]` ends the preview inside whichever value straddles the
    # boundary, and the model reads that fragment as the real value — a
    # truncated name is a fabricated fact rather than a missing one.
    kept: list[str] = []
    used = 0
    for part in parts:
        extra = len(part) + (2 if kept else 0)
        if used + extra > ATTRS_PREVIEW_CHARS:
            break
        kept.append(part)
        used += extra
    dropped = [p.split("=", 1)[0] for p in parts[len(kept) :]]
    # Never truncate silently — an attr the decision-maker never saw is the
    # first thing to check when a card reasons off stale or partial evidence.
    logger.info(
        "[priorities] attrs preview over %d chars (%d) — dropped %d attr(s): %s",
        ATTRS_PREVIEW_CHARS,
        len(rendered),
        len(dropped),
        ", ".join(dropped),
    )
    return ", ".join(kept)
