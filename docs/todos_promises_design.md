# Cubie To-Dos — promises Cubie hears, tracks, and helps you keep

- **Status:** Phase 0 built on this branch (2026-09-14); Phases 1–3 remain the plan below
- **Date:** 2026-09-14
- **Branch:** `claude/todo-feature-redesign-52b8b3`
- **Scope:** a personal to-do system for every Cubie user. EOS To-Dos (`eos_todos`) and Sales Cockpit commitments stay where they are and are *bridged*, not replaced.
- **Since:** Help Desk was removed after this design was written. Its ticket bridge (`ticket_transitioned` evidence, `object_kind="ticket"`, the Productivity → Tasks tab) no longer applies.
- Line numbers below are as of this branch's HEAD and will drift.

## 1. Why every to-do app fails, and why Cubie can be different

A to-do app is a text box and a checkbox. Everything wrong with it follows from that:

1. **Capture is manual.** Most commitments are made out loud in a meeting or in a reply, and never get written down. The list only knows what you remembered to type.
2. **Completion is manual.** You did the thing at 4:12pm and forgot to tick it. Lists rot with done-but-unchecked items, and the rot destroys trust in the list.
3. **A to-do is a string.** It does not know what it is about, who is waiting for it, or what "done" looks like. To act on it you leave the list and hunt for the email, the doc, the deal.
4. **Slipping is silent.** An overdue item turns red and the person you promised hears nothing. The relationship pays for the list's silence.
5. **The list only grows.** No app has a good answer for "you have pushed this five times." So the item sits there, generating guilt.
6. **The list is one-sided.** You cannot see what others owe you unless they use the same tool and type it in.

Cubie is the only tool in the company that already sees the meeting transcript, the email thread, the calendar, HubSpot, Slack and the EOS ledger for every user. That changes what a to-do can be:

> **A Cubie to-do is a promise: something you said you would do, for someone, by when — that Cubie heard, keeps track of, and can tell when you have kept.**

Six things follow from that definition, and each one is something no to-do app does today:

| # | Principle | What the user experiences |
|---|-----------|---------------------------|
| P1 | **Heard, not typed** | "Cubie heard you promise Priya the Q3 deck by Friday" appears the morning after the meeting. One tap accepts it. Typing is the fallback, not the default. |
| P2 | **Kept, not checked** | When the deck goes out on the thread Priya is on, the item moves to "Looks done, confirm?" You confirm, you never hunt for the checkbox. |
| P3 | **Every to-do knows who is waiting** | The list groups by *who you owe*, not by project or tag. "3 people are waiting on you" is the headline number. |
| P4 | **Slipping is a conversation** | Snooze past the promised date and Cubie drafts the heads-up to Priya. A promise is never allowed to die silently. |
| P5 | **Lists never rot** | The third snooze opens a renegotiation: drop it, hand it off, do the 5-minute version, or recommit. Each option comes with the message already drafted. |
| P6 | **Two-sided** | If Priya was in the room, Priya's Cubie shows "Jordan owes you the Q3 deck by Friday" under *You're waiting on*. Nobody typed anything. |

Everything else a to-do app has (projects, tags, drag-to-reorder, subtasks, recurring chores) is deliberately **not built**. Cubie ranks; the user does not sort. Recurring reminders already exist as `scheduled_tasks`.

## 2. What Cubie already has (and throws away)

The rethink is cheap because most of the machinery exists. What is missing is a durable, user-owned record.

| Capability | Where it lives today | Gap |
|---|---|---|
| Hearing first-person promises in transcripts | `rubik_core/priorities/agents/meet_agent.py` prompt asks for `commitments_made_by_user`; Fathom action items go into RAG text (`rubik_core/integrations/fathom.py`) | Output lives only in one night's `ChannelSignal.attrs`. **Never persisted.** |
| "Promise without fulfilment" as a priority | `PriorityKind.COMMITMENT_DEBT`, decision-maker meta-rule 5 (`agents/decision_maker.py`) | Re-derived nightly from the top 2 transcripts; the promise itself has no identity across days |
| Resolve / snooze / dismiss with anti-refire memory | `priority_memory` + `apply_post_filter` (`rubik_core/priorities/memory.py`) | Fully general; keyed `(user, entity_kind, entity_id, priority_kind)` |
| Evidence-based auto-resolve | UC-27 outbound reply resolves `FOLLOW_UP_DEBT`; draft-from-transcript resolves `COMMITMENT_DEBT` (`backend/routers/email.py`) | Resolves a *card*, not a durable item |
| Aging clock, escalation ladder, "was this ever late" | `commitments` + `rubik_core/cockpit/commitment_engine.py` | Sales-only (`sow`/`wrapup`/`lead_touch`), keyed on HubSpot deal/company |
| Owned, due-dated team task | `eos_todos` (`rubik_core/database/eos/todos.py`) | Two states, owner must be an `eos_people` row, team-visible by design |
| Per-entity smart actions on a card | `AIPrioritiesPanel.tsx` (`entityAction()`) | No "add to my list", no free-text capture |
| Slack capture | `/todo` modal (`backend/routers/slack_commands.py`) | Creates EOS To-Dos only |
| Slack morning DM | `backend/agents/slack_delivery.py` | One URL button, **zero interactive callbacks** |
| Generic personal item with label/detail/due/urgency | `offboard_items` | Only for inherited work |

Two assets are worth inheriting verbatim:

- **`priority_memory`** is the surfacing engine. The new system must feed it as a channel, not reinvent it. Its documented failure mode (one `priority_kind` per *situation*, never per entity; snooze-only for recurring conditions) applies directly.
- **`commitment_engine`'s three invariants**: *Cubie owns the clock*; *silence is a feature* (a DB-level send guard, not a convention); *no silent auto-close* ("a wrong auto-close is worse than no auto-close"). All three carry over.

One warning from history: the Homefeed **Action Items** column was deleted in UC-39 as "strictly weaker duplication of AI Priorities." This design is not that column. Action Items re-extracted a list every morning and owned nothing. A promise is a durable record the *user* owns, with identity, a counterparty, evidence and a lifecycle. AI Priorities stays Cubie's *opinion* about today; the to-do list is the user's *ledger* of what they owe. Priorities cite the ledger; the ledger never tries to be the feed.

## 3. Product design

### 3.1 The page: `/todos`

```
┌──────────────────────────────────────────────────────────────────────────┐
│ To-Dos                                          3 people are waiting on you │
│                                                                              │
│ ▸ Cubie heard you promise…                                      (2 new)      │
│   ┌──────────────────────────────────────────────────────────────────────┐   │
│   │ Send Priya the Q3 pipeline deck            by Fri Sep 18              │   │
│   │ "I'll get you the deck before Friday" — Ops sync, Tue 10:04          │   │
│   │                                     [ Yes, add it ]  [ Not mine ]    │   │
│   └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│ ▸ Looks done — confirm?                                          (1)         │
│   ┌──────────────────────────────────────────────────────────────────────┐   │
│   │ Reply to Initech on pricing                                          │   │
│   │ Cubie saw your reply on that thread at 4:12pm yesterday              │   │
│   │                                     [ ✓ Kept ]   [ Not yet ]         │   │
│   └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│ ▸ Waiting on you                                                             │
│   Priya Shah (2)                                                             │
│   ┌──────────────────────────────────────────────────────────────────────┐   │
│   │ ● Q3 pipeline deck                       Fri Sep 18   [ Draft it ]   │   │
│   │ ● Vendor list for the offsite            Mon Sep 21   [ Open doc ]   │   │
│   └──────────────────────────────────────────────────────────────────────┘   │
│   Globex — Sam (1)                                                           │
│   ┌──────────────────────────────────────────────────────────────────────┐   │
│   │ ● Revised SOW               slipping · promised Sep 11  [ Heads-up ] │   │
│   └──────────────────────────────────────────────────────────────────────┘   │
│   Just you (3)  …                                                            │
│                                                                              │
│ ▸ You're waiting on                                        (Phase 3)         │
│   ● Alex — decision on the conference budget            said Thu            │
│                                                                              │
│ ▸ Kept this week  11 of 14  ·  2 renegotiated  ·  1 went quiet   [ Recap ]  │
└──────────────────────────────────────────────────────────────────────────────┘
```

Sections, top to bottom, are ordered by *what needs a decision from you*, not by date:

1. **Cubie heard you promise…** — the suggestion tray. Never nags, never counts toward "waiting on you" until accepted. Each card quotes the sentence Cubie heard and links to the transcript or thread. "Not mine" teaches the extractor (it writes a `rejected` event, and repeated rejections on a source lower that source's confidence).
2. **Looks done, confirm?** — items where deterministic evidence arrived (§4.3). One tap. This is the checkbox, relocated to the moment it is true.
3. **Waiting on you** — open promises grouped by counterparty, most-owed first. Items without a counterparty group under *Just you*. Slipping items carry the *Heads-up* verb instead of the normal one.
4. **You're waiting on** — Phase 3, promises *others* made to you in shared contexts (§3.5).
5. **Kept this week** — the ledger footer. Links to the Friday recap.

### 3.2 Anatomy of a to-do

Every card carries: **title** (imperative noun phrase), **for whom** (person, company or deal, or nobody), **by when** (a local date, never a timestamp), **where Cubie heard it** (quote + link), a **primary verb**, and an overflow menu.

The **primary verb does the first 80% of the work**, chosen from the entity the promise is about, reusing the UC-39 verb table:

| Promise is about… | Primary verb | Reuses |
|---|---|---|
| a thread / a person | *Draft it* → Gmail draft pre-addressed, body seeded from the promise | `POST /api/email/drafts` |
| a transcript follow-up | *Draft follow-up* | `POST /api/email/drafts/from-transcript` |
| a document | *Open doc* / *Share doc* | Drive deep link |
| a deal / company | *Open in HubSpot* / *Log note* | HubSpot deep link, UC-46 write path |
| a meeting | *Generate prep* | `POST /api/homefeed/meeting-prep` |
| nothing specific | *Block 30 min* | `POST /api/calendar/focus-block` |
| any, when slipping | *Heads-up* → drafted slip message to the counterparty (§3.4) | new |

Overflow: *Snooze* (Tomorrow / 3 days / Next week / pick a date), *Hand off to…*, *Make it a team To-Do* (promote to `eos_todos`), *Private* (never visible two-sided), *Drop* (asks for a one-line reason only when a counterparty exists, and offers to send it).

### 3.3 Capture paths, in order of preference

1. **Heard** — transcripts (Meet, Fathom, L10 recordings), inbound/outbound email, Slack (where the user has connected it). Produces *suggestions*, never open items. See §4.2.
2. **From a priority card** — a new smart action on every `AIPrioritiesPanel` card: *Add to my To-Dos*. The promise inherits the card's entity, label and anchor date. This is how Cubie's daily opinion becomes a durable commitment when the user agrees with it.
3. **Chat** — "remind me to send Sam the SOW Thursday", "add a to-do: vendor list for the offsite". The chat agent resolves *Sam* to a person and *the SOW* to a Drive doc or deal where it can, and echoes the parsed promise for a one-tap confirm. Uses the existing directory and entity resolution.
4. **Slack `/todo`** — the modal gains a *Where* choice: **My list** (default) or **Team L10**. The existing `eos_todo_create` callback keeps working unchanged for the second option. (Open decision: default to *My list*, or keep EOS as default for the pilot teams that already use it.)
5. **Typed on the page** — one input at the top. Natural language, same parser as Chat.

### 3.4 Slipping and renegotiation (P4, P5)

Cubie owns the clock. `promised_for` is set once (by the user, or from the quote); `due_local` can move, and every move is an event. State moves to `slipping` at 09:00 local on the day after `due_local` if the item is still open, or the moment the user snoozes past it.

- **First slip:** the primary verb becomes *Heads-up*. Cubie drafts, in the user's voice (using the same signature and tone plumbing as the AI email drafts), a two-sentence note: what, new date, one line of reason left blank. The user edits and sends, or dismisses the draft. Nothing is sent automatically.
- **Third snooze, or 7 days slipping:** the card turns into a four-way choice, each pre-drafted:
  - **Recommit** — pick a date you believe; Cubie drafts the heads-up.
  - **Shrink it** — Cubie proposes the 5-minute version ("send Priya the two slides you have instead of the full deck") and drafts that message.
  - **Hand off** — pick a colleague; they get it as a *suggestion* with the full context; Cubie drafts the note to both.
  - **Drop** — Cubie drafts the honest "I'm not going to get to this" note.
  There is no fifth option. Silent snoozing past this point is not offered.

### 3.5 Two-sided ledger (P6) — Phase 3

If Jordan promised Priya the deck in a meeting both attended, Priya's list shows it under *You're waiting on*. Rules, all enforced server-side:

1. The counterparty must be a user in the same workspace (`users` row). External counterparties never see anything.
2. The promise must come from a **shared context**: a transcript where both are in `meeting_participants`, or a thread where both are addressees. Typed promises are never two-sided unless the owner explicitly shares them.
3. The promise is visible to the counterparty **only after the owner accepts it** from the suggestion tray. Acceptance is the consent step. *Private* revokes it.
4. The counterparty sees title, owner, due date and status (open / kept / renegotiated). Never the quote, never the source, never the owner's snooze count.
5. Confidential meetings follow the existing exec-content fence and per-line confidentiality rules; the extractor runs under the owner's own transcript ACL and nothing else.

The counterparty gets one verb: *Nudge* — a drafted, friendly "still expecting this Friday?" DM. Cubie never nudges on anyone's behalf.

### 3.6 The weekly ledger

Friday afternoon, in the same Slack DM slot as the existing look-ahead, and on the page footer:

> You made 14 promises this week and kept 11. Two you renegotiated with a heads-up. One went quiet: the revised SOW for Sam at Globex, promised Sep 11. Here's a draft.

Kept-rate is per user and private in v1. For teams running L10, the To-Do review segment already targets 90% completion; Phase 3 pre-computes the team's kept-rate from `eos_todos` so the segment opens with the number instead of a poll of the room.

### 3.7 Where the AI is, specifically

- **Extraction** — one Gemini Flash call per new transcript (and per thread batch in Phase 1b) returning `{quote, title, counterparty, due, confidence}` per promise. Prompt lineage: `meet_agent.py` `commitments_made_by_user` + the Fathom action-items section.
- **Entity resolution** — counterparty and object resolution through the existing six-tier correlator (`backend/signals/correlator.py`), so "the deck" becomes a Drive id when it can.
- **Evidence judge** — deterministic matchers first (§4.3). Only when a matcher is ambiguous (an outbound email to the right person that may or may not be *the* deliverable) does a Flash call read the message and answer kept / not kept / unclear.
- **Drafting** — heads-up, shrink, hand-off and drop messages, in the user's voice, through the existing draft pipeline.
- **Shrink** — proposing the smaller deliverable is generative and is the one place the model is asked to be creative.
- **Weekly narrative** — one call per user per week.

Everything else (ranking, grouping, the clock, the ledger math) is deterministic.

## 4. Integration into the Cubie ecosystem

### 4.1 Data model

New package `rubik_core/promises/`; new table `promises` (+ `promise_events`). Internal name **promise**, user-facing name **To-Do**. The internal name avoids colliding with `eos_todos`, `EntityKind.TODO` (which already means an EOS To-Do) and the `tasks_tab` feature (which is the Help Desk).

```
promises
  id                 uuid PK
  owner_email        text NOT NULL            -- lower-folded, NO FK (survives offboarding, like commitments)
  title              text NOT NULL
  detail             text NULL
  status             text NOT NULL            -- suggested | open | slipping | evidence_found
                                              -- | kept | dropped | handed_off
  promised_for       date NULL                -- set once; the original promise
  due_local          date NULL                -- movable; LOCAL date, never an instant
  timezone           text NOT NULL            -- owner's tz at creation (per-user Settings)
  counterparty_kind  text NULL                -- person | company | deal
  counterparty_id    text NULL                -- email / hubspot id
  counterparty_label text NULL
  object_kind        text NULL                -- thread | document | deal | event | transcript | ticket
  object_id          text NULL
  object_label       text NULL
  source             text NOT NULL            -- heard_transcript | heard_email | heard_slack
                                              -- | priority_card | chat | slack_cmd | page | handed_off
  source_ref         jsonb NULL               -- {entity_kind, entity_id, quote, span, at}
  confidence         numeric NULL             -- extraction confidence (heard_* only)
  done_rule          jsonb NULL               -- what "kept" looks like, see 4.3
  evidence           jsonb NULL               -- {kind, ref, at, judge: deterministic|llm}
  snooze_count       int NOT NULL DEFAULT 0
  slipping_since     timestamptz NULL
  is_private         bool NOT NULL DEFAULT false
  visible_to_counterparty bool NOT NULL DEFAULT false   -- true only after owner accepts a shared-context promise
  eos_todo_id        uuid NULL                -- bridge when promoted
  created_at / updated_at / kept_at / dropped_at

promise_events
  id, promise_id FK CASCADE, at, actor (email | 'system'), kind
  -- suggested | accepted | rejected | due_moved | snoozed | slipping
  -- | headsup_drafted | headsup_sent | renegotiated | evidence_found
  -- | kept_confirmed | kept_denied | dropped | handed_off | promoted_eos | made_private
  payload jsonb
```

Rules carried over from the codebase's own lessons:

- `due_local` and `promised_for` are **dates**, not instants. The scorecard and EOS to-do memory notes are explicit that due dates must stay local-midnight; `parseInstant` must not be applied to them on the client.
- `owner_email` has no FK, matching `commitments` and `home_cards`, so a departed teammate's ledger survives for the offboard sweep. Offboarding adds `kind="promise"` to `offboard_items` so open promises flow to the manager through the existing Handoffs page.
- Alembic: number off `origin/main`'s head, check every worktree, single head in CI.

### 4.2 Extraction worker (Phase 1)

`rubik_core/promises/extract.py`, run on **cubie-worker** as a Cloud Run Job, never fire-and-forget from the request path (Cloud Run throttles CPU after the response).

- **Trigger:** the transcript ingest paths (`backend/services/transcript_service.py`, `rubik_core/call_capture/transcript_ingest.py`) enqueue `(transcript_id, participant_emails)` with the same debounced watermark-and-claim pattern the EOS re-ingest hook uses. A nightly backstop sweeps transcripts from the last 48h that have no `promise_events` row, scheduled **before** `priorities-run-all` at 06:00 UTC so the morning feed can cite fresh suggestions.
- **Per participant:** the extractor runs once per transcript and fans promises out to each speaker who is a workspace user, under that user's transcript ACL (`meeting_participants`). Non-participants never receive a suggestion from a meeting they were not in.
- **Output:** `status="suggested"`, `source="heard_transcript"`, `source_ref` with the quote and span, `confidence`. Below a threshold (start at 0.6) nothing is written. Duplicates collapse on `(owner_email, normalized title, counterparty_id, ±3 days)`.
- **Cost:** `@attribute_to("todos")` (`rubik_core/cost/usage.py`) on the worker plus a `_FEATURE_LABELS` entry in `backend/routers/admin.py`, same PR. Never ship into "Unattributed".
- **Phase 1b:** email. Runs as a step inside the existing nightly Gmail channel read, on threads where `direction=i_sent_last`, asking one extra question: did the reader promise something with a date. Same output shape.

### 4.3 Evidence and "kept" (P2)

`rubik_core/promises/evidence.py`. `done_rule` is derived at acceptance from the object and counterparty:

| `done_rule.kind` | Deterministic evidence | Where it hooks |
|---|---|---|
| `email_sent_to` | outbound message on any thread whose recipients include the counterparty, after `accepted_at` | the UC-27 outbound hook in `backend/routers/email.py` already resolves `FOLLOW_UP_DEBT`; add a promise lookup beside it, plus the nightly Gmail read for mail sent outside Cubie |
| `doc_shared_with` | Drive permission added for the counterparty on `object_id` | Drive channel read |
| `hubspot_changed` | property / stage change on the deal after `accepted_at` | HubSpot channel read (live API) |
| `invite_sent` | calendar event created with the counterparty as attendee | Calendar channel read |
| `ticket_transitioned` | Help Desk ticket moved to `resolved`/`closed` | tickets channel read |
| `manual` | none; the user confirms | — |

Evidence moves status to `evidence_found` and the card to *Looks done, confirm?*. **It never moves to `kept` on its own.** That is the `commitment_engine` invariant, kept deliberately: a wrong auto-close hides a real broken promise. Revisit after the pilot measures the confirm-rate; if it is above ~95% for `email_sent_to`, auto-keep with a 24h undo is a one-line policy change.

Ambiguous matches (right recipient, unclear content) go to a Flash judge that reads only the matched message. Its verdict is stored in `evidence.judge`.

### 4.4 AI Priorities integration

The ledger becomes a **deterministic channel**, exactly like `INHERITED` and `TICKETS`:

- `ChannelKind.PROMISES` — a reader over `promises` for the run's user, no LLM.
- `EntityKind.PROMISE` — new; `entity_id` is the promise uuid.
- One `PriorityKind` **per situation** (the memory-poisoning lesson):
  - `PROMISE_DUE` — open, due within the horizon. Resolves when kept; auto-resolved by the kept hook via `resolve_entity_everywhere`.
  - `PROMISE_SLIPPING` — recurring condition; card actions **snooze only**, never write `resolved`.
  - `PROMISE_CONFIRM_KEPT` — evidence waiting for a tap. Resolves on confirm or deny.
  - `PROMISE_SUGGESTED_BATCH` — "Cubie heard you promise 3 things yesterday", one card per day; snooze-only.
  - `PROMISE_OWED_TO_YOU` (Phase 3) — counterparty view; snooze-only.
- The decision-maker's meta-rule 5 (`commitment_debt`) is told that a promise already in the ledger is **not** a debt candidate; it cites `PROMISE_DUE` instead. This removes the nightly re-derivation of the same promise from the top 2 transcripts.
- `AIPrioritiesPanel.entityAction()` gains `promise` → *Draft it* / *Heads-up* / *Confirm kept*, and every card gains *Add to my To-Dos*.

### 4.5 Backend and gating

- Router `backend/routers/promises.py`, mounted on the `core` parent in `backend/main.py`. Endpoints: list (grouped), accept / reject suggestion, create (parsed text or structured), patch (due, private), snooze, kept / not-yet, drop, hand-off, promote-to-eos, heads-up draft, weekly recap.
- Gate every endpoint with `feature_allows("todos", user)`. No roster literal anywhere; CI checks `backend/auth.py`.
- `FeatureSpec(key="todos", label="To-Dos", surface="/todos, Homefeed, Chat, Slack /todo", group="Productivity", kill_switch="TODOS_ENABLED", default=everyone, exec_allowed=True)`. Business-outcome description and admin notes per the registry convention. Narrowing it to some departments is an Admin → Feature Access click, no deploy.
- `TODOS_ENABLED` in `rubik_core/config.py`, set on **both** the Service and cubie-worker (a flag set on the Service alone leaves the worker's jobs switched off).
- Frontend: `FEATURE.todos` in `AuthContext.tsx`, nav item and `/todos` route gated on `features.todos`; the page lives in `frontend/src/pages/Todos.tsx`, cards in `components/todos/`.
- Help page updated in both homes (`frontend/src/pages/Help.tsx`, `rubik_core/help/content.py`) in the same PR as each phase.
- Slack: extend `/slack/interactivity` dispatch with `action_id`s for **Kept / Snooze / Heads-up** buttons on promise lines in the morning DM. This is the first interactive callback in the DM; the slash-command modal path is the precedent and the signing/verification is already there.

### 4.6 The EOS bridge

- *Make it a team To-Do* creates an `eos_todos` row (owner resolved to `eos_people`, team from the user's primary or chosen department, no `l10_meeting_id` unless promoted from inside an L10) and stamps `eos_todo_id`. Completing either side completes both through `sync_todo_priority`.
- The `/todos` page shows the user's open `eos_todos` under *Waiting on you → Your team (L10)* as **read-through rows** with the existing EOS verbs. No second copy is stored.
- Sales Cockpit `commitments` for the user appear the same way under *Waiting on you → Customers*, read-through, with their own aging state. Help Desk tickets stay in Productivity → Tasks; a promise may *link* to a ticket via `object_kind="ticket"`, which is how `ticket_transitioned` evidence works.

One list, four systems of record. Only `promises` is a new writer.

## 5. Phases

| Phase | Ships | New LLM calls | Depends on |
|---|---|---|---|
| **0 — Ledger** | `promises` tables, router, `/todos` page (typed capture, grouping, verbs, snooze), *Add to my To-Dos* on priority cards, Chat intent, `/todo` *Where* choice, `PROMISES` channel + `PROMISE_DUE`, EOS and commitments read-through, feature gate, Help page | none (Chat parsing rides the existing chat agent) | — |
| **1 — Heard** | transcript extractor on cubie-worker, suggestion tray, deterministic evidence + *Looks done, confirm?*, `PROMISE_CONFIRM_KEPT`, `PROMISE_SUGGESTED_BATCH`, AI Cost category | 1 Flash call per new transcript; Flash judge on ambiguous evidence only | Phase 0 |
| **1b — Heard in email** | promise question added to the nightly Gmail channel read | marginal tokens on an existing call | Phase 1 |
| **2 — Never rot** | slipping state, heads-up drafts, renegotiation menu (recommit / shrink / hand off / drop), weekly ledger in Slack and on the page, interactive Slack buttons | drafts on demand; 1 call per user per week | Phase 1 |
| **3 — Two-sided** | *You're waiting on*, Nudge, consent rules, L10 kept-rate pre-compute, `PROMISE_OWED_TO_YOU` | none | Phase 2 + `meeting_participants` coverage on every transcript path |

Phase 0 is a complete, useful product on its own and is where leadership should first see it: the list, the grouping by who is owed, and the verbs that do the work. Phase 1 is the moment it becomes something nobody else has.

## 6. Success measures and kill rule

Measured on the pilot roster after four weeks of Phase 1, from `promise_events`:

| Measure | Target | What it tells us |
|---|---|---|
| Share of open promises whose `source` is `heard_*` | ≥ 50% | P1 works: Cubie, not typing, fills the list |
| Suggestion acceptance rate (accepted ÷ (accepted + rejected)) | ≥ 40% | extraction is signal, not noise |
| Share of `kept` reached via `evidence_found` | ≥ 30% | P2 works: done is detected |
| Confirm-rate on `email_sent_to` evidence | measured, no target | decides whether auto-keep is ever allowed |
| Promises that went `slipping` with no heads-up drafted | trending down week over week | P4 works |
| Weekly active users of `/todos` on the roster | ≥ 60% | people come back |

**Kill rule.** If suggestion acceptance is below 20% after two prompt iterations, the extractor is switched off (`TODOS_ENABLED` stays on) and Phase 0 stands alone as a good to-do list with smart verbs. If Phase 0 itself has under 30% weekly active use on the pilot roster after four weeks, stop and do not build Phase 2.

## 7. Risks and how the design absorbs them

- **False "heard" promises erode trust.** Suggestions are a tray, not a list; they never count, never nag, and *Not mine* is one tap and feeds back. Threshold starts conservative.
- **Wrong auto-close hides a broken promise.** No auto-close; evidence proposes, the user confirms. Inherited invariant.
- **"This is Action Items again."** It is not: durable, owned, with counterparty, evidence and lifecycle, feeding Priorities as a channel rather than competing with it. §2 states the distinction; the decision-maker is told about the ledger so the same promise is not re-derived nightly.
- **Duplicating EOS To-Dos.** Explicit bridge, read-through rows, one write path per system of record.
- **Two-sided visibility feels like surveillance.** Consent at acceptance, shared-context only, workspace users only, minimal fields, *Private* at any time, no manager dashboard in any phase.
- **Slack interactivity is new plumbing.** Contained to Phase 2; the modal callback path is the precedent.
- **Read-filter poisoning.** One `PriorityKind` per situation, snooze-only on recurring kinds, exactly as the tickets agent does.
- **Background work dying silently.** Extraction and the nightly backstop run on cubie-worker; failures are covered by the existing job-silence alerts.

## 8. Decisions (taken 2026-09-14)

1. Slack `/todo` defaults to **My list**; Team L10 stays one click away and unchanged.
2. Roster: **everyone in the workspace** by default (registry key `todos`), narrowed from Admin → Feature Access.
3. Phase 0 ships **with** the Chat tools (`create_todo`, `list_my_todos`, `mark_todo_kept`, `snooze_todo`).
4. UI says **My To-Dos** (the EOS area already has a "To-Dos" row at `/eos/todos`); code says *promise*.

What Phase 0 shipped, for the record: `promises` + `promise_events`, `rubik_core/promises/` (store, parse, read-through, priority sync), `/api/todos` on the `core` parent behind `TODOS_ENABLED` + registry key `todos`, the `promises` AI Priorities channel with `PriorityKind.PROMISE_DUE` / `EntityKind.PROMISE`, the `/todos` page and My Work nav row, *Add to my To-Dos* on priority cards, the Slack *Where* choice, the Chat tools, Help in both homes, and `TODOS_ENABLED=true` on the dev Service and worker deploys.

## 8a. Original decision list

1. `/todo` in Slack: default the *Where* choice to **My list**, or keep **Team L10** as default for the teams already using it.
2. Roster for `todos`: one department for Phase 1, or everyone from day one.
3. Whether Phase 0 ships with the Chat intent or the page-only input (the parser is shared; Chat adds intent-routing work).
4. Naming on the page: **To-Dos** (familiar, what people will say) versus **Promises** (says what is different). This document assumes To-Dos in the UI and *promise* in code.
