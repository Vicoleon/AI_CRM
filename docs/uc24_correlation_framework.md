UC-24 — Cross-signal correlation framework + playbook injection
================================================================

Status: SCOPING
Depends on: UC-22 (entity-tagged signals), UC-23 (playbook persistence)
Blocks:    UC-25 (priority memory consumes the new bundle shape)

Problem
-------

UC-20's decision-maker is a single LLM call that picks priorities from
a flat signal list. Its prompt encodes Sales-specific bias ("external
> internal", "decision-stage deals", etc.) and never explicitly looks
for **cross-channel patterns** — e.g. "I sent an email and they didn't
reply AND the recipient has an open HubSpot deal." Users are left
guessing whether the model considered the relationships at all, and
the prompt won't transfer to a different department.

We need the decision-maker to apply a small, **domain-agnostic** set
of correlation meta-rules to *any* combination of entity-tagged
signals, and to take its domain knowledge from a department-specific
playbook (UC-23) instead of hardcoded sales language.

Scope
-----

Rewrite the decision-maker (and the critic that guards it) so that:

1. The decision-maker's system prompt encodes a closed set of **five
   correlation meta-rules** that apply to every department.
2. The user's department is resolved at run time, and the
   concatenation of *active playbooks for that department + default
   playbook* is injected as the **only department-specific text** in
   the prompt.
3. Signals are pre-grouped by ``(entity_kind, entity_id)`` before
   being shown to the LLM, so cross-channel correlation is structurally
   obvious (the LLM sees "person:ceo@acme.example appears in 3 signals: a
   sent email thread, a HubSpot deal, a calendar event next Tuesday").
4. Every priority the decision-maker emits **MUST** cite ≥2 signal_ids
   OR a single signal carrying a concrete future deadline. The critic
   enforces this.

The five meta-rules
-------------------

These are baked into the decision-maker system prompt (never edited
by admins; they're the engine, not the policy):

1. **Action without response** — a user-originated signal followed by
   silence over a threshold. Concrete forms:
     - Outbound email + no reply
     - Meeting held + no follow-up email
     - Slack message sent + no reaction
   ➤ Generates a **follow-up debt** priority.

2. **Commitment without fulfillment** — a transcript or thread contains
   a first-person promise ("I will…", "I'll send…", "Let me…") and no
   subsequent signal evidences the promise being kept.
   ➤ Generates a **commitment debt** priority.

3. **Future obligation without preparation** — a calendar event or
   deadline is approaching for an entity that lacks any prep artifact
   (meeting prep run, Drive doc attached, recent thread on the topic).
   ➤ Generates a **prep debt** priority. (Persistence side handled
   by UC-25: when prep is run, the debt auto-resolves.)

4. **Deadline mention without calendar match** — a signal mentions a
   concrete future date ("by Friday EOD", "next Tuesday at 2pm") and
   no calendar event falls in that window for that entity.
   ➤ Generates a **scheduling debt** priority.

5. **High-value entity in silent state** — an entity flagged as
   high-value by the department playbook (e.g. a HubSpot deal in
   "Decision" stage, or a top-quartile campaign) has had no inbound
   *or* outbound signal in the last N days.
   ➤ Generates a **neglect debt** priority.

The decision-maker never invents priority kinds beyond these five.
The kind is recorded on the priority and used by UC-25 to auto-resolve
the right way.

Out of scope
------------

- Adding new meta-rules. If a department needs something unusual it
  goes in the playbook as instruction, not in the engine.
- Tuning thresholds (3 days vs 5 days, etc.) per department. Defaults
  live in the engine; playbooks can override in prose ("for our org,
  silence > 2 days on a Decision-stage deal is high urgency").

Data model changes
------------------

Extend ``PriorityCandidate`` (in ``rubik_core/priorities/models.py``)::

    @dataclass
    class PriorityCandidate:
        id: str
        title: str
        why_it_matters: str
        urgency: str

        # NEW: which meta-rule fired (for UC-25 auto-resolve routing)
        priority_kind: PriorityKind | None = None

        # NEW: the canonical entity this priority is about (for memory)
        entity_kind: EntityKind | None = None
        entity_id: str | None = None
        entity_label: str | None = None

        source_channels: list[ChannelKind]
        source_links: list[str]
        suggested_action: str
        signal_ids: list[str]

``PriorityKind`` enum::

    class PriorityKind(str, Enum):
        FOLLOW_UP_DEBT = "follow_up_debt"
        COMMITMENT_DEBT = "commitment_debt"
        PREP_DEBT = "prep_debt"
        SCHEDULING_DEBT = "scheduling_debt"
        NEGLECT_DEBT = "neglect_debt"

Entity grouping (decision-maker input)
--------------------------------------

Before invoking the LLM, the orchestrator builds an entity index from
the consolidated signals::

    entity_index = {
      "person:ceo@acme.example": [signal_id_1, signal_id_5, signal_id_7],
      "deal:hubspot_1234":   [signal_id_2, signal_id_7],
      "thread:gmail_abc":    [signal_id_1],
      ...
    }

The prompt shows the LLM:

1. The narrative + themes (unchanged from UC-20).
2. The full signal list (unchanged).
3. **NEW**: an "Entities with multiple signals" section listing every
   entity referenced by ≥2 signals, with the signal ids pointed back.
   This makes correlation structural — the LLM doesn't have to discover
   the joins, the join is on screen.

Department resolution
---------------------

At ``PrioritiesOrchestrator.generate(request)``::

    user_email = request.user_email
    department = _lookup_department(user_email)        # users.department
    playbook_text = _resolve_effective_playbook(department)
                    # = concat of (active dept playbooks) + (active default playbooks)
                    # ordered by sort_order ascending

    consolidated = self.summarizer.consolidate(digests)
    priorities = self.decision_maker.decide(
        consolidated,
        department=department,
        playbook_text=playbook_text,
        ...
    )

The playbook text is injected as a single labeled section in the
decision-maker system prompt::

    ## DEPARTMENT PLAYBOOK ({department})
    {playbook_text}

If ``playbook_text`` is empty (department has no playbook AND no
default), the decision-maker falls back to a minimal generic note
("Prefer high-impact, decision-bearing items the user can act on
today"). Better than nothing; encourages admins to author at least
the default playbook.

Critic prompt update
--------------------

The critic gains one explicit check before scoring::

    BLOCKER CONDITIONS — each priority MUST satisfy at least one:
      • cites ≥2 signal_ids (cross-channel correlation), OR
      • cites exactly 1 signal_id AND that signal carries a concrete
        future date/amount in attrs.

    If a priority fails the blocker check, drop the overall score by
    1.5 points and list it under weak_rationales.

This is what enforces "the system always correlates" — even if the
decision-maker has a bad run, the critic forces a revision.

Test plan
---------

- ``test_entity_index_groups_signals_by_entity_key`` — unit
- ``test_decision_maker_priorities_cite_two_signals_when_grouped``
  — give it 2 signals about the same person, assert ≥2 signal_ids
- ``test_critic_drops_score_for_unsupported_priorities``
- ``test_department_lookup_uses_users_table_dept``
- ``test_playbook_injection_includes_default_when_dept_has_none``
- ``test_correlation_meta_rules_are_present_in_system_prompt``
  — regression test against the 5 meta-rule headings

Migration / rollout
-------------------

1. Land the model + enum changes (additive — old code keeps working).
2. Plumb department + playbook resolution through orchestrator.
3. Rewrite decision-maker and critic prompts.
4. Update the seed default playbook (UC-23 migration) to contain a
   short "what counts as high-value here" stub admins can refine.
5. Update existing tests; add new ones from the test plan.

Open questions
--------------

- Should we surface ``priority_kind`` in the UI (chip on each card)?
  Recommend: yes — useful for users to understand "why am I seeing
  this" and aligns with UC-25's auto-resolve UX.
- Should the entity index be computed by the LLM or in Python?
  Recommend: Python. Deterministic, cheap, and removes one source of
  hallucination.

Estimate
--------

Medium. Prompt-engineering risk is the main unknown; tests need to
verify the 5 meta-rules actually fire as expected. ~5-7 hours.
