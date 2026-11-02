UC-25 — Priority memory, auto-resolve, dismiss + snooze
=======================================================

Status: SCOPING
Depends on: UC-22 (entity tags), UC-24 (priority_kind)
Blocks:    nothing (final UC in this arc)

Problem
-------

UC-20 reads the most recent ``HomeCard`` row each day and either
returns it or runs the pipeline. Nothing tracks whether a previously
surfaced priority has been **acted on**, **dismissed**, or
**resolved**. Three consequences:

1. The same "prep debt" for a meeting will surface again the next day,
   even after the user ran meeting prep — annoying, breaks trust, and
   matches the user's #2 complaint from the diagram review.
2. There's no way for the user to tell the system "I'm not doing this"
   (dismiss) or "remind me later" (snooze).
3. When new evidence arrives that *should* re-surface a dismissed
   item (e.g. a fresh email on a thread the user dismissed last week),
   we have no way to detect it.

Scope
-----

A small, deliberate memory layer that:

1. Records the state of every priority the pipeline has ever surfaced,
   keyed by ``(user_email, entity_key, priority_kind)``.
2. Auto-resolves debts when downstream activity in another system
   evidences fulfilment (e.g. meeting-prep run resolves the prep
   debt; outbound email resolves the follow-up debt).
3. Surfaces ✓ done / 💤 snooze controls on each priority card. The UI
   posts to a small CRUD endpoint that writes to the memory table.
4. Feeds the decision-maker's filter at run time: signals tied to a
   resolved / dismissed / not-yet-due-to-resurface priority are
   demoted (the decision-maker still sees them but is told "the user
   has already chosen to ignore this — only surface it again if there
   is new material evidence").

Out of scope
------------

- Cross-user memory sharing (one user dismisses → another user
  doesn't see it). Memory is strictly per-user.
- Full activity log / audit trail UI. Memory state is observable via
  an admin debug endpoint; a polished history view can come later.
- ML-based resurface logic. Resurface uses simple heuristics: new
  signal ``timestamp > priority_memory.dismissed_at`` AND new
  signal carries the same entity.

Data model
----------

New table ``priority_memory``::

    CREATE TABLE priority_memory (
        id                   TEXT PRIMARY KEY,        -- uuid
        user_email           TEXT NOT NULL,
        entity_kind          TEXT NOT NULL,
        entity_id            TEXT NOT NULL,
        priority_kind        TEXT NOT NULL,
        state                TEXT NOT NULL,           -- 'active' | 'resolved'
                                                      -- | 'dismissed' | 'snoozed'
        snoozed_until        TIMESTAMP NULL,
        last_priority_id     TEXT NULL,               -- which PriorityCandidate.id
                                                      -- introduced this state
        resolved_evidence    JSONB NULL,              -- {kind: 'meeting_prep',
                                                      --  id: 'prep_artifact_xyz',
                                                      --  at: '2026-05-27T…'}
        first_seen_at        TIMESTAMP NOT NULL,
        last_seen_at         TIMESTAMP NOT NULL,      -- last time pipeline matched
        last_state_change_at TIMESTAMP NOT NULL,
        last_state_change_by TEXT NULL,               -- user email for manual
                                                      -- transitions; 'system'
                                                      -- for auto-resolve
        UNIQUE (user_email, entity_kind, entity_id, priority_kind)
    );

    CREATE INDEX idx_priority_memory_state
      ON priority_memory (user_email, state);
    CREATE INDEX idx_priority_memory_snoozed
      ON priority_memory (user_email, snoozed_until)
      WHERE state = 'snoozed';

State machine
-------------

::

       ┌───────────┐   user clicks ✓     ┌────────────┐
       │  active   │  ─────────────────► │  resolved  │
       │           │                     └────────────┘
       │           │   user clicks 💤    ┌────────────┐
       │           │  ─────────────────► │  snoozed   │
       │           │                     └─────┬──────┘
       │           │  ◄──────────────────────  │ snoozed_until passed
       │           │                           │ on next pipeline run
       │           │                           ▼
       │           │   user clicks ✗     ┌────────────┐
       │           │  ─────────────────► │ dismissed  │
       │           │                     └─────┬──────┘
       │           │  ◄──────────────────────  │ new signal arrives
       │           │      (resurface heuristic)│
       │           │                           ▼
       │           │   system detects     ┌────────────┐
       │           │   downstream evidence│  resolved  │
       │           │  ─────────────────►  └────────────┘
       └───────────┘

Auto-resolve hooks
------------------

Each is a one-line call from an existing code path; the memory module
exports a small façade.

``backend/services/meeting_prep_service.py`` (or wherever the prep is
persisted) — after a successful prep run::

    priority_memory.resolve(
        user_email=user.email,
        entity_kind=EntityKind.EVENT,
        entity_id=event_id,
        priority_kind=PriorityKind.PREP_DEBT,
        evidence={"kind": "meeting_prep", "id": prep_id},
    )

``rubik_core/tools/gmail.py`` (or the equivalent send-draft path) —
when the user actually sends an email::

    priority_memory.resolve(
        user_email=...,
        entity_kind=EntityKind.THREAD,
        entity_id=thread_id,
        priority_kind=PriorityKind.FOLLOW_UP_DEBT,
        evidence={"kind": "outbound_email", "message_id": msg_id},
    )

``backend/routers/agents.py`` or the future ClickUp/ticketing integ —
when a task is created for a commitment::

    priority_memory.resolve(
        ...,
        priority_kind=PriorityKind.COMMITMENT_DEBT,
        evidence={"kind": "task_created", "task_id": …},
    )

These hooks are best-effort — failures log a warning but never abort
the upstream action.

API endpoints
-------------

``POST /api/homefeed/priorities/{priority_id}/dismiss``
  Body: ``{}``. Sets state ``dismissed``. The priority is identified by
  the ``PriorityCandidate.id`` from the most recent bundle; the server
  looks up the matching ``(entity_kind, entity_id, priority_kind)`` and
  updates the memory row.

``POST /api/homefeed/priorities/{priority_id}/snooze``
  Body: ``{until: ISO date}``. Sets state ``snoozed`` with
  ``snoozed_until`` set.

``POST /api/homefeed/priorities/{priority_id}/resolve``
  Body: ``{evidence?: object}``. Manual ✓ — the user marks it done
  themselves (no auto-resolve hook fired).

``GET /api/admin/priorities/memory?user_email=…``
  Admin-only debug view. Returns the user's full memory table.

Pipeline integration
--------------------

At the top of ``PrioritiesOrchestrator.generate`` we load the user's
active memory into a lookup map::

    memory = priority_memory.load_active(user.email)
    # { (entity_kind, entity_id, priority_kind) : MemoryRow }

We pass this map into ``DecisionMakerAgent.decide`` as ``memory``.
The decision-maker prompt gains an explicit instruction::

    ## ALREADY HANDLED
    The following priorities are in a non-active state for this user.
    Do NOT re-surface unless new material evidence has appeared since
    the state change date.

    [{entity_kind}:{entity_id} / {priority_kind} → {state}
     since {last_state_change_at}; evidence: {evidence}]
    …

After the decision-maker returns its list, we run a deterministic
post-filter::

  For each candidate:
    key = (candidate.entity_kind, candidate.entity_id, candidate.priority_kind)
    if memory[key] is resolved → DROP
    if memory[key] is snoozed AND snoozed_until > now → DROP
    if memory[key] is dismissed:
      if max(signal_timestamp for signal in candidate.signal_ids)
         > memory[key].last_state_change_at:
          KEEP (new evidence resurfaces it)
      else:
          DROP

Two layers (LLM hint + deterministic filter) on purpose — belt and
suspenders so a noisy LLM can't reintroduce a dismissed item.

Frontend changes (priority card)
---------------------------------

Each priority card on the Homefeed gains two icon buttons in the
top-right corner::

      ┌──────────────────────────────────────────────────────┐
      │ Prep Acme pitch deck                  [HIGH]  ✓  💤  │
      │ External pitch in 4 hours, CEO attending.            │
      │ _Next step:_ Block 30 min to review the deck         │
      │ _Sources:_ calendar, hubspot                         │
      └──────────────────────────────────────────────────────┘

``✓`` resolves the priority. ``💤`` opens a small popover with
"Tomorrow / Next week / Custom…" date options. A toast confirms the
action and removes the card with a 200ms fade-out. Optimistic update;
server failures revert + show a toast error.

Test plan
---------

- ``test_priority_memory_upsert`` — basic insert + update path
- ``test_resolve_idempotent`` — calling resolve() twice = same state
- ``test_post_filter_drops_resolved``
- ``test_post_filter_drops_active_snooze``
- ``test_post_filter_passes_expired_snooze``
- ``test_post_filter_resurfaces_dismissed_on_new_evidence``
- ``test_dismiss_endpoint_admin_or_self``
- ``test_dismiss_endpoint_404_for_unknown_priority_id``
- ``test_meeting_prep_run_resolves_prep_debt`` — integration

Migration / rollout
-------------------

1. Alembic migration creating ``priority_memory`` + indexes.
2. Memory façade module (``rubik_core/priorities/memory.py``).
3. Wire the auto-resolve hooks (one-line edits in 2-3 places).
4. New endpoints under ``/api/homefeed/priorities/{id}/...``.
5. Frontend buttons + popover; optimistic update with rollback.
6. Decision-maker prompt updated to receive "already handled" list;
   orchestrator post-filter implemented.
7. End-to-end QA: surface a prep debt → run meeting prep → confirm
   it doesn't re-surface tomorrow.

Open questions
--------------

- How long does ``resolved`` last before the row is eligible for
  resurface? Recommend: indefinitely. Once resolved, it stays
  resolved unless explicitly dismissed-then-resurfaced via new
  evidence. Resolution is a durable judgment by the user.
- Do we expose the memory row to the user (e.g. a "previously
  dismissed" filter in the UI)? Recommend: not in v1. Add only if
  users ask for it.
- Should snooze offer "until specific event happens" (e.g. "snooze
  until they reply")? Recommend: not in v1. Time-based snooze is
  ~80% of the value.

Estimate
--------

Medium. The DB + endpoints are small; the prompt change is small;
the post-filter is small; the frontend popover + button placement is
the chunk of UI work. ~6-8 hours.
