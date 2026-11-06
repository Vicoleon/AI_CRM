"""Orchestrator for the multi-agent priorities pipeline.

Pipeline (mirrors BookWriter's orchestrator):

    1. Per-channel agents run in parallel (one thread each, with a
       per-channel wall-clock budget so a slow source can't block).
    2. Summarizer merges all digests into a single cross-channel view.
    3. Decision-maker picks the priorities.
    4. Critic scores them. While the score is below threshold, the
       decision-maker revises with the critic's feedback baked in and the
       critic re-scores — at most ``max_revisions`` rounds, stopping early
       when a round does not improve the score.
    5. The final ``PrioritiesBundle`` is what the supervisor receives.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable

from rubik_core.ai_providers.registry import IncompatibleModelRoute
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.priorities.agents import (
    BaseChannelAgent,
    CalendarChannelAgent,
    CriticAgent,
    DecisionMakerAgent,
    DriveChannelAgent,
    EosChannelAgent,
    GmailChannelAgent,
    HubSpotChannelAgent,
    InheritedChannelAgent,
    MeetChannelAgent,
    PromisesChannelAgent,
    SlackChannelAgent,
    SummarizerAgent,
)
from rubik_core.priorities.agents.decision_maker import DecisionLLMUnavailable
from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.models import (
    EOS_PRIORITY_KINDS,
    FORWARD_PRIORITY_KINDS,
    GENERATION_EMPTY_WITH_SIGNALS,
    GENERATION_FILTERED_EMPTY,
    GENERATION_NO_SIGNALS,
    GENERATION_OK,
    GENERATION_ON_PTO,
    HIGH_URGENCY_EXEMPT_KINDS,
    PLATE_HIGH_URGENCY_EXEMPT_KINDS,
    PLATE_ONLY_PRIORITY_KINDS,
    ChannelDigest,
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PrioritiesBundle,
    PrioritiesRequest,
    PriorityCandidate,
    PriorityKind,
    filter_card_attrs,
)
from rubik_core.priorities.temporal import (
    TemporalContext,
    is_elapsed,
    is_future,
    is_past,
    parse_to_absolute,
    resolve_user_now,
)
from rubik_core.tenancy.threads import ContextThreadPoolExecutor

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


# Registry of channel agents — extend here when adding a new channel.
CHANNEL_AGENT_REGISTRY: dict[ChannelKind, type[BaseChannelAgent]] = {
    ChannelKind.GMAIL: GmailChannelAgent,
    ChannelKind.CALENDAR: CalendarChannelAgent,
    ChannelKind.MEET: MeetChannelAgent,
    ChannelKind.DRIVE: DriveChannelAgent,
    ChannelKind.HUBSPOT: HubSpotChannelAgent,
    ChannelKind.EOS: EosChannelAgent,
    ChannelKind.INHERITED: InheritedChannelAgent,
    ChannelKind.PROMISES: PromisesChannelAgent,
    ChannelKind.SLACK: SlackChannelAgent,
}


class PrioritiesOrchestrator:
    """Run the full priorities pipeline for one user."""

    def __init__(
        self,
        config: PrioritiesConfig | None = None,
        *,
        progress: ProgressCallback | None = None,
    ):
        # When the caller doesn't pin a config, prefer the admin-saved values
        # in workspace_settings so model/temperature overrides take effect on
        # the very next run with no process restart.
        if config is None:
            try:
                from rubik_core.priorities.settings_store import (
                    load_priorities_config,
                )

                config = load_priorities_config()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Falling back to default priorities config (settings_store load failed)",
                    exc_info=True,
                )
                config = PrioritiesConfig.default()
        self.config = config
        self.progress = progress or (lambda _msg: None)
        self.summarizer = SummarizerAgent(self.config)
        self.decision_maker = DecisionMakerAgent(self.config)
        self.critic = CriticAgent(self.config)
        self._personal_mail_guard = None

    # ── Entry point ──────────────────────────────────────────────────────

    def generate(self, request: PrioritiesRequest) -> PrioritiesBundle:
        audit: list[str] = []
        self._emit(f"Priorities run starting for {request.user_email}", audit)
        # An orchestrator can be reused; no previous run's mailbox approval
        # may carry over to the next user's model calls.
        for agent in (self.summarizer, self.decision_maker, self.critic):
            client = getattr(agent, "client", None)
            if client is not None:
                client.route_guard = None

        # Pin "now" once for the whole run — anchored to the user's local
        # day. Every reasoning prompt is told this date, and the
        # deterministic past-event filter below enforces it regardless of
        # what the model says.
        temporal = resolve_user_now(
            request.user_email,
            tz_name=request.tz_name,
            lookahead_hours=request.lookahead_hours,
            report_kind=request.report_kind,
        )
        audit.append(
            f"temporal → today={temporal.today_str} tz={temporal.tz_name} "
            f"lookahead={temporal.lookahead_hours}h report_kind={temporal.report_kind}"
            + (
                f" window={temporal.week_start}..{temporal.week_end}"
                if temporal.week_start
                else ""
            )
        )

        # Out-of-office awareness — detect the user's PTO ONCE and attach it to
        # the (frozen) temporal context so every downstream stage (channels,
        # summarizer, decision-maker, critic) is told the user is off via
        # TemporalContext.block(). Best-effort: a detection failure degrades to
        # "not on PTO" and never blocks a run. This closes the bug where a full
        # week of PTO still produced a normal work-day briefing — OOO was the
        # one calendar signal the pipeline discarded.
        #
        # The SAME detection also drives the pre-vacation handoff card below:
        # ``PtoStatus.upcoming`` already covers 45 days ahead, so warning someone
        # the week BEFORE they leave costs no extra calendar traffic. That's why
        # ``pto_status`` is kept in scope rather than consumed here — the old code
        # threw it away unless the user was out *today*, which is precisely the
        # case where a warning is useless.
        pto_status = None
        try:
            from dataclasses import replace as _dc_replace

            from rubik_core.availability.pto import detect_pto

            pto_status = detect_pto(
                request.user_email,
                today=temporal.today,
                tz_name=temporal.tz_name,
            )
            if pto_status.on_pto_today and pto_status.current is not None:
                temporal = _dc_replace(temporal, pto=pto_status)
                _cur = pto_status.current
                audit.append(
                    f"PTO → user is OUT today; on PTO {_cur.start}..{_cur.end}, "
                    f"back {_cur.return_date}"
                )
        except Exception:  # noqa: BLE001 — PTO detection is best-effort
            logger.warning(
                "PTO detection failed for %s", request.user_email, exc_info=True
            )

        digests = self._run_channels(request, audit, temporal)
        if self._personal_mail_guard is not None:
            for agent in (self.summarizer, self.decision_maker, self.critic):
                client = getattr(agent, "client", None)
                if client is not None:
                    client.route_guard = self._personal_mail_guard.assert_route
        private_gmail_context = _private_gmail_context_from_digests(digests)
        crm_provenance = _crm_prompt_provenance(digests)
        consolidated = self._run_summarizer(digests, audit, temporal)

        # Every channel crashing/timing out is an OUTAGE, not a quiet day —
        # zero signals in that state must never be labeled "no_signals".
        all_channels_errored = bool(digests) and all(d.error for d in digests)
        if all_channels_errored:
            audit.append(
                f"ALL {len(digests)} channels errored — an empty result here is "
                "a generation failure, not a quiet day"
            )

        # UC-24 — build the entity index and resolve the user's
        # department + active playbook stack before the decision-maker
        # sees anything. Both feed into its system prompt.
        entity_index = _build_entity_index(consolidated.all_signals)
        audit.append(
            f"entity_index → {len(entity_index)} entities; "
            f"{sum(1 for v in entity_index.values() if len(v) >= 2)} cross-signal"
        )

        department, plate_enabled = _resolve_user_context(request.user_email)
        playbook_text = _resolve_playbook_text(department)
        audit.append(
            f"department={department or '(none)'}; playbook_chars={len(playbook_text)}"
        )
        audit.append(f"open_commitments={'on' if plate_enabled else 'off'}")

        # UC-25 — load the user's memory so we can tell the decision-maker
        # which entities they've already handled, and so the deterministic
        # post-filter can drop anything that leaks past the prompt hint.
        # Includes RESOLVED rows: without them the decision-maker re-proposed
        # resolved entities under fresh titles and the read-side filter
        # silently deleted them (see memory.load_for_run).
        memory = self._load_memory(request.user_email)
        resolved_n = sum(
            1 for r in memory.values() if getattr(r, "state", None) == "resolved"
        )
        audit.append(
            f"memory → {len(memory)} entries ({resolved_n} resolved, "
            f"{len(memory) - resolved_n} non-resolved)"
        )

        # Per-user AI Personalization: about-me/focus context + muted topics.
        # `request.focus` (an explicit per-run intent) wins over the saved one.
        about_me, muted_topics, pref_focus = _resolve_user_priorities_prefs(
            request.user_email
        )
        effective_focus = request.focus or pref_focus
        audit.append(
            f"user prefs → about_me={'set' if about_me else 'none'}; "
            f"muted_topics={len(muted_topics)}; "
            f"focus={'request' if request.focus else ('saved' if pref_focus else 'none')}"
        )

        # Name the reader for the decision-maker so it can tell them apart from
        # the people they meet with — an internal 1:1 titled with both names was
        # producing "your 1:1 with <the reader's own name>".
        reader_name = _resolve_user_name(request.user_email)

        def _decide(**overrides) -> list[PriorityCandidate]:
            kwargs: dict = dict(
                max_priorities=request.max_priorities,
                focus=effective_focus,
                entity_index=entity_index,
                department=department,
                playbook_text=playbook_text,
                plate_enabled=plate_enabled,
                memory=memory,
                temporal=temporal,
                about_me=about_me,
                muted_topics=muted_topics,
                reader_email=request.user_email,
                reader_name=reader_name,
            )
            kwargs.update(overrides)
            return self.decision_maker.decide(consolidated, **kwargs)

        revision_count = 0
        critique = None
        # Defined before the quiet-day branch so every path below has it: the
        # revision merge (to canonicalize EOS keys on BOTH passes before
        # matching them) and the deterministic post-filters both use it. Empty
        # dict on a zero-signal day, which is exactly right.
        signal_by_id = {s.id: s for s in consolidated.all_signals}
        if not consolidated.all_signals:
            # Genuinely quiet day: nothing to rank, so spend no decision/critic
            # LLM calls — and an LLM hiccup here can't masquerade as "no
            # priorities" (or the reverse).
            priorities: list[PriorityCandidate] = []
            audit.append(
                "no signals across all channels — skipping decision/critic stages"
            )
            self._emit("Quiet day: zero signals — no priorities to rank", audit)
        else:
            # An LLM failure below RAISES (DecisionLLMUnavailable) and fails the
            # whole run: the batch then marks this user failed and persists
            # NOTHING, instead of shipping an empty bundle as success.
            priorities = _decide()
            audit.append(f"decision_maker → {len(priorities)} priorities")
            self._emit(f"Decision-maker produced {len(priorities)} priorities", audit)

            if not priorities:
                # Confident zero with signals on the table — demand the
                # least-bad picks once before declaring the bundle empty.
                audit.append(
                    "zero-repair: decision returned 0 with "
                    f"{len(consolidated.all_signals)} signals — retrying with "
                    "least-bad instruction"
                )
                self._emit(
                    "Decision returned 0 despite signals — running zero-repair pass",
                    audit,
                )
                try:
                    priorities = _decide(zero_repair=True)
                except DecisionLLMUnavailable as exc:
                    audit.append(
                        f"zero-repair LLM unavailable — keeping empty list: {exc}"
                    )
                    priorities = []
                audit.append(
                    f"decision_maker (zero-repair) → {len(priorities)} priorities"
                )

            if priorities:
                critique = self.critic.review(
                    consolidated, priorities, temporal=temporal
                )
                if critique.critic_failed:
                    audit.append(
                        "critic LLM UNAVAILABLE — score not recorded; only the "
                        f"deterministic blocker check ran (needs_revision={critique.needs_revision})"
                    )
                    self._emit(
                        "Critic LLM unavailable — quality score not recorded for this run "
                        "(deterministic blocker check still applied)",
                        audit,
                    )
                else:
                    audit.append(
                        f"critic score={critique.overall_score:.1f} "
                        f"needs_revision={critique.needs_revision}"
                    )
                    self._emit(
                        f"Critic scored {critique.overall_score:.1f}/10 "
                        f"(threshold {self.config.revision_threshold:.1f})",
                        audit,
                    )

                # Revision loop. ``max_revisions`` used to be read as a mere
                # boolean (`> 0`), so an admin who set 3 silently got exactly 1
                # — a dead knob promising more than it did. It now genuinely
                # bounds the number of rounds. Each round re-scores, because
                # that is the only way to know whether another one is wanted;
                # it also means the persisted score describes the list that
                # shipped, not the draft the critic first saw.
                while critique.needs_revision and revision_count < max(
                    0, self.config.max_revisions
                ):
                    self._emit(
                        f"Critic asked for a revision (round {revision_count + 1}"
                        f"/{self.config.max_revisions}) — re-running decision-maker",
                        audit,
                    )
                    # Counts the ATTEMPT, not the success: an LLM call was made
                    # and a round consumed either way, so an empty or failed
                    # revision must still show up in the persisted
                    # revision_count (and must still burn a round of the loop).
                    revision_count += 1
                    try:
                        revised = _decide(
                            previous_attempt=priorities, critique=critique
                        )
                    except DecisionLLMUnavailable as exc:
                        audit.append(
                            f"revise LLM unavailable — keeping the current list: {exc}"
                        )
                        break
                    if not revised:
                        audit.append(
                            "decision_maker (revise) → 0 priorities — keeping the "
                            f"current {len(priorities)}: a revision may improve "
                            "the list, never erase it"
                        )
                        break

                    audit.append(
                        f"decision_maker (revise {revision_count}) → "
                        f"{len(revised)} priorities"
                    )
                    # Canonicalize opaque entity keys on BOTH passes before
                    # matching. The model garbles EOS row UUIDs, promise ids
                    # and Google event ids (that's why the
                    # _reattach_* helpers exist) and can garble the same one
                    # DIFFERENTLY per pass, which would make a rewritten
                    # item look brand-new and get its original restored
                    # beside it as a duplicate. Idempotent — they all
                    # re-stamp from the cited signal, so the later calls
                    # still run. Same order as the main flow below, so
                    # canonicalization stays symmetric.
                    _reattach_eos_entities(priorities, signal_by_id)
                    _reattach_eos_entities(revised, signal_by_id)
                    _reattach_promise_entities(priorities, signal_by_id)
                    _reattach_promise_entities(revised, signal_by_id)
                    _reattach_calendar_entities(priorities, signal_by_id)
                    _reattach_calendar_entities(revised, signal_by_id)
                    priorities, restored = _merge_revision(
                        first_pass=priorities,
                        revised=revised,
                        max_priorities=request.max_priorities,
                    )
                    if restored:
                        audit.append(
                            f"revision dropped {len(restored)} item(s) the "
                            "critique never challenged — restored them: "
                            + "; ".join(restored)
                        )
                        self._emit(
                            f"Revision silently dropped {len(restored)} "
                            "priorit(y/ies) — restored",
                            audit,
                        )

                    # Re-score to decide whether another round is warranted.
                    # Without this the loop couldn't converge and would just
                    # burn max_revisions decision-maker calls every time.
                    previous = critique
                    critique = self.critic.review(
                        consolidated, priorities, temporal=temporal
                    )
                    if critique.critic_failed:
                        # Its fallback score is the threshold minus the
                        # blocker penalty: a number the critic never judged.
                        # Steering another round by it, or comparing it with
                        # a real score, would be deciding on noise.
                        audit.append(
                            "critic LLM UNAVAILABLE on the re-score after "
                            f"revision {revision_count} — stopping; the revised "
                            "list's score is not recorded"
                        )
                        break
                    audit.append(
                        f"critic score={critique.overall_score:.1f} "
                        f"needs_revision={critique.needs_revision} "
                        f"(after revision {revision_count})"
                    )
                    if not critique.needs_revision:
                        break
                    # Convergence guard: a round that didn't improve the score
                    # won't be rescued by another one — stop rather than spend
                    # two more LLM calls chasing the same list. Only between
                    # two REAL scores: a first-pass critic that was down has
                    # no score to compare against.
                    if (
                        not previous.critic_failed
                        and critique.overall_score <= previous.overall_score
                    ):
                        audit.append(
                            f"revision {revision_count} did not improve the score "
                            f"({previous.overall_score:.1f} → "
                            f"{critique.overall_score:.1f}) — stopping early"
                        )
                        break

        # Snapshot the decision stage's output size BEFORE the deterministic
        # post-filters: a list they empty out is a legitimate "everything
        # already handled / stale" outcome, not a generation failure.
        pre_filter_count = len(priorities)

        # EOS deterministic keying — the decision-maker LLM can't be trusted
        # to copy opaque EOS row UUIDs verbatim, so re-stamp
        # entity_kind/entity_id/priority_kind (and the target person for a
        # teammate's item) onto any EOS-sourced priority straight from its
        # cited signal. Runs BEFORE the temporal + memory post-filters so both
        # key off the correct (entity, kind) tuple and auto-resolve stays exact.
        _reattach_eos_entities(priorities, signal_by_id)

        # Promises (My To-Dos + heard promises): the kind says which SITUATION
        # — due vs. "you said you would and there is no sign of it" — and is
        # half of the memory key a ✓ writes, so it comes from the signal.
        _reattach_promise_entities(priorities, signal_by_id)

        # Calendar deterministic keying — the same repair for meeting-prep
        # priorities, whose Google event ids the model garbles just as readily.
        # Runs AFTER the two structured passes: both now decline to touch a
        # prep-kind priority, so none of the three can fight over one item.
        _reattach_calendar_entities(priorities, signal_by_id)
        _stamp_gmail_provenance(priorities, signal_by_id)

        # Stamp the recurring-series id (if any) BEFORE the memory filter, so a
        # "never prep this series" dismissal taken on last week's occurrence is
        # visible to this week's differently-keyed occurrence.
        _attach_series_ids(priorities, signal_by_id)

        # Temporal post-filter — the hard backstop that makes "prep for a
        # meeting that already happened" structurally impossible. Runs
        # BEFORE the memory filter and regardless of what the LLM emitted:
        # a forward-looking priority with no future-dated anchor is dropped;
        # a backward debt that claims high urgency with nothing imminent is
        # down-ranked. Reasons go into the audit trail.
        # Feature gate, enforced deterministically. The prompt already omits
        # the plate-only kinds for a reader who is not onboarded, but a prompt
        # is a request, not a gate — this is what actually guarantees a
        # non-onboarded reader can never be shown one.
        if not plate_enabled:
            priorities, gated_drops = _drop_ungated_kinds(priorities)
            for d in gated_drops:
                audit.append(f"feature-gate: {d}")

        priorities, past_drops = _drop_past_anchored(
            priorities, signal_by_id, temporal, plate_enabled=plate_enabled
        )
        for d in past_drops:
            audit.append(f"temporal-filter: {d}")

        # RSVP post-filter — an invite the user never accepted is not a
        # commitment, so it must never produce a "prepare for X" card. Those
        # meetings are reported once, as calendar hygiene, below.
        priorities, rsvp_drops = _drop_unaccepted_prep(priorities, signal_by_id)
        for d in rsvp_drops:
            audit.append(f"rsvp-filter: {d}")

        # Answered-thread post-filter — an email thread whose newest message
        # is the reader's own answer to somebody else's request is handled;
        # no "you went quiet" card and no re-opened deadline can be built on
        # it no matter what the LLM emitted. Same reason the two above exist.
        priorities, answered_drops = _drop_answered_threads(priorities, signal_by_id)
        for d in answered_drops:
            audit.append(f"answered-thread-filter: {d}")

        # UC-25 post-filter — the deterministic backstop on top of the
        # decision-maker's "already handled" hint. Drops resolved /
        # active-snoozed; resurfaces dismissed when new evidence
        # exists. Reasons go into the audit trail.
        priorities, drops = self._apply_memory_filter(priorities, memory, signal_by_id)
        for d in drops:
            audit.append(f"post-filter dropped: {d}")

        # Deal cards share a five-card list with everything else the reader
        # owns. Placed HERE deliberately: after the memory filter, so it caps
        # survivors rather than cards that were going to be dropped anyway,
        # and before _record_memory, so a card the reader never saw never
        # gets an `active` row claiming they did.
        priorities, cap_drops = _cap_deal_cards(priorities)
        for d in cap_drops:
            audit.append(f"deal-cap: {d}")

        # Persist what we surfaced so subsequent runs see the same
        # priorities as "active".
        self._record_memory(request.user_email, priorities)

        headline = self._build_headline(consolidated, priorities)

        # Calendar hygiene — the counterweight to the RSVP filter above. The
        # user still hears that these meetings exist; they just get ONE line
        # asking them to RSVP instead of a prep card each.
        hygiene = _calendar_hygiene(consolidated.all_signals, temporal)
        if hygiene:
            audit.append(
                f"calendar-hygiene: {hygiene['unanswered_count']} unanswered invite(s)"
            )

        # PTO reframing — when the user is out today, an empty list is the
        # CORRECT result and the headline says "enjoy your time off" instead of
        # summarising a work day. pto_meta rides on the bundle for the home UI
        # and the Slack DM banner.
        on_pto = temporal.pto is not None and temporal.pto.on_pto_today
        pto_meta: dict | None = None
        if on_pto and temporal.pto.current is not None:
            cur = temporal.pto.current
            pto_meta = {
                "on_pto": True,
                "start": cur.start.isoformat(),
                "end": cur.end.isoformat(),
                "return_date": cur.return_date.isoformat(),
            }
            back = cur.return_date.strftime("%A")
            end_str = f"{cur.end.strftime('%A, %b')} {cur.end.day}"
            if priorities:
                _n = len(priorities)
                headline = (
                    f"You're on PTO (back {back}), but {_n} thing"
                    f"{'s' if _n != 1 else ''} you kept still need"
                    f"{'' if _n != 1 else 's'} you today."
                )
            else:
                headline = (
                    f"You're on PTO through {end_str} — nothing needs you today. "
                    f"Enjoy the time off; you're back {back}."
                )

        # Pre-vacation hand-off — the one card that must arrive BEFORE the user
        # leaves. Injected deterministically at the TOP rather than offered to
        # the decision-maker as a signal, for two reasons:
        #   * its deadline is external and unmissable (the day they go), so
        #     "the model chose other things this morning" is not an acceptable
        #     outcome the way it is for a follow-up nudge;
        #   * the brief is already fully composed (dates, items, delegates), so
        #     there is nothing for a judgment stage to add.
        # Skipped entirely when the user is out TODAY: by then the warning has
        # missed its window and the PTO reframing above owns the bundle.
        handoff_meta: dict | None = None
        if not on_pto and pto_status is not None:
            brief = self._build_handoff(request.user_email, pto_status, temporal, audit)
            if brief is not None:
                handoff_meta = brief.as_dict()
                priorities.insert(0, _handoff_card(brief))

        # Deterministic cards added after the model provenance pass still
        # belong to the same private run. Shared card readers inspect attrs;
        # shared bundle readers inspect the top-level marker.
        if private_gmail_context:
            for priority in priorities:
                priority.attrs["private_gmail_context"] = True

        # Truth-label how this bundle ended up. The batch and the on-demand
        # path use this as a delivery gate: "empty_with_signals" is a per-user
        # failure (nothing is persisted, the user is retried and reported),
        # while "no_signals" (genuinely quiet day), "filtered_empty" (the
        # decision stage produced items but the temporal/memory post-filters
        # legitimately dropped them all) and "on_pto" (a full day off) are
        # deliverable empties.
        if priorities:
            generation_status = GENERATION_OK
        elif on_pto:
            # A full day off — an empty list here is expected, never a failure.
            generation_status = GENERATION_ON_PTO
        elif not consolidated.all_signals:
            generation_status = (
                GENERATION_EMPTY_WITH_SIGNALS
                if all_channels_errored
                else GENERATION_NO_SIGNALS
            )
        elif pre_filter_count > 0:
            generation_status = GENERATION_FILTERED_EMPTY
        else:
            generation_status = GENERATION_EMPTY_WITH_SIGNALS
        audit.append(f"generation_status={generation_status}")

        return PrioritiesBundle(
            user_email=request.user_email,
            crm_provenance=crm_provenance,
            private_gmail_context=private_gmail_context,
            priorities=priorities,
            headline=headline,
            revision_count=revision_count,
            # Don't persist a fabricated score when the critic LLM was down
            # or never ran (empty list) — None reads honestly as "not
            # measured" instead of a fake pass.
            last_critique_score=(
                None
                if critique is None or critique.critic_failed
                else critique.overall_score
            ),
            audit_trail=audit,
            generation_status=generation_status,
            pto=pto_meta,
            calendar_hygiene=hygiene,
            handoff=handoff_meta,
        )

    # ── Pre-vacation hand-off (best-effort — never breaks a run) ─────────

    def _build_handoff(self, user_email: str, pto_status, temporal, audit: list):
        """The handoff brief for an absence starting inside the lead window.

        Returns None when there's no upcoming absence, or nothing of the user's
        falls inside it. Wrapped: a reminder is an addition to the briefing and
        must never be the reason one fails.
        """
        try:
            from rubik_core.availability.handoff import (
                build_handoff_brief,
                upcoming_handoff_period,
            )

            period = upcoming_handoff_period(pto_status, temporal.today)
            if period is None:
                return None
            brief = build_handoff_brief(user_email, period, today=temporal.today)
            if brief is None:
                audit.append(
                    f"handoff → out {period.start}..{period.end} but nothing "
                    "of theirs lands inside it; no card"
                )
                return None
            audit.append(
                f"handoff → OUT {brief.start}..{brief.end} in "
                f"{brief.days_until}d; {len(brief.items)} item(s) need cover "
                f"({len(brief.l10_items)} L10) — injected as top priority"
            )
            return brief
        except Exception:  # noqa: BLE001 — additive feature, never fatal
            logger.warning("handoff brief failed for %s", user_email, exc_info=True)
            return None

    # ── UC-25 memory plumbing (best-effort — never breaks a run) ─────────

    def _load_memory(self, user_email: str):
        try:
            from rubik_core.priorities.memory import load_for_run

            return load_for_run(user_email)
        except Exception:  # noqa: BLE001
            logger.warning("memory load failed for %s", user_email, exc_info=True)
            return {}

    def _apply_memory_filter(self, priorities, memory, signal_by_id):
        try:
            from rubik_core.priorities.memory import apply_post_filter

            return apply_post_filter(
                priorities,
                memory,
                signal_by_id=signal_by_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("memory post-filter failed", exc_info=True)
            return priorities, []

    def _record_memory(self, user_email: str, priorities) -> None:
        try:
            from rubik_core.priorities.memory import record_seen

            record_seen(user_email, priorities)
        except Exception:  # noqa: BLE001
            logger.warning(
                "memory record_seen failed for %s", user_email, exc_info=True
            )

    # ── Stages ───────────────────────────────────────────────────────────

    def _run_channels(
        self,
        request: PrioritiesRequest,
        audit: list[str],
        temporal: TemporalContext,
    ) -> list[ChannelDigest]:
        """Run all requested channel agents in parallel.

        UC-40.2 / UC-40.3 hardening:

        - Outer wall-clock budget = ``per_channel_timeout_seconds`` (NOT
          ``× 2`` — the inner per-future timeout is also that value, and
          we want the orchestrator to give up at the same point a single
          channel would).
        - When the ``as_completed`` iterator times out, we **catch and
          continue** with whatever channels finished, marking the
          stragglers as ``timeout``. Previously the TimeoutError
          propagated out, the ``with`` context manager called
          ``shutdown(wait=True)``, and the orchestrator hung for up to
          10 minutes waiting for slow threads (Python can't kill them).
          That's how a manual run produced 9-10 min wall-clock
          per user despite a 30s "timeout" — the threadpool exit was
          blocking on Vertex calls that had no business taking that long.
        - We explicitly call ``pool.shutdown(wait=False, cancel_futures=True)``
          so queued futures are cancelled and the function returns with
          the partial result set. Running threads finish on their own
          time but no longer block the loop or the next user.
        """
        self._personal_mail_guard = None
        agents: list[BaseChannelAgent] = []
        eos_enabled = False
        if ChannelKind.EOS in request.channels:
            from rubik_core.control.repository import module_enabled

            try:
                eos_enabled = module_enabled("eos")
            except Exception:  # noqa: BLE001 — never run EOS on a failed control read
                logger.warning("priorities: EOS module lookup failed", exc_info=True)
        for kind in request.channels:
            if kind == ChannelKind.EOS and not eos_enabled:
                audit.append("channel eos → workspace module off, skipping")
                continue
            agent_cls = CHANNEL_AGENT_REGISTRY.get(kind)
            if agent_cls is None:
                audit.append(f"channel {kind.value} → no registered agent, skipping")
                continue
            agents.append(
                agent_cls(
                    user_email=request.user_email,
                    config=self.config,
                    temporal=temporal,
                )
            )

        self._emit(f"Running {len(agents)} channel agents in parallel", audit)

        digests: list[ChannelDigest] = []
        if not agents:
            return digests

        budget = float(self.config.per_channel_timeout_seconds)
        pool = ContextThreadPoolExecutor(max_workers=max(1, len(agents)))
        try:
            future_to_agent = {pool.submit(a.run): a for a in agents}
            try:
                for future in concurrent.futures.as_completed(
                    future_to_agent,
                    timeout=budget,
                ):
                    agent = future_to_agent[future]
                    try:
                        digest = future.result(timeout=1.0)
                    except (ProviderRequestError, IncompatibleModelRoute):
                        # A selected client provider must fail the whole run;
                        # a partial briefing must not be persisted as success.
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "channel %s crashed: %s",
                            agent.channel.value,
                            type(exc).__name__,
                        )
                        digest = ChannelDigest(
                            channel=agent.channel,
                            error=f"agent_crashed: {type(exc).__name__}",
                        )
                    if agent.channel == ChannelKind.GMAIL:
                        guard = getattr(agent, "personal_mail_guard", None)
                        if guard is not None:
                            self._personal_mail_guard = guard
                    digests.append(digest)
                    audit.append(
                        f"channel {digest.channel.value} → "
                        f"{len(digest.signals)} signals, "
                        f"{len(digest.highlights)} highlights"
                        f"{' [ERROR]' if digest.error else ''}"
                    )
            except concurrent.futures.TimeoutError:
                # Managed Vertex keeps the existing best-effort partial result
                # behavior. A client-owned route is an explicit billing and
                # processing choice, so its timeout must be visible instead.
                from rubik_core.ai_providers.registry import model_routes
                from rubik_core.tenancy.context import NoWorkspaceContext

                try:
                    selected = model_routes().get("ai_priorities", {})
                except NoWorkspaceContext:
                    selected = {}
                if selected.get("provider") not in (None, "vertex_managed"):
                    raise ProviderRequestError(
                        "Selected AI provider timed out during priorities extraction"
                    ) from None
                # Wall-clock budget exceeded — record what's still running
                # as timed-out digests and proceed with partial data.
                completed_agents = {
                    future_to_agent[f] for f in future_to_agent if f.done()
                }
                for _f, agent in future_to_agent.items():
                    if agent in completed_agents:
                        continue
                    if any(d.channel == agent.channel for d in digests):
                        continue
                    digests.append(
                        ChannelDigest(
                            channel=agent.channel,
                            error=f"channel_timeout: exceeded {budget:.0f}s budget",
                        )
                    )
                    audit.append(
                        f"channel {agent.channel.value} → TIMEOUT after {budget:.0f}s"
                    )
                logger.warning(
                    "Channel fan-out timed out after %.1fs — proceeding with "
                    "%d completed channel(s), %d timed out",
                    budget,
                    sum(1 for d in digests if not d.error),
                    sum(1 for d in digests if d.error),
                )
        finally:
            # Don't wait for stragglers — they'd block this user (and the
            # entire batch) for as long as Vertex AI / Workspace take to
            # finally answer. cancel_futures=True drops anything still
            # queued; running threads will finish in the background.
            pool.shutdown(wait=False, cancel_futures=True)

        return digests

    def _run_summarizer(self, digests, audit, temporal: TemporalContext):
        consolidated = self.summarizer.consolidate(digests, temporal=temporal)
        audit.append(
            f"summarizer → narrative={len(consolidated.narrative)} chars, "
            f"themes={len(consolidated.cross_channel_themes)}, "
            f"signals={len(consolidated.all_signals)}"
        )
        return consolidated

    # ── Helpers ──────────────────────────────────────────────────────────

    def _build_headline(self, consolidated, priorities) -> str:
        if not priorities:
            return consolidated.narrative.split(".")[0].strip() or (
                "No urgent priorities surfaced today."
            )
        top = priorities[0]
        return f"Top priority: {top.title}".strip()

    def _emit(self, message: str, audit: list[str]) -> None:
        logger.info(message)
        audit.append(message)
        self.progress(message)


# ── Serialization helpers (used by the overnight job + the read endpoint) ────


def _crm_prompt_provenance(digests: list[ChannelDigest]) -> dict:
    """Stamp native HubSpot deal IDs from every signal entering LLM prompts."""
    from rubik_core.tenancy.context import current_workspace

    deal_ids: set[str] = set()
    unprovable = False
    for digest in digests:
        if digest.channel == ChannelKind.HUBSPOT and digest.error:
            unprovable = True
        for signal in digest.signals:
            attrs = signal.attrs if isinstance(signal.attrs, dict) else {}
            hubspot_native = (
                digest.channel == ChannelKind.HUBSPOT
                or signal.channel == ChannelKind.HUBSPOT
            )
            inherited_deal = attrs.get("orig_entity_kind") == EntityKind.DEAL.value
            crm_link = "hubspot.com" in str(signal.link or "").casefold()
            if not (
                hubspot_native
                or signal.entity_kind == EntityKind.DEAL
                or inherited_deal
                or crm_link
            ):
                continue
            if hubspot_native:
                deal_id = signal.entity_id
                provable = (
                    signal.channel == ChannelKind.HUBSPOT
                    and signal.entity_kind == EntityKind.DEAL
                    and isinstance(deal_id, str)
                    and bool(deal_id.strip())
                    and signal.id == deal_id
                )
            elif signal.entity_kind == EntityKind.DEAL:
                deal_id = signal.entity_id
                provable = isinstance(deal_id, str) and bool(deal_id.strip())
            elif inherited_deal:
                deal_id = attrs.get("orig_entity_id")
                provable = isinstance(deal_id, str) and bool(deal_id.strip())
            else:
                deal_id = None
                provable = False
            if not provable:
                unprovable = True
                continue
            deal_ids.add(deal_id)
        if digest.channel == ChannelKind.HUBSPOT and not digest.signals:
            # The base agent's exact no-signal sentence contains no CRM facts.
            # Any other narrative/highlight reached the summarizer without a
            # native deal ID that could be proved at read time.
            quiet = "No notable activity in hubspot today."
            if digest.narrative.strip() not in {"", quiet} or digest.highlights:
                unprovable = True
    return {
        "workspace": current_workspace(),
        "deal_ids": sorted(deal_ids),
        "unprovable": unprovable,
    }


def _private_gmail_context_from_digests(digests: list[ChannelDigest]) -> bool:
    """Private if a personal mailbox reached any channel's model input."""
    return any(
        digest.private_gmail_context
        or any(
            signal.channel == ChannelKind.GMAIL
            and signal.attrs.get("personal_gmail") is True
            for signal in digest.signals
        )
        for digest in digests
    )


def _stamp_gmail_provenance(
    priorities: list[PriorityCandidate], signal_by_id: dict[str, ChannelSignal]
) -> None:
    """Carry mailbox ownership through the model boundary deterministically.

    The decision-maker sees the entire consolidated digest and can use private
    evidence without citing its signal ID. When any personal Gmail signal was
    present, every resulting card carries a run-level privacy flag so no
    model-created wording can enter a manager/team digest through incomplete
    citations. The visible mailbox label remains tied to cited Gmail signals.
    """
    personal = [
        signal
        for signal in signal_by_id.values()
        if signal.channel == ChannelKind.GMAIL
        and signal.attrs.get("personal_gmail") is True
    ]
    for priority in priorities:
        cited = [
            signal_by_id[sid]
            for sid in priority.signal_ids
            if sid in signal_by_id and signal_by_id[sid].channel == ChannelKind.GMAIL
        ]
        labels = {
            str(signal.attrs["gmail_mailbox"])
            for signal in cited
            if signal.attrs.get("gmail_mailbox")
        }
        if personal:
            priority.attrs["private_gmail_context"] = True
        if any(signal.attrs.get("personal_gmail") is True for signal in cited):
            priority.attrs["personal_gmail"] = True
        if labels:
            priority.attrs["gmail_mailbox"] = ", ".join(sorted(labels))
        if len(cited) == 1:
            account_id = cited[0].attrs.get("gmail_account_id")
            if account_id:
                priority.attrs["gmail_account_id"] = account_id
            if priority.entity_kind == EntityKind.THREAD:
                priority.entity_id = cited[0].entity_id


def bundle_to_metadata(bundle: PrioritiesBundle) -> dict:
    """JSON-safe dict for ``HomeCard.metadata_json`` persistence."""
    return {
        "schema_version": 2,
        "private_gmail_context": bundle.private_gmail_context,
        "crm_provenance": (
            dict(bundle.crm_provenance)
            if isinstance(bundle.crm_provenance, dict)
            else None
        ),
        "generated_at": bundle.generated_at,
        "headline": bundle.headline,
        "revision_count": bundle.revision_count,
        "last_critique_score": bundle.last_critique_score,
        "generation_status": bundle.generation_status,
        "pto": bundle.pto,
        "calendar_hygiene": bundle.calendar_hygiene,
        "handoff": bundle.handoff,
        "audit_trail": list(bundle.audit_trail),
        "priorities": [
            {
                "id": p.id,
                "title": p.title,
                "why_it_matters": p.why_it_matters,
                "urgency": p.urgency,
                "source_channels": [c.value for c in p.source_channels],
                "source_links": list(p.source_links),
                "suggested_action": p.suggested_action,
                "signal_ids": list(p.signal_ids),
                "priority_kind": p.priority_kind.value if p.priority_kind else None,
                "entity_kind": p.entity_kind.value if p.entity_kind else None,
                "entity_id": p.entity_id,
                "entity_label": p.entity_label,
                "covered_entity_ids": list(p.covered_entity_ids),
                "covered_entity_labels": list(p.covered_entity_labels),
                "covered_entity_is_own": list(p.covered_entity_is_own),
                "series_entity_id": p.series_entity_id,
                "target_email": p.target_email,
                "target_label": p.target_label,
                # The card's own renderable facts. Re-filtered here rather
                # than trusted, because this is the one chokepoint into
                # `metadata_json`: whatever lands in this dict is what the
                # read endpoint projects into a browser, so the allow-list has
                # to hold at the boundary, not only where it was stamped —
                # see models.CARD_ATTR_ALLOWLIST.
                "attrs": filter_card_attrs(p.attrs),
            }
            for p in bundle.priorities
        ],
    }


def bundle_from_metadata(user_email: str, metadata: dict) -> PrioritiesBundle:
    """Rebuild a bundle only after its whole CRM prompt scope passes live proof."""
    from rubik_core.priorities.cached_visibility import verified_bundle_deals

    if verified_bundle_deals(metadata) is None:
        raise ValueError("cached priority bundle provenance is unavailable")
    priorities = []
    for raw in metadata.get("priorities", []) or []:
        channels = []
        for c in raw.get("source_channels", []) or []:
            try:
                channels.append(ChannelKind(c))
            except ValueError:
                continue
        priority_kind = None
        if raw.get("priority_kind"):
            try:
                priority_kind = PriorityKind(raw["priority_kind"])
            except ValueError:
                pass
        entity_kind = None
        if raw.get("entity_kind"):
            try:
                entity_kind = EntityKind(raw["entity_kind"])
            except ValueError:
                pass

        priorities.append(
            PriorityCandidate(
                id=raw.get("id") or "",
                title=raw.get("title", ""),
                why_it_matters=raw.get("why_it_matters", ""),
                urgency=raw.get("urgency", "medium"),
                source_channels=channels,
                source_links=list(raw.get("source_links", []) or []),
                suggested_action=raw.get("suggested_action", ""),
                signal_ids=list(raw.get("signal_ids", []) or []),
                priority_kind=priority_kind,
                entity_kind=entity_kind,
                entity_id=raw.get("entity_id"),
                entity_label=raw.get("entity_label"),
                covered_entity_ids=list(raw.get("covered_entity_ids") or []),
                covered_entity_labels=list(raw.get("covered_entity_labels") or []),
                # Empty on cards written before per-sibling ownership shipped;
                # the reader treats a missing slot as "unknown", not "not
                # yours".
                covered_entity_is_own=list(raw.get("covered_entity_is_own") or []),
                # Absent on cards written before series suppression shipped —
                # .get() keeps those readable, they simply have no series id.
                series_entity_id=raw.get("series_entity_id"),
                target_email=raw.get("target_email"),
                target_label=raw.get("target_label"),
                # Filtered on the way back OUT too. Cards written before this
                # shipped simply have none, and a row that somehow carries a
                # key the allow-list does not name (an older build, a hand
                # edit) must not become the one that reaches a reader.
                attrs=filter_card_attrs(raw.get("attrs")),
            )
        )
    return PrioritiesBundle(
        user_email=user_email,
        crm_provenance=dict(metadata["crm_provenance"]),
        private_gmail_context=metadata.get("private_gmail_context") is True,
        generated_at=metadata.get("generated_at") or "",
        priorities=priorities,
        headline=metadata.get("headline", ""),
        revision_count=int(metadata.get("revision_count") or 0),
        last_critique_score=metadata.get("last_critique_score"),
        audit_trail=list(metadata.get("audit_trail", []) or []),
        # Older cards predate the field; "ok" is the
        # honest default for anything that was actually delivered.
        generation_status=metadata.get("generation_status") or GENERATION_OK,
        pto=metadata.get("pto"),
        # Absent on cards written before RSVP filtering shipped — None reads
        # correctly as "nothing unanswered to report".
        calendar_hygiene=metadata.get("calendar_hygiene"),
        handoff=metadata.get("handoff"),
    )


# ── UC-24 helpers — entity index + dept playbook resolution ──────────────────


def _build_entity_index(signals: list) -> dict[str, list[str]]:
    """Group signal_ids by ``(entity_kind, entity_id)`` join key.

    Returns ``{entity_key: [signal_id, …]}`` for every entity that
    appears in at least one signal. The decision-maker prompt
    highlights entries where ``len >= 2`` to make cross-channel
    correlation structurally obvious to the LLM.

    Signals can also publish ``attrs["related_entities"]`` (a list of
    ``{"entity_kind", "entity_id"}`` dicts) — used by the Calendar
    extractor so an event references both its event-id and the
    person/company of each external attendee. Every related entity
    extends the index alongside the signal's primary key.
    """
    index: dict[str, list[str]] = {}

    def _add(key: str | None, sig_id: str) -> None:
        if not key:
            return
        bucket = index.setdefault(key, [])
        if sig_id not in bucket:
            bucket.append(sig_id)

    for sig in signals:
        _add(getattr(sig, "entity_key", None), sig.id)
        for related in sig.attrs.get("related_entities") or []:
            if not isinstance(related, dict):
                continue
            kind = related.get("entity_kind")
            ident = related.get("entity_id")
            if not kind or not ident:
                continue
            _add(f"{kind}:{ident}", sig.id)
    return index


def _resolve_user_department(user_email: str) -> str | None:
    """Look up ``users.department`` for the pipeline target.

    Returns ``None`` on any failure (unknown user, DB error, etc.) — the
    decision-maker then falls back to playbook resolution with
    department=None, i.e. default-everyone rows only.
    """
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import User
    except Exception:  # noqa: BLE001
        return None

    try:
        with SessionLocal() as db:
            row = db.query(User).filter(User.email == user_email).first()
            if row is None:
                return None
            dept = (row.department or "").strip()
            return dept or None
    except Exception:  # noqa: BLE001
        logger.warning("dept lookup failed for %s", user_email, exc_info=True)
        return None


def _resolve_user_context(user_email: str) -> tuple[str | None, bool]:
    """``(department, may_see_plate)`` for one reader — one row, one session.

    Resolves BOTH things the run needs from the users table so the feature
    verdict costs no extra query beyond the department lookup that already
    happens here.

    The verdict is resolved in the orchestrator rather than in the nightly
    batch because the app lazily regenerates a missing bundle through this
    same ``generate()``; gating in ``batch.py`` alone would leave the in-app
    path ungated and the two surfaces would disagree about what a user sees.

    Two failure modes are deliberately collapsed to "denied": an unknown user
    and a lookup error. A denial is SILENCE — the reader still gets their
    normal briefing, just without the plate shapes. It must never become a
    ``ChannelDigest(error=…)``: an all-errored run is treated as poisoned
    upstream, which would push a not-onboarded user toward a FAILED bundle.
    """
    department = _resolve_user_department(user_email)

    # The env switch decides whether the feature RUNS AT ALL in this
    # environment; the registry decides WHO reaches it once it does.
    # feature_allows does not consult the kill switch, so — like every other
    # caller in the repo — check it first.
    from rubik_core.config import settings as _settings

    if not getattr(_settings, "OPEN_COMMITMENTS_ENABLED", False):
        return department, False

    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import User
        from rubik_core.features import feature_allows

        with SessionLocal() as db:
            row = db.query(User).filter(User.email == user_email).first()
            if row is None:
                return department, False
            # Pass the ORM ROW, never the email string. feature_allows
            # duck-types its ``user`` and a bare string does not raise — it
            # reads no department, matches no roster and returns False for
            # everyone, silently, all night.
            return department, bool(feature_allows("open_commitments", row, db))
    except Exception:  # noqa: BLE001 — a gate that cannot read is not a grant
        logger.warning(
            "open_commitments gate lookup failed for %s", user_email, exc_info=True
        )
        return department, False


def _resolve_user_name(user_email: str) -> str | None:
    """The reader's display name from ``users.name``, or ``None``.

    Reused by the decision-maker's READER block. ``None`` is fine — the block
    then names the reader by email alone, which is still enough to keep the
    model from addressing them as their own counterparty.
    """
    try:
        from rubik_core.priorities.agents._people import resolve_display_names

        return resolve_display_names([user_email]).get(user_email.strip().lower())
    except Exception:  # noqa: BLE001 — never fail a run over a display name
        logger.warning("reader name lookup failed for %s", user_email, exc_info=True)
        return None


def _resolve_user_priorities_prefs(
    user_email: str,
) -> tuple[str | None, list[str], str | None]:
    """Per-user AI-Priorities personalization: ``(about_me, muted_topics, focus)``.

    Reads the user's ``UserPreference`` row so the decision-maker prompt can
    reflect who they are (``about_me``), skip topics they've muted
    (``priorities_muted_topics``), and inherit a saved focus
    (``priorities_focus``) when the request didn't specify one. Returns
    ``(None, [], None)`` on any failure — the prompt is then unchanged, so this
    can never break a run. Applies to every entry point (interactive, overnight
    batch, scheduled job) since they all flow through the orchestrator.
    """
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import UserPreference
    except Exception:  # noqa: BLE001
        return (None, [], None)

    try:
        with SessionLocal() as db:
            row = (
                db.query(UserPreference)
                .filter(UserPreference.user_email == user_email)
                .first()
            )
            if row is None:
                return (None, [], None)
            about = (row.about_me or "").strip() or None
            muted = row.priorities_muted_topics or []
            if not isinstance(muted, list):
                muted = []
            muted = [str(t).strip() for t in muted if str(t).strip()]
            focus = (row.priorities_focus or "").strip() or None
            return (about, muted, focus)
    except Exception:  # noqa: BLE001
        logger.warning(
            "priorities prefs lookup failed for %s", user_email, exc_info=True
        )
        return (None, [], None)


def _resolve_wgll_text(surface: str, user_email: str) -> str:
    """Build the WGLL feedback block for the decision-maker prompt.

    Exemplars are scoped to ``user_email``'s own ratings (see
    ``build_wgll_context``). Returns "" on any failure or when there's no
    feedback — the prompt is then unchanged, so this can never break a run.
    """
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.feedback.wgll import build_wgll_context
    except Exception:  # noqa: BLE001
        return ""

    try:
        with SessionLocal() as db:
            return build_wgll_context(db, surface=surface, user_email=user_email)
    except Exception:  # noqa: BLE001
        logger.warning("WGLL resolution failed for surface=%s", surface, exc_info=True)
        return ""


# ── Temporal post-filter — the hard "no past events" backstop ────────────────


def _candidate_anchor_dates(sig) -> list:
    """Date-shaped values a priority anchored on this signal could rely on.

    We only consider the keys that denote *when something happens*
    (``start_iso``, deal ``close_date``, extracted ``mentioned_dates``) —
    not ingestion/modification timestamps, which say nothing about whether
    the underlying obligation is still ahead of the user.
    """
    attrs = getattr(sig, "attrs", {}) or {}
    out: list = []
    for key in ("start_iso", "close_date"):
        v = attrs.get(key)
        if v:
            out.append(v)
    md = attrs.get("mentioned_dates")
    if isinstance(md, list):
        out.extend(md)
    elif md:
        out.append(md)
    return out


def _actionable_future_anchor(
    candidates: list, temporal: TemporalContext, *, prep: bool
) -> bool:
    """Does any cited anchor point at a genuinely-upcoming moment?

    For PREP kinds an anchor must be in the future at *datetime* granularity
    — a meeting earlier today has already happened and cannot be prepped for.
    For deadline-style kinds, date granularity is enough (a deadline "today"
    is still actionable until day-end)."""
    for c in candidates:
        if not is_future(c, temporal):
            continue
        if prep and is_elapsed(c, temporal):
            continue  # future by date but the time-of-day has already passed
        return True
    return False


# The plate prompt asks for "at most THREE deal cards". This is the backstop,
# because a prompt is a request, not a gate. The first tier fires on every open
# deal whose close date has already passed, which in a CRM nobody grooms is a
# large slice of the book on day one — uncapped, an AE's first morning is five
# deal cards and nothing else.
_MAX_DEAL_CARDS = 3


def _cap_deal_cards(
    priorities: list[PriorityCandidate],
) -> tuple[list[PriorityCandidate], list[str]]:
    """Keep at most ``_MAX_DEAL_CARDS`` close-date cards, best first.

    Order is whatever the decision-maker returned — it was told to rank by
    value and nearness — so this truncates the tail rather than re-sorting.
    """
    kept: list[PriorityCandidate] = []
    drops: list[str] = []
    seen = 0
    for p in priorities:
        if p.priority_kind is PriorityKind.CLOSE_DATE_AT_RISK:
            seen += 1
            if seen > _MAX_DEAL_CARDS:
                drops.append(
                    f"dropped '{p.title or '(unnamed)'}' — more than "
                    f"{_MAX_DEAL_CARDS} deal cards in one briefing"
                )
                continue
        kept.append(p)
    return kept, drops


def _drop_ungated_kinds(
    priorities: list[PriorityCandidate],
) -> tuple[list[PriorityCandidate], list[str]]:
    """Remove kinds that only exist for readers on "What's On Your Plate".

    Called only when the feature is OFF for this reader. The decision-maker's
    allowlist already excludes these kinds in that case, so this should never
    fire — it logs when it does, because a model inventing a kind it was not
    offered is worth knowing about.
    """
    if not PLATE_ONLY_PRIORITY_KINDS:
        return priorities, []
    kept: list[PriorityCandidate] = []
    drops: list[str] = []
    for p in priorities:
        kind = p.priority_kind
        if kind in PLATE_ONLY_PRIORITY_KINDS:
            drops.append(
                f"dropped '{p.title or '(unnamed)'}' — kind="
                f"{kind.value} requires the open_commitments feature"
            )
            continue
        kept.append(p)
    return kept, drops


def _drop_past_anchored(
    priorities: list[PriorityCandidate],
    signal_by_id: dict,
    temporal: TemporalContext,
    *,
    plate_enabled: bool = False,
) -> tuple[list[PriorityCandidate], list[str]]:
    """Deterministically remove past-anchored priorities.

    Rules (model-independent):

    * A FORWARD-looking priority (``upcoming_prep``, ``approaching_deadline``,
      legacy ``prep_debt`` / ``scheduling_debt``) is DROPPED when it has no
      genuinely-upcoming anchor AND its evidence is provably stale — every
      cited date is past/elapsed, or its only justification is a meeting
      transcript (a transcript proves the meeting already happened). For prep
      kinds, "stale" includes a meeting earlier *today* (datetime, not date).
    * A BACKWARD debt (``follow_up_debt``, ``commitment_debt``,
      ``neglect_debt``) — and ``pre_touchpoint_followup``, whose resolving
      action (send the follow-up) stays valid even after a past meeting —
      is never dropped on date; a ``high`` urgency claim with no upcoming
      anchor is down-ranked to ``medium``.

    We never drop on uncertainty: if any cited anchor is unresolvable (could
    be future), the priority is kept.
    """
    kept: list[PriorityCandidate] = []
    drops: list[str] = []

    for p in priorities:
        sigs = [
            signal_by_id[sid] for sid in (p.signal_ids or []) if sid in signal_by_id
        ]
        candidates: list = []
        cites_transcript = False
        for s in sigs:
            candidates.extend(_candidate_anchor_dates(s))
            if getattr(s, "channel", None) == ChannelKind.MEET:
                cites_transcript = True

        kind = p.priority_kind
        title = p.title or "(unnamed)"
        is_forward = kind in FORWARD_PRIORITY_KINDS
        is_prep = kind in (PriorityKind.UPCOMING_PREP, PriorityKind.PREP_DEBT)

        has_future = _actionable_future_anchor(candidates, temporal, prep=is_prep)
        # An unresolvable date might be future — never drop on that uncertainty.
        has_unresolved = any(parse_to_absolute(c, temporal) is None for c in candidates)
        has_stale = any(is_past(c, temporal) for c in candidates) or (
            is_prep and any(is_elapsed(c, temporal) for c in candidates)
        )

        if is_forward:
            # A transcript can never justify a forward item that has no
            # genuinely-upcoming anchor (applies to every forward kind).
            if cites_transcript and not has_future and not has_unresolved:
                drops.append(
                    f"dropped '{title}' — forward priority justified only by a "
                    f"past meeting transcript (kind={kind.value if kind else '-'})"
                )
                continue
            # Provably stale: nothing upcoming, nothing uncertain, ≥1 past anchor.
            if not has_future and not has_unresolved and has_stale:
                drops.append(
                    f"dropped '{title}' — forward priority with only "
                    f"past/elapsed anchors (kind={kind.value if kind else '-'})"
                )
                continue
            kept.append(p)
            continue

        # Backward debt: keep, but a 'high' needs something imminent — EXCEPT
        # EOS execution kinds, where an OVERDUE item (a past anchor) is MORE
        # urgent, not less, so it keeps the 'high' the decision-maker assigned.
        # "What's On Your Plate" widens the exemption for the debt kinds, and
        # only for readers whose department is onboarded — see
        # PLATE_HIGH_URGENCY_EXEMPT_KINDS. Off, this set is byte-identical to
        # today's, so a non-onboarded reader's ranking does not move.
        exempt = HIGH_URGENCY_EXEMPT_KINDS
        if plate_enabled:
            exempt = exempt | PLATE_HIGH_URGENCY_EXEMPT_KINDS
        if (
            kind not in exempt
            and (p.urgency or "").lower() == "high"
            and not has_future
        ):
            p.urgency = "medium"
            drops.append(
                f"down-ranked '{title}' high→medium — no upcoming anchor "
                f"(kind={kind.value if kind else '-'})"
            )
        kept.append(p)

    return kept, drops


# How many unanswered invites the calendar-hygiene note names before it
# collapses the rest into "and N more".
_HYGIENE_NAMED_LIMIT = 3


def _is_unanswered(sig) -> bool:
    """True for a calendar signal the reader was invited to but never answered."""
    # Lazy — `rubik_core.tools.calendar` pulls in googleapiclient at module
    # scope, so every consumer in this package defers it.
    from rubik_core.tools.calendar import RSVP_NEEDS_ACTION

    if getattr(sig, "channel", None) != ChannelKind.CALENDAR:
        return False
    return (getattr(sig, "attrs", {}) or {}).get("rsvp") == RSVP_NEEDS_ACTION


def _drop_unaccepted_prep(
    priorities: list[PriorityCandidate],
    signal_by_id: dict,
) -> tuple[list[PriorityCandidate], list[str]]:
    """Deterministically remove prep cards for meetings the reader never accepted.

    An invite sitting on ``needsAction`` is not a commitment — it's most often a
    call the reader was cc'd onto and doesn't need to be on (a routine rollout,
    say). Telling them to prep for it buries the meetings they DID commit to.
    Those events still reach the decision-maker as context, so this is the
    backstop that keeps them from becoming "prepare for X" cards no matter what
    the LLM emits — the same reason :func:`_drop_past_anchored` exists.

    A prep card survives if it cites even one committed meeting: the reader has
    a real obligation there, and the unanswered invite is merely corroborating.
    Declined events never get this far (the calendar extractor drops them).
    Non-prep kinds are untouched — a follow-up you owe someone stays owed
    whether or not you RSVP'd to the meeting where you promised it.
    """
    kept: list[PriorityCandidate] = []
    drops: list[str] = []

    for p in priorities:
        if p.priority_kind not in (PriorityKind.UPCOMING_PREP, PriorityKind.PREP_DEBT):
            kept.append(p)
            continue

        cal_sigs = [
            signal_by_id[sid]
            for sid in (p.signal_ids or [])
            if sid in signal_by_id
            and getattr(signal_by_id[sid], "channel", None) == ChannelKind.CALENDAR
        ]
        # No calendar evidence at all → not a "prep for this invite" card; leave
        # it to the temporal filter. Never drop on absence of evidence.
        if cal_sigs and all(_is_unanswered(s) for s in cal_sigs):
            drops.append(
                f"dropped '{p.title or '(unnamed)'}' — prep for an invite the "
                "user never accepted (surfaced as calendar hygiene instead)"
            )
            continue
        kept.append(p)

    return kept, drops


# The two kinds an already-answered email thread was being turned into. Both
# read `direction=i_sent_last` as "the reader is waiting" when it can equally
# mean "the reader answered" — see `_reader_closed_the_loop`.
_ANSWERED_THREAD_KINDS = (
    PriorityKind("follow_up_debt"),
    PriorityKind("approaching_deadline"),
)


def _reader_closed_the_loop(sig) -> bool:
    """True for a Gmail thread whose newest message is the reader's own answer.

    The Gmail extractor stamps `reader_last_message_kind` only when the reader
    spoke last: "ask" when they opened a thread nobody has answered (or their
    reply itself poses a question — the LLM may promote to this), "answer"
    when their message followed somebody else's, "fyi" when it shared
    information and asked for nothing. Only the last two count as closed.
    Anything unknown reads as False — we never drop on absence of evidence.
    """
    if getattr(sig, "channel", None) != ChannelKind.GMAIL:
        return False
    attrs = getattr(sig, "attrs", {}) or {}
    return attrs.get("direction") == "i_sent_last" and attrs.get(
        "reader_last_message_kind"
    ) in ("answer", "fyi")


def _drop_answered_threads(
    priorities: list[PriorityCandidate],
    signal_by_id: dict,
) -> tuple[list[PriorityCandidate], list[str]]:
    """Deterministically remove cards built on an email the reader already answered.

    Two kinds of "not useful" rating come from this: a `follow_up_debt`
    card ("confirm the plan with stakeholders") and an
    `approaching_deadline` card ("ACTION NEEDED by 8/6") on threads where
    the reader's own reply was the newest message. Both rules keyed on
    `direction=i_sent_last`, which is true in that case too. The prompt now
    says so; this is the backstop that holds when the model ignores it — the
    same reason :func:`_drop_past_anchored` and :func:`_drop_unaccepted_prep`
    exist.

    A card survives if it cites even one signal that is NOT such a thread (a
    calendar deadline, a second thread the reader is actually waiting on).
    Other kinds are untouched: a first-person promise made inside the reply
    ("I'll send the deck Friday") is still a `commitment_debt`.
    """
    kept: list[PriorityCandidate] = []
    drops: list[str] = []

    for p in priorities:
        if p.priority_kind not in _ANSWERED_THREAD_KINDS:
            kept.append(p)
            continue
        sigs = [
            signal_by_id[sid] for sid in (p.signal_ids or []) if sid in signal_by_id
        ]
        if sigs and all(_reader_closed_the_loop(s) for s in sigs):
            kind = p.priority_kind
            drops.append(
                f"dropped '{p.title or '(unnamed)'}' — the reader's own reply is "
                f"the newest message in every cited email thread "
                f"(kind={kind.value if kind else '-'})"
            )
            continue
        kept.append(p)

    return kept, drops


def _calendar_hygiene(signals: list, temporal: TemporalContext) -> dict | None:
    """Structured "you haven't answered these" note, or None when nothing to say.

    Scoped to genuinely-upcoming invites so a stale entry from earlier today
    can't nag. Returns the same shape the ``pto`` banner uses — raw data on the
    bundle, rendered by each surface (home UI + Slack DM).
    """
    pending: list[tuple[str, dict]] = []
    for s in signals:
        if not _is_unanswered(s):
            continue
        attrs = getattr(s, "attrs", {}) or {}
        start = attrs.get("start_iso") or ""
        # Same standard prep cards are held to: future, and not already elapsed
        # today. An unparseable start is kept — never hide on uncertainty.
        if start and (not is_future(start, temporal) or is_elapsed(start, temporal)):
            continue
        pending.append(
            (
                start,
                {
                    "title": getattr(s, "title", "") or "(untitled)",
                    "when_local": attrs.get("when_local"),
                    "link": getattr(s, "link", None),
                },
            )
        )

    if not pending:
        return None

    pending.sort(key=lambda item: item[0] or "")
    invites = [item[1] for item in pending]
    return {
        "unanswered_count": len(invites),
        "invites": invites[:_HYGIENE_NAMED_LIMIT],
    }


_HANDOFF_NAMED_ITEMS = 4


def _handoff_card(brief) -> PriorityCandidate:
    """Turn a :class:`HandoffBrief` into the top card of the bundle.

    Deliberately built AFTER ``_apply_memory_filter`` / ``_record_memory`` in
    ``generate``, which has two consequences worth stating out loud:

    * It **refires every day** of the lead week instead of being suppressed as a
      repeat. That's correct here and nowhere else — the card is a countdown to a
      fixed date, so "you already saw this yesterday" is not a reason to stop
      saying it while the deadline is still ahead.
    * It is **never written to priority memory**, so it can't leave a resolved
      key behind that blacklists anything later (the read-filter poisoning that
      can zero a feed).

    It also carries no ``entity_kind``/``entity_id``: the card speaks for several
    entities of different kinds at once, and pinning it to the first one is the
    bug that made multi-entity cards "not stick".
    """
    when = "tomorrow" if brief.days_until <= 1 else f"in {brief.days_until} days"
    # Built from %b + .day rather than "%b %-d": the no-pad directive is
    # platform-specific and blows up on any libc that doesn't implement it.
    start_str = f"{brief.start.strftime('%b')} {brief.start.day}"
    end_str = f"{brief.end.strftime('%b')} {brief.end.day}"
    span = start_str if brief.start == brief.end else f"{start_str}–{end_str}"

    n_l10 = len(brief.l10_items)
    if n_l10:
        title = (
            f"Hand off {n_l10} L10 you manage before you're out {span} ({when})"
            if n_l10 > 1
            else f"Hand off {brief.l10_items[0].label} before you're out {span} ({when})"
        )
    else:
        title = f"Hand off {len(brief.items)} item(s) before you're out {span} ({when})"

    lines: list[str] = []
    for item in brief.items[:_HANDOFF_NAMED_ITEMS]:
        who = item.delegate_name or "no obvious cover"
        due = f" (due {item.due})" if item.due else ""
        reason = f" — {item.delegate_reason}" if item.delegate_reason else ""
        lines.append(f"• {item.label}{due} → {who}{reason}")
    remaining = len(brief.items) - _HANDOFF_NAMED_ITEMS
    if remaining > 0:
        lines.append(f"• …and {remaining} more")

    action = "\n".join(lines)
    if n_l10:
        # The specific dead end this whole feature exists to prevent: only the
        # manager (or their team's leader / an admin) can re-point an L10, and
        # once they're away the team can't do it for them without break-glass.
        action += (
            "\n\nFor each L10: open EOS → Meetings → Edit and set the manager to "
            "whoever is covering. Do it before you leave — once you're out, your "
            "team has to take the meeting over instead."
        )

    return PriorityCandidate(
        title=title,
        why_it_matters=brief.summary,
        urgency="high",
        suggested_action=action,
        priority_kind=PriorityKind.OOO_HANDOFF,
        source_channels=[ChannelKind.CALENDAR, ChannelKind.EOS],
    )


def _revision_identity(p) -> tuple:
    """Identity used to tell "same priority, rewritten" from "different priority".

    Prefers the UC-24 entity triple, which is what the rest of the pipeline
    (memory, auto-resolve hooks, dedupe) keys on. Falls back to a normalized
    title for pre-UC-24 / untagged candidates, since a revision that only
    rewords a title would otherwise look like a brand-new item and the
    original would be restored alongside it as a near-duplicate.
    """
    ek = getattr(p, "entity_kind", None)
    eid = getattr(p, "entity_id", None)
    pk = getattr(p, "priority_kind", None)
    if ek and eid and pk:
        ek_v = getattr(ek, "value", ek)
        pk_v = getattr(pk, "value", pk)
        return ("entity", str(ek_v), str(eid), str(pk_v))
    return ("title", " ".join((getattr(p, "title", "") or "").lower().split()))


def _merge_revision(
    *,
    first_pass: list,
    revised: list,
    max_priorities: int | None = None,
) -> tuple[list, list[str]]:
    """Keep the revision's improvements without losing its silent drops.

    The critic returns a holistic score plus ``weak_rationales`` /
    ``missing_signals`` / ``suggestions`` — it has NO per-item "remove this"
    channel, so a revision that comes back shorter has dropped work nobody
    asked it to drop. Observed twice on one user: 4 priorities → 1, and 3 → 2.
    Combined with a downstream filter that removed the sole survivor, the 4→1
    collapse is what emptied a real briefing.

    The enforced invariant is narrow on purpose: **a revision may not REDUCE the
    count.** It is NOT "every first-pass item must reappear". Rewriting an item
    is the single most common legitimate revision, and a rewritten title is
    indistinguishable from "dropped one, added a different one" for candidates
    with no entity triple — so demanding per-item reappearance would restore a
    vague original next to the improved rewrite that replaced it.

    So: if the revision returns as many items as it received, it is accepted
    untouched (it refined in place — exactly what was asked). Only when it comes
    back SHORTER do we close the gap, restoring at most ``len(first_pass) -
    len(revised)`` first-pass items that the revision did not carry forward,
    taken in original rank order. That bounds any duplicate risk to the size of
    the gap while guaranteeing the user never loses coverage.

    The revised items come first — they carry the improvements. ``max_priorities``
    still caps the result, so this can never inflate a list past the request.

    Returns ``(merged, restored_titles)``. ``restored_titles`` is empty in the
    normal, healthy case.
    """
    gap = len(first_pass) - len(revised)
    if gap <= 0:
        merged = list(revised)
        if max_priorities and max_priorities > 0:
            merged = merged[:max_priorities]
        return merged, []

    revised_ids = {_revision_identity(p) for p in revised}
    unmatched = [p for p in first_pass if _revision_identity(p) not in revised_ids]
    restore = unmatched[:gap]

    merged = list(revised) + restore
    if max_priorities and max_priorities > 0:
        merged = merged[:max_priorities]

    # Only report what actually survived the cap — claiming to restore an item
    # the cap then trimmed would be a lie in the audit trail.
    surviving = merged[len(revised) :] if len(merged) > len(revised) else []
    restored = [(getattr(p, "title", "") or "(untitled)") for p in surviving]
    return merged, restored


def _reattach_channel_entities(
    priorities: list,
    signal_by_id: dict,
    *,
    channel: ChannelKind,
    kind_attr: str,
    kinds: frozenset[PriorityKind],
    stamp_attrs: bool = False,
) -> None:
    """Deterministically stamp one channel's entity keys onto its priorities.

    Channel agents emit observation only; the decision-maker LLM assigns
    entity_kind / entity_id / priority_kind — but the entity_ids of a structured
    source (an EOS row UUID, a promise id) are opaque strings the model
    routinely garbles. For a priority that IS about one of ``channel``'s
    entities we overwrite those fields (and the target person, for someone
    else's item) from the signal itself, so the UC-25 memory key and the
    auto-resolve hooks always agree. ``kind_attr`` names the attr the agent
    stamped its ``PriorityKind`` into (``eos_priority_kind`` /
    ``promise_priority_kind``); ``kinds`` is that channel's closed set of
    ``PriorityKind``\\ s. Mutates ``priorities`` in place.

    Generalised from the EOS-only version of this fix; the
    "about, not merely cited" gate below is that fix, parameterised.

    ``stamp_attrs`` additionally copies the PRIMARY signal's allow-listed attrs
    onto the card (see :data:`~rubik_core.priorities.models.CARD_ATTR_ALLOWLIST`).
    Only the promises pass asks for it. It rides the same "about, not merely
    cited" gate below, so a card that only CITES a promise never wears that
    promise's facts any more than it wears its entity key.

    **Only re-keys a priority that is genuinely ABOUT this channel's entity.**
    This used to fire for ANY priority merely CITING such a signal, which
    hijacked the identity of priorities that only used a To-Do or a Rock as
    supporting evidence. The canonical breakage:
    "Prepare for your Team L10 meeting" cited calendar + eos + drive, and was
    stamped ``todo/769abf3d…/eos_todo_due`` — a meeting-prep item wearing a
    To-Do's key, while still carrying the calendar ``series_entity_id`` that
    gives the mis-key away. Consequences: ticking the meeting card off marked
    the unrelated To-Do resolved (which is what poisoned that user's feed for
    8+ days), and the meeting-prep auto-resolve hook could never fire because
    the item wasn't keyed to the event.

    The model is reliable about ``priority_kind`` — a small closed enum it only
    has to copy — and unreliable about opaque UUIDs, so trust the kind and
    repair the id:

    * kind is one of ``kinds``  → re-key (repair the garbled id).
    * EVERY cited signal is this channel's → re-key (the item can only be about
      this channel, so the model simply mislabeled the kind).
    * otherwise → LEAVE IT ALONE. A foreign kind on a mixed-citation priority
      means the model is telling us this is about a meeting / thread / deal
      that merely references this channel's work.
    """
    for p in priorities:
        cited = [
            signal_by_id[sid] for sid in (p.signal_ids or []) if sid in signal_by_id
        ]
        sigs = [s for s in cited if s.channel == channel]
        if not sigs:
            continue
        # See the docstring: re-key only what is genuinely this channel's item.
        if not (p.priority_kind in kinds or len(sigs) == len(cited)):
            continue
        primary = sigs[0]
        attrs = primary.attrs or {}
        kind_str = attrs.get(kind_attr)
        if kind_str:
            try:
                p.priority_kind = PriorityKind(kind_str)
            except ValueError:
                pass
        if primary.entity_kind is not None:
            p.entity_kind = primary.entity_kind
        if primary.entity_id:
            p.entity_id = str(primary.entity_id)
        if not p.entity_label:
            p.entity_label = primary.entity_label
        if not attrs.get("is_own"):
            p.target_email = attrs.get("owner_email")
            p.target_label = attrs.get("owner_name")
        if stamp_attrs:
            # The card's own facts, from the entity the card is ABOUT. Narrow
            # by construction — `filter_card_attrs` is an allow-list, so a new
            # attr on the signal does not silently reach the browser. Assigned
            # unconditionally so a re-run never leaves yesterday's basis on a
            # card whose primary signal has since dropped it.
            p.attrs = filter_card_attrs(attrs)

        # One card routinely speaks for SEVERAL sibling items ("two overdue To-Dos").
        # Record every one of them, not just the first, so acting on the card suppresses
        # the whole set — see PriorityCandidate.covered_entity_ids. Only siblings of the
        # SAME (entity_kind, priority_kind) qualify: a memory key is (kind, id,
        # priority_kind), so folding in a rock cited by the same card would write a
        # bogus row.
        covered: list[str] = []
        covered_labels: list[str] = []
        covered_own: list[bool | None] = []
        for sib in sigs:
            sib_attrs = sib.attrs or {}
            if (
                sib.entity_kind != primary.entity_kind
                or sib_attrs.get(kind_attr) != kind_str
                or not sib.entity_id
            ):
                continue
            sib_id = str(sib.entity_id)
            if sib_id not in covered:
                covered.append(sib_id)
                covered_labels.append(sib.entity_label or sib.title or "")
                # Ownership PER sibling, in the same slot as the id. Siblings
                # are grouped on (entity_kind, kind) alone, so one card can
                # legitimately mix the reader's item with a colleague's — and
                # the card-level answer (derived from the PRIMARY's
                # target_email) then says nothing about the sibling. Absent on
                # channels that don't decide ownership → None, which means
                # "unknown", never False.
                own = sib_attrs.get("is_own")
                covered_own.append(None if own is None else bool(own))
        # Single-entity cards carry nothing — the primary key already covers it.
        if len(covered) > 1:
            p.covered_entity_ids = covered
            p.covered_entity_labels = covered_labels
            p.covered_entity_is_own = covered_own
        else:
            p.covered_entity_ids = []
            p.covered_entity_labels = []
            p.covered_entity_is_own = []


def _reattach_eos_entities(priorities: list, signal_by_id: dict) -> None:
    """Re-stamp EOS (Rock / To-Do / Issue / Milestone) keys. See above."""
    _reattach_channel_entities(
        priorities,
        signal_by_id,
        channel=ChannelKind.EOS,
        kind_attr="eos_priority_kind",
        kinds=EOS_PRIORITY_KINDS,
    )


# The two kinds that mean "prepare for this upcoming meeting". Their
# auto-resolve hook fires on the EVENT (running meeting prep clears them — see
# PriorityKind), so they are only correct when keyed to the calendar event.
_MEETING_PREP_KINDS: frozenset[PriorityKind] = frozenset(
    {PriorityKind.UPCOMING_PREP, PriorityKind.PREP_DEBT}
)


#: The kinds a promises-channel card may carry. ``commitment_debt`` is here so
#: a card the model filed as commitment debt but built ON a ledger promise is
#: re-keyed to that promise (the prompt forbids the duplicate; this enforces it).
PROMISE_CARD_KINDS: frozenset[PriorityKind] = frozenset(
    {
        PriorityKind.PROMISE_DUE,
        PriorityKind.PROMISE_UNKEPT,
        PriorityKind.COMMITMENT_DEBT,
    }
)


def _reattach_promise_entities(priorities: list, signal_by_id: dict) -> None:
    """Re-stamp promise keys and kinds from the cited promises signal.

    Same "about, not merely cited" gate as the EOS pass, and
    ``stamp_attrs`` so the card carries the words, when and where they were
    said, and what was checked — the "You said you would…" card is not
    honest without them.
    """
    _reattach_channel_entities(
        priorities,
        signal_by_id,
        channel=ChannelKind.PROMISES,
        kind_attr="promise_priority_kind",
        kinds=PROMISE_CARD_KINDS,
        stamp_attrs=True,
    )


def _reattach_calendar_entities(priorities: list, signal_by_id: dict) -> None:
    """Repair the EVENT key on meeting-prep priorities. Mutates in place.

    The mirror of :func:`_reattach_eos_entities`, and the other half of the
    mis-keying fix. Stopping the EOS overwrite from
    hijacking a meeting-prep priority only guarantees the model's OWN key
    survives — and Google event ids are opaque strings the model garbles
    exactly like EOS row UUIDs. A garbled event id is quieter than a hijacked
    one but still wrong: dismiss/resolve writes a ``priority_memory`` row
    against an id that no event has, so the choice never sticks, and the
    meeting-prep auto-resolve hook never clears the card.

    The calendar agent already tags each signal with the authoritative
    ``entity_kind=EVENT`` / ``entity_id=<event_id>`` (its ``ChannelSignal.id``
    IS the event id), so the correct key is sitting right there on the cited
    signal.

    Deliberately narrow, so this cannot become the bug it fixes:

    * only ``upcoming_prep`` / ``prep_debt`` — the kinds that mean "prep for
      this meeting" and whose resolving action is running prep on the event.
      ``pre_touchpoint_followup`` is excluded on purpose: it is anchored to an
      upcoming meeting but is ABOUT a thread, so the event is not its identity.
    * only from a cited CALENDAR signal that actually carries an event id.
    """
    for p in priorities:
        if p.priority_kind not in _MEETING_PREP_KINDS:
            continue
        cal_sig = next(
            (
                signal_by_id[sid]
                for sid in (p.signal_ids or [])
                if sid in signal_by_id
                and signal_by_id[sid].channel == ChannelKind.CALENDAR
                and signal_by_id[sid].entity_id
            ),
            None,
        )
        if cal_sig is None:
            continue
        if cal_sig.entity_kind is not None:
            p.entity_kind = cal_sig.entity_kind
        p.entity_id = str(cal_sig.entity_id)
        if not p.entity_label:
            p.entity_label = cal_sig.entity_label


# The two kinds that mean "prepare for this upcoming meeting". Their
# auto-resolve hook fires on the EVENT (running meeting prep clears them — see
# PriorityKind), so they are only correct when keyed to the calendar event.
_MEETING_PREP_KINDS: frozenset[PriorityKind] = frozenset(
    {PriorityKind.UPCOMING_PREP, PriorityKind.PREP_DEBT}
)


def _attach_series_ids(priorities: list, signal_by_id: dict) -> None:
    """Stamp ``series_entity_id`` onto priorities about a recurring event.

    The calendar extractor already carries ``recurring_event_id`` in its attrs;
    this lifts it onto the priority so (a) the card can offer "never for this
    series" and (b) :func:`memory.apply_post_filter` can honor a series-level
    dismissal against a *different* occurrence's entity_id. Mutates in place;
    a priority with no recurring calendar signal is left untouched.
    """
    for p in priorities:
        if p.series_entity_id:
            continue
        for sid in p.signal_ids or []:
            sig = signal_by_id.get(sid)
            if sig is None or sig.channel != ChannelKind.CALENDAR:
                continue
            series = (sig.attrs or {}).get("recurring_event_id")
            if series:
                p.series_entity_id = str(series)
                break


def _resolve_playbook_text(department: str | None) -> str:
    """Return the concatenated playbook text the decision-maker will
    inject. Safe to call even when the playbook table is empty — yields
    an empty string and the decision-maker uses its generic fallback.
    """
    try:
        from rubik_core.priorities.playbook_store import resolve_effective

        effective = resolve_effective(department)
        return effective.concatenated_text
    except Exception:  # noqa: BLE001
        logger.warning("playbook resolution failed", exc_info=True)
        return ""
