# Engineering notes

The rules and gotchas that are easy to break in this codebase, and why each one
exists. Read the section for the area you are changing before you change it.
When a rule here and the code disagree, fix one of them in the same change.

**Conventions in these notes**

- `<pkg>/` is the core Python package: the domain and persistence layer
  (models, agents, services, the RAG pipeline). `backend/` is the HTTP layer.
- Some settings carry the product's environment prefix. They are written here
  without it, behind `…_` (for example `…_WGLL_FEEDBACK_ENABLED`).
- "The worker job" is the shared Cloud Run Job that runs background work
  (EOS re-ingest, Assets ingest, scheduled tasks and so on). It does not inherit
  the Service's environment.

---

## 1. Architecture and conventions

### Package layout: `<pkg>/` vs `backend/`

Two top-level Python packages with a one-way dependency:

- **`<pkg>/`**: domain and persistence. SQLAlchemy models (`<pkg>/database/`),
  agent logic, services, the RAG pipeline. Nothing here imports `backend/` at
  module scope.
- **`backend/`**: FastAPI routers, auth middleware, dependencies. Imports
  freely from `<pkg>/`.

Models and business logic go in `<pkg>/`, route handlers in
`backend/routers/`. A subdomain gets a subpackage in both, for example
`<pkg>/database/eos/` and `backend/routers/eos/`.

A few deliberate lazy imports go the other way (`<pkg>` → `backend`) inside a
function, where duplicating an access gate would let two copies drift apart.
The Help tool's section gate and the EOS priorities agent are the examples.
Keep such imports inside the function.

### Feature access is data, not code

Who may reach a feature is decided in **Admin → Feature Access** (the
`feature_access` table), resolved by `<pkg>/features`:

- **`registry.py`** declares each feature once as a `FeatureSpec`: key, label,
  business-outcome description, where it shows up, admin notes, `membership`
  (primary-or-managed vs managed-only), an optional env `kill_switch`, and the
  access it ships with (`default`). Defaults name no department and no person:
  a user-facing feature ships open to everyone (`everyone=True`), the Loose
  Ends mailbox scan included (with both flags off, so a narrowed roster never
  sweeps an admin or exec in); a role-scoped one (admin dashboards, manager
  views, exec roll-ups, cross-person views) opens on the admin and Exec-team
  flags only.
  `backend/tests/test_default_access.py` sorts every key into one of the two.
- **`policy.py`** resolves the live roster: an admin override row wins, else the
  default. Reads are cached for 60 s per process and **fail safe to the code
  default** (missing table, database down): never open, never a 500.
  `feature_allows(key, user)` is the one call a gate makes; `resolve_all(user)`
  is what `/api/users/me` returns as `features`.
- **Admin → Feature Access** (`backend/routers/feature_access.py`,
  `frontend/src/pages/admin/FeatureAccessTab.tsx`) lists departments as
  checkboxes from the live list, plus Everyone, Exec team, admins and named
  emails, with a live "who gains or loses access" preview. Every write is
  audited.
- **Frontend:** `AuthContext.tsx` holds no rosters. `canAccess*` is
  `features.<key>`, and the `FEATURE` map lists every registry key
  (`test_feature_access.py` checks the two agree).

**A new gated surface does all of this in the same change, or it is not
gated:**

1. Add a `FeatureSpec` to `registry.FEATURES` with the access it ships with
   (open, or role-scoped), and list its key in `test_default_access.py`.
2. Gate the backend with `feature_allows("<key>", user)`. No frozenset of
   departments, no `settings.*_DEPARTMENTS` compare, no inline `dept in {...}`
   (CI fails on a roster literal in `backend/auth.py`).
3. Add the key to `FEATURE` in `AuthContext.tsx` and gate the nav and route on
   `features.<key>` (or `hasFeature(key)`), never on a department string.

Env kill switches decide whether a feature runs at all and stay in
`<pkg>/config.py`; a switched-off feature is a 404, not a permission problem.
Structural rules that are not rosters stay in code: the Smart Teams
Workspace-domain check, the cockpit transcript-text rule, Smart Teams raw-data
privacy. A feature key that is no longer in the registry (a deleted feature
still stored in `feature_access` or in a preference) is ignored, never a crash.

### Model ids come from settings

Never hardcode a model string. Read the tier from settings
(`VERTEX_LLM_MODEL_FLASH`, `…_PRO`, `…_LITE`) or from the admin's per-job
choice (Admin → Sales Models, Admin → AI Priorities), so a model change is a
config change.

Client-owned inference credentials are workspace-bound connector secrets.
`rubik_core/ai_providers` holds the fixed provider/model catalog and the
per-workload route. A route becomes selectable only when its call path and
capability contract are adapted. Text, JSON, agent tool calls and streaming,
Google Search grounding, audio/image input and embeddings have separate
activation probes. Each Chat turn, batch and multi-step analysis pins one
workspace route; a selected client provider failure is visible and never
falls back to managed Vertex. Cost events carry provider and billing owner,
and the admin page labels dollar figures as estimates rather than the
provider invoice. Local Chroma embedding changes rebuild a versioned index
before activation. GCP client-owned OpenAI/Gemini embeddings rebuild each
bound corpus into the workspace Cloud SQL pgvector tables, verify the exact
GCS source manifest, build a versioned HNSW index, then switch that corpus's
control pointer. RAG writes use a tenant outbox and sync active/candidate
versions; selected index or key failures stop search without falling back to
managed Vertex. Live Cloud SQL and provider-key smoke tests remain go-live
gates. Never switch
providers by changing a model string or reuse another workspace's key.

### Copilot routers mount on `core`

The copilot routers are mounted under one parent router, `core`, in
`backend/main.py`, which carries `Depends(require_full_app_access)`. **A new
copilot router must be added to `core`**, not to `app` directly, or it ships
ungated. The routers outside `core` are the ones that must work for everyone
who can sign in: all of `/api/eos/*`, `users` (`/users/me` bootstraps auth),
`departments`, and the admin routers, which keep their own `admin_required`
gates. Also outside `core` are the callers that carry no user token, each
behind its own credential, declared on the router or checked by every route:
the Cloud Scheduler endpoints (`X-Scheduler-Key`), Slack's slash commands and
interactivity (the selected installation's signing secret) and its OAuth
callback (signed workspace state), Cubie for Chrome (its per-browser key), and
the Fathom webhook, `POST /api/fathom/w/{slug}/webhook`. Middleware binds an
active URL workspace before tenant dependencies; the router then checks that
workspace's Fathom signature and refuses a missing signing secret. The old
shared URL is 404. A repeat of a workspace/event pair that instance already
accepted inside the replay window (ten minutes in a bounded in-process cache)
gets 200 "duplicate" and no work; a failed delivery is forgotten for retry.
The other Fathom routes stay on `core`. On the frontend, every
feature-gated route sits behind `<FeatureRoute feature="…">` in `App.tsx`
(copilot routes: `feature="copilot"`).

Nobody is locked out for having no department: everyone who passes the domain
check signs in, and the registry decides what they reach.

### Help has a backend mirror: update both

Help is one page per topic under `frontend/src/pages/help/`:

- **`helpTopics.ts`** is the registry: each topic's `id` (its URL,
  `/help/<id>`), title, panel label and group, one-line summary and gate
  (`requires`). The Help panel, the Help home, the `/help/:topicId` route and
  the previous/next links all read it.
- **`topics/<Topic>.tsx`** holds the words; **`topicPages.tsx`** maps id to page
  and icon; **`kit.tsx`** holds the shared blocks. A `<SubHeading id>` becomes a
  row in the page's "On this page" list.

Chat answers "how do I…" questions about the product through the Help search
tool. It cannot read the JSX (the image ships only the built `frontend/dist`),
so it reads **`<pkg>/help/content.py`**: the same guidance in plain text, keyed
by the same topic ids.

So "every shipped change updates Help" has two halves: **the topic's page and
its section in `<pkg>/help/content.py`, in the same change.**
`backend/tests/test_help_chat_link.py` fails if a topic is added, removed,
renamed or re-gated without a matching entry, but it only polices ids, titles
and gates. Prose drift is on you.

- **Topic ids are a public contract.** Chat cites topics as in-app links, and
  Help's example questions deep-link into Chat as `/chat?q=…`. Renaming an id
  breaks answers already sent. Links in the old `/help#<id>` form are
  redirected (`legacyHelpHashTarget`); keep that redirect.
- **Gated topics stay gated.** `HelpSection.requires` mirrors `requires` in
  `helpTopics.ts` (the test pins them equal), and the tool's
  `_help_allowed_section_ids` resolves the gate through `backend.auth` and
  fails closed.
- Retrieval is lexical (a scored token match over a fixed set of sections), not
  vectors: instant, free, and it cannot fail mid-turn. If a real question
  misses, add a keyword to the section.

### A department's identity is its RAG slug

The `departments` table key is the raw human label, but a department's
identity is its slug (`slugify_department` in `<pkg>/rag/config.py`, which
lowercases and collapses punctuation). `"Sales"`, `"sales"` and `" Sales "` are
the same team.

- **Compare departments by slug**, every time:
  `slugify_department(a) == slugify_department(b)` in Python (see `_dept_key` in
  `<pkg>/auth/access.py`), or `func.lower(col) == value.lower()` in SQL. Never a
  bare `asset.department == user.department` or `user.department == "systems"`.
  `add_department` rejects any slug-equivalent of an existing row.
- **The rule covers display and grouping too.** Duplicate rows can still exist
  (a direct `db.add(Department(...))` bypasses the guard), so any surface that
  lists departments must fold them itself: fold by slug, keep the spelling with
  capitals, and drop `general` (the unassigned default of `users.department`,
  never a real team). `_canonical_departments` in `backend/routers/eos/me.py`
  does this; the frontend mirrors it in `frontend/src/pages/eos/_utils.ts`
  (`deptSlug`, `preferDeptLabel`, `matchesTeamFilter`). A picker that offers one
  spelling must fold case when it filters, or it hides rows tagged with the
  other.

---

## 2. Database and migrations

### Connection-pool budget

`instances × (pool_size + max_overflow)` must stay under the database server's
`max_connections`, with room for migrations and local tools. The app pool is
`pool_size=5, max_overflow=5` (at most 10 per instance). A live L10 with several
attendees once exhausted a small server's ~25 connections and returned 500s on
the whole EOS surface. With one database per workspace, pools multiply per
workspace as well as per instance, so budget for both. Local pytest and
`alembic` runs count too unless `CLOUDSQL_CONNECTION_NAME` is blank.

### Hold no connection across slow work

Handlers on long paths (a model call, an external API, a big build) do not
declare `Depends(get_db)`. The work runs in the threadpool and opens its own
short `SessionLocal()` blocks for each read and write, so no request holds a
connection idle while a model thinks. X-Ray, Insights and the L10 recap email
follow this; a new long path does too.

### Tests never touch a deployed database

`db.get_engine()` routes on `CLOUDSQL_CONNECTION_NAME` before it looks at
`DATABASE_URL`, so a test that only points `DATABASE_URL` at a throwaway SQLite
file is not isolated when a developer `.env` is present. The root
`conftest.py` blanks the variable for the whole session, and its autouse fixture
fails any test whose engine is not SQLite. Fixtures that were once written
to a shared database by a local test run reached real users, so keep both
guards.

### Migration drift is guarded: fail open on drift, closed on writing to a deployed database

A database stamped with a revision the image does not know used to kill every
new container at boot (`alembic upgrade head` could not resolve the revision,
retried, exited 1, never listened on its port), and routing traffic forces cold
starts, so rollback failed too. Three guards, sharing
`<pkg>/database/migration_guard.py` (pure logic; I/O in
`<pkg>/database/migration_preflight.py`, tests in `test_migration_guard.py`):

1. **Boot preflight.** `scripts/start.sh` runs the preflight in `--mode boot`
   before `alembic upgrade head` and branches on its exit code: `3` = drift →
   log a loud diagnostic (database revision vs image head) and start without
   migrating; `4` = drift with `MIGRATION_DRIFT_POLICY=fail` → refuse to start;
   anything else, including a crash, means migrate as usual, so the guard can
   never become a new boot failure. `GET /health` reports
   `migrations.skipped / .drift / .db_revision / .image_head / .stage`, read from
   env markers `start.sh` exports, never a live database read. After a
   successful upgrade `start.sh` runs `--mode postmigrate` and rewrites the
   markers with `stage=postmigrate`: **`stage: postmigrate` means `db_revision`
   is the truth being served**; `stage: preflight` with a mismatched head on a
   migration deploy means the refresh never ran, not that the migration
   failed.
2. **`alembic upgrade|downgrade|stamp` refuses to touch a deployed database from
   a laptop.** `alembic/env.py` calls `assert_migration_target_allowed`, which
   blocks when `CLOUDSQL_CONNECTION_NAME` is set (or `DATABASE_URL` names a
   non-local host) unless it runs inside Cloud Run or `ALEMBIC_ALLOW_REMOTE=1`
   is set deliberately. Read-only commands (`current`, `history`, `heads`,
   `check`, `revision`) are untouched.
3. **Pre-deploy check.** The deploy scripts source
   `scripts/lib/migration_preflight.sh` and call `run_migration_preflight` before
   building. It blocks on drift (`MIGRATION_PREFLIGHT_ALLOW_DRIFT=1` overrides,
   for the deploy that is the fix) and warns when the working tree carries
   migration files that are not on the main branch: a `--source .` deploy
   uploads uncommitted files. `MIGRATION_PREFLIGHT_STRICT=1` makes that a hard
   stop. CI keeps only the single-head check, because it has no database
   access.

### One baseline migration; defaults come from `seed_workspace`

The schema starts at `alembic/versions/0001_cubie_baseline.py`, generated from
the models; later migrations are `0002_...` onward, one head. The baseline
creates no rows: a workspace's defaults (default AI Priorities playbook, Drive
label weights, SLA placeholders, Smart Teams skills taxonomy) come from the
idempotent `rubik_core.database.seed.seed_workspace`, which fills only empty
slots and never overwrites an admin's edit. A new default row belongs there,
with a fixed primary key (a `uuid5` of a fixed name, never `uuid4`) written by
its insert-or-nothing helper, so two instances booting at once write one row.
`backend/tests/test_migrations_baseline.py` holds the migration to the models
on SQLite; `scripts/dev/schema_diff.sh` diffs two versions of the chain on
Postgres. See `docs/migrations_guide.md`.

### A migration that touches an existing table sets `lock_timeout`

A boot-time `ALTER TABLE` on a table nearly every request reads once waited for
a lock behind live transactions; every reader queued behind it and the service
returned 504 and 429 to everything until the stuck instances were replaced. A
migration that alters an existing table must `SET LOCAL lock_timeout`
(`backend/tests/test_migration_lock_safety.py` enforces it). Prefer a new table
over new columns on a hot table: the per-person voice shortcut lives in its own
table for this reason.

### `eos_organizations` is a single-row anchor: never surface it

The EOS schema inherits an `Organization` root (`eos_organizations`, with FK
columns such as `eos_vto_versions.organization_id` and
`eos_quarters.organization_id`). Treat it as invisible scaffolding: the
`/api/eos/organizations` endpoints must not appear in any page (no org selector,
list or switcher). A handler that needs the org id takes the only row. One row
per workspace database is the shape multi-workspace keeps.

---

## 3. EOS invariants

- **People and Seats are read-only.** People are managed in the main `users`
  table. `eos_people` stays only as the identity backbone (rock, to-do, issue
  and scorecard owners, `eos_l10_meetings.manager_id`,
  `eos_l10_attendance.person_id`, GWC point at it). `GET /api/eos/people/`
  lazily syncs a row per user by email so owner pickers stay populated. Do not
  re-add People/Seats CRUD or a People page.
- **Every EOS row is timestamped and attributed.** `AuditMixin` carries
  `created_at`, `updated_at`, `created_by` and `updated_by`. A new EOS write path
  sets `created_by` on insert and `updated_by = current_user.email` on update.
- **The L10 meeting is the single workspace.** Everything discussed is
  actionable inside `L10Session.tsx`: Rock weekly updates, scorecard values
  (`POST /scorecard/entries/upsert`, idempotent on metric and calendar day),
  to-dos, issues, headlines. The Conclude step is an individual survey: each
  attendee submits their own 1–10 rating and comment, and
  `L10Session.average_rating` is derived from those rows in `end_session`. Do
  not reintroduce a single manager rating.
- **Check-in is two entries per person**, Personal and Professional, in separate
  column pairs on `eos_l10_attendance`. Do not reintroduce a single slot: the
  two overwrote each other. The attendance upsert is keyed on
  `(session_id, person_id)` and writes only the fields the caller sent, so the
  two check-ins and the Conclude survey never clobber each other.
- **The L10 is a shared live session; the timer is an anchor, not a tick.**
  `eos_l10_sessions` carries `current_step`, `step_started_at`, `is_paused` and
  `state_version`. The facilitator drives the cursor (`POST
  /sessions/{id}/state`, manager-gated); everyone else follows.
  - Every client computes `remaining = duration − (serverNow − step_started_at)`
    with `serverNow = Date.now() + serverOffset`, re-derived on each poll from
    the `server_now` that `/state` returns. Never a ticking server clock or a
    local reset.
  - State is read through the existing 2 s poll (a short stateless GET), never
    SSE or a websocket: a held stream per attendee re-creates the
    connection-exhaustion outage. `state` and `signals` share one tick.
  - Live edits surface through change markers (`attendance_rev`, `items_rev` =
    `max(updated_at)`); a client refetches only the list whose marker moved.
    Optimistic handlers set `suppressItemsRefetchUntilRef` (~2.5 s) so a poll
    cannot clobber an in-flight edit.
  - `actual_end` is the "meeting over" beacon: the client stops polling and
    freezes the clock, and `POST /state` answers 409 after the end.
  - `start_session` sets the cursor on create and must not re-anchor on rejoin.
- **L10 calendar import expands Google Group invitees.** The Calendar API
  returns a group as one attendee with the group's address, so group-only
  members were unpickable in check-in. `<pkg>/eos/calendar_groups.
  expand_group_invitees` asks the Directory API (delegated to `ADMIN_EMAIL`) for
  the members of every attendee address that is not a known user, nested to
  depth 2, suspended members dropped. Best effort: any error leaves that address
  as the calendar gave it. Kill switch `EOS_L10_EXPAND_GROUP_INVITEES`. The
  roster stays "who was invited", refreshed only on Import or Sync; exec
  members are not unioned in.
- **The L10 recap email trusts the transcript for attendance.**
  `meeting_participants` is the read ACL for a recording and a superset of who
  was in the room (it can include the invite list, no-shows and all).
  `<pkg>/eos/l10_recording.present_participants` re-derives presence from the
  transcript body: their email appears, their full name appears as whole words,
  or they own the recording. A silent attendee Gemini never named is missed on
  purpose: a missing recap is a nuisance, a recap sent to someone who was not
  there is an incident.
- **An L10 has no join key to its recording.** `find_recording` matches on title
  score ≥ 0.6 against the meeting name (after stripping the "Notes by Gemini"
  suffix), ingest time inside `[session start − 2 h, session end + 36 h]`, and at
  least one shared attendee when the meeting has a roster. A meeting renamed in
  the app but not in Calendar silently stops matching (`not_recorded`).
- **EOS re-ingest is debounced and is the only freshness path.** The edit hooks
  (`backend/jobs/eos_ingest_hooks.py`: issue create/solve, VTO save, process
  create, L10 end) dispatch to the worker job. Never re-add them as FastAPI
  BackgroundTasks, and never launch a job per edit: each run re-ingests the
  whole kind, so edits coalesce through one `eos_ingest_state` row per kind
  (watermark plus claim CAS; state machine in
  `<pkg>/ingestion/eos/debounce.py`). There is no weekly full-sweep cron, so if
  the hooks break, EOS edits stop reaching RAG. A claim stuck in
  `launching`/`running` blocks only until it goes stale. Tunables:
  `EOS_REINGEST_DEBOUNCE_SECONDS` (120), `EOS_REINGEST_DEBOUNCE_MAX_WAIT_SECONDS`
  (600), `EOS_REINGEST_CLAIM_STALE_SECONDS` (1800).

---

## 4. Transcripts and calls

### An unfinished "Notes by Gemini" doc exports as a BOM, and a BOM is truthy

Google creates the notes doc as an empty shell when a meeting ends and fills it
over the next minutes; the transcript sync runs every 15 minutes, so it lands in
that gap regularly. An empty Google Doc exports as a bare UTF-8 BOM, `"﻿"`
is truthy, and `str.strip()` does not remove it, so the shell was ingested,
marked `approved`, and never revisited. **Strip the BOM wherever transcript
text is read** (`read_transcript_text` does) and **never treat non-empty as
"has content"**: check length against `MIN_TRANSCRIPT_CHARS`. To repair a
poisoned asset, move its status off `approved` so the sync picks it up again.
Never set `deleted`: that is a tombstone meaning a person revoked access.

### Fathom recordings become captured calls through the Fathom sync

`<pkg>/call_capture/fathom_attach.py` runs inside
`process_fathom_transcripts_background` (`backend/routers/fathom.py`), per
meeting, **before** that function's "already ingested, skipping" `continue`;
that ordering is the backfill. The webhook and `POST /fathom/ingest` both run it
on the worker job.

- `fathom_recording_id` is a real UNIQUE column on `call_sessions`, never a JSON
  key: it is the found-or-created key for a replay, and nothing may query
  inside JSON (CI runs on SQLite).
- An existing session wins (same host, start within 45 minutes). A matched
  session that already holds entries from any source is linked, never given a
  second transcript.
- The host check uses `own_domains()` with no argument; `own_domains(host)`
  unions in the host's domain and makes every recorder look internal.
- `CCS_FATHOM_ENABLED` defaults on and is not chained to `CCS_ENABLED`: the sync
  runs on the worker job, and a flag unset there would skip silently with exit 0.
- Company binding reuses `domain_match.resolve_domain_matches`: exactly one
  candidate company, or NULL and the wrap-up asks.

---

## 5. AI Priorities

### The feedback loop feeds prompts: keep it safe

The assistant-feedback table stores a person's rating (`useful`, `meh`,
`not_useful`) and optional note on one AI Priorities card, chat message or
meeting-prep brief, upsert-keyed on `(user_email, surface, proposal_id)` with a
`proposal_text` snapshot. Store and prompt builder: `<pkg>/feedback/{store,wgll}.py`;
API `backend/routers/feedback.py`; Admin → Feedback.

`build_wgll_context()` injects recent `useful` and `not_useful` exemplars into
the priorities decision-maker prompt, the chat coordinator instruction and the
meeting-prep prompt. Two guardrails must stay: it returns `""` on any error or
when no ratings exist (an empty block changes nothing), and it is gated by
`…_WGLL_FEEDBACK_ENABLED`. Keep the instruction to match style and substance,
not to copy entities or dates. Chat feedback is tied to the persisted
`ChatMessage.id` (`ChatResponse.message_id`); do not revert to client-side ids.

### An ask the reader already answered is dropped in the agent, not argued away in the prompt

- **Slack:** `SlackChannelAgent._drop_answered` removes a message before it
  becomes a signal when the reader replied in its thread, or (DMs only) posted
  later on the DM's timeline. Channels are not timeline-checked (unrelated
  traffic). The thread fetch is skipped when history reports `reply_count == 0`
  (`None` means unknown and does fetch). A failed fetch keeps the message:
  dropping a real ask is worse than repeating a handled one.
- **Email:** `direction=i_sent_last` is ambiguous ("I asked and they went
  quiet" vs "they asked, I answered"). `GmailChannelAgent` stamps
  `reader_last_message_kind` (`answer`, `ask`, or `fyi` from the extractor),
  `reader_replied` and `messages_since_reader_reply`; the decision maker reads
  them, and `orchestrator._drop_answered_threads` drops a `follow_up_debt` or
  `approaching_deadline` card whose every cited signal is an answered thread.
  `commitment_debt` is left alone: a promise inside an answer is still owed.

### A new signal correlates with another only through identical `entity_refs`

The correlator groups signals purely by shared `entity_refs` keys (`deal_id`,
`contact_email`, `company_name`, `event_id`, …). To attach a new source's signal
to another source's card, build its `entity_refs` from the same extraction the
other source uses (import its helpers rather than re-deriving), and pin the two
byte-for-byte equal in a test. Re-implementing drifts silently and splits the
card in two.

### A source's lookback may differ from the pipeline's `since`: anchor on `now`

The multi-source pipeline calls every source with `since = now − 24h`, an
incremental-pull hint, not a contract (the calendar source uses its own forward
window). When a source's natural window is not 24 h, anchor on `now` and say
why; do not let the 24 h `since` truncate its data.

---

## 6. Background work, jobs and scheduling

- **Multi-call work never runs as a FastAPI BackgroundTask.** Cloud Run
  throttles the Service's CPU after the response, so a pipeline fired that way
  starves. Use a Cloud Run Job (the worker job, `deep-prep`, `xray-deep`), with
  an in-process fallback only where noted.
- **A kill switch a job reads must be set on the job too.** Jobs inherit nothing
  from the Service; a job-side switch left unset exits 0 looking healthy.
  Scheduler endpoints for a switched-off feature answer 200
  `{"status": "disabled"}`, never 404 (a 404 reads as a broken job in
  Scheduler's alerting).
- **Deploy order:** guarded preflight, one image build, control and workspace
  migration Job, Service, declared Jobs, declared schedules, `/health` check.
  `scripts/platform/deploy.sh --env <file> --dry-run` lists that order without
  cloud writes. A release or rollback obeys every workspace's freeze window
  and the 20-minute lead. Missing control tables or an unknown migration
  revision stop the release before any service change.
- **Job argv uses `--flag=value`** for every value: an id starting with `-`
  would be read as a flag. `containerOverrides.args` replace the job's args, so
  re-supply `-m <module>`.
- **`@attribute_to(...)` goes on the function that does the work**, not the
  entrypoint: the cost contextvar does not survive a thread hand-off.
- **Schedules and the business day run on the workspace's control-row IANA
  time zone**, with an explicit user preference taking precedence where the
  feature supports one. A job's cadence names it ("weekdays 08:00 in
  the business time zone", `BUSINESS_CLOCK` in `backend/jobs/__init__.py`);
  never write a company's own zone into code, a cadence or a doc.

### Outbound system email: pass your own `autosend`

`<pkg>/tools/email_sender.send_email` defaults to Deep Prep's autosend switch.
A new email feature passes its own `autosend=` (see
`backend/services/l10_summary_email.py`) and resolves recipients under its own
report ACL. The sender and company audience come from Workspace operations;
production recipient redirects are not a rollout control. Use a dry-run to
preview the exact audience. Check the autosend flag before generating the body,
so a dark rollout does not re-spend tokens.

### Scheduled tasks: the schedule is data, the tick is one cron, the run is a headless chat turn

1. **One Cloud Scheduler entry drives every schedule.** Per-person cadence lives
   in `scheduled_tasks.next_run_at`, never in GCP. The entry hits
   `POST /api/agents/scheduled-tasks/tick` every ~5 minutes (the worst-case
   lateness).
2. **`next_run_at` moves forward at claim time**, by a compare-and-swap on the
   value the tick read (`store.claim_task_for_run`). An occurrence can be missed
   (recorded as failed) but never run twice. A launch failure releases the
   claim. `dispatch.reap_stale_runs` fails a run after
   `SCHEDULED_TASKS_RUN_TIMEOUT_SECONDS + 900 s`; keep that grace well above the
   timeout plus a cold start. The worker CASes its run row `queued → running`
   and exits 0 if it loses, and `scheduled_task_run_job.py` always exits 0 after
   the claim, because a retry of a delivered run would send the digest twice.
3. **A scheduled run is the same chat agent, built headless.**
   `backend/services/chat_orchestrator.build_chat_orchestrator` serves both
   paths. `headless=HeadlessRun(title)` makes `build_coordinator` omit every
   `require_confirmation=True` tool, the sub-agents, and the scheduling tools,
   and adds a "nobody is present" block. The reduction happens at construction,
   never by filtering `agent.tools` afterwards (ADK keeps the confirmation flag
   on a private attribute).

- **`ADKOrchestrator.run` never raises**: it returns an apology as the answer.
  `runner._answer_failed` inspects the trace (an `ADK_EXECUTION` error step,
  `empty_agent_result`, `AGENT_LOOP_LIMIT`). Anything on the run path must keep
  that check.
- Each run gets a fresh `ChatSession` (the ADK session id is that id) in a
  per-person "Scheduled tasks" folder the dispatcher get-or-creates
  sequentially (`chat_folders` has no unique name constraint).
  `ChatSession.user_email` is written verbatim: chat lookups are case-exact.
  Delivery is a Slack DM by default, email on request with its own autosend
  settings, plus a `Notification` row.
- Slack from the worker job works: `SlackBotClient` reads the bot token from the
  database (`SlackCredential.bot_token`), not from env.
- Two rules that apply to all of chat: date the "today" note in the user's time
  zone (`_today_in(self.timezone)`), and record AI cost against
  `orchestrator.model_id`, the model that actually ran. ADK tokens are recorded
  explicitly with `record_usage`, because the coordinator runs outside the
  shared client the cost wrapper sees.
- `docs/uc30_scheduled_jobs_framework.md` is the older admin-owned spec this
  deliberately diverges from for the per-person case.

---

## 7. Features

### Meeting Prep: a version chain, section edits, newer sources

Code: `<pkg>/meeting_prep/` (`sections.py`, `versions.py`, `rewrite.py`,
`signals.py`, `background.py`), endpoints in `backend/routers/home.py`, page
`frontend/src/pages/MeetingPrep.tsx` and `frontend/src/pages/meetingPrep/`.

- **The chain lives on `meeting_prep_runs`** (`version`, `base_run_id`,
  `change_kind`, `change_note`, `changed_section`, `section_meta`, unique on
  `(user_email, event_id, version)`). `versions.append_version` takes max+1 and
  retries once on `IntegrityError`. **`version IS NULL` means never a version a
  person lands on**: Deep Prep, failed generations and pending builds. Every
  chain read goes through `versions._chain_query`.
- **Version fields are never trusted from the stored `response` JSON**:
  stripped on write, refilled from the row's columns on read.
- **Opening the page never regenerates.** GET returns the latest version and
  builds v1 only when there is none.
- **New routes live under `/api/homefeed/prep-runs/…`** because
  `/meeting-prep/{event_id:path}` is a catch-all. Writes are owner-only and only
  on the latest version (409 with the latest version otherwise). Regenerate with
  `keep_edits` carries hand-edited sections forward; sections the model
  rewrote are not carried. Impersonation already 403s every
  `/api/homefeed/*`; do not add these routes to the impersonation allowlist.
- **`sections.SECTIONS` is the one list of what is editable**; hand edits and
  rewrites both go through `normalize_section_value`.
- **One-section rewrite** (`rewrite.rewrite_section`) uses the same grounding
  rules word for word as the full brief (a drift test pins them), reads the
  evidence from the stored `context_bundle` of the run it rewrites, and treats
  the instruction as a style request, never a licence to invent.
- **Newer sources are conditional, each with its own switch** (`…_PREP_CRM_ENABLED`,
  `…_PREP_XRAY_ENABLED`, `…_PREP_PROMISES_ENABLED`,
  `…_PREP_LAST_MEETING_NOTES_ENABLED`, `…_PREP_AVAILABILITY_ENABLED`,
  `…_PREP_FLAGGED_ENABLED`). Each is fail-soft and adds a block only when it
  found something; no block means no clause, so a switch turned off restores
  that source's prompt byte for byte. Reader-gated sources (X-Ray, to-dos)
  follow the reader's Feature Access. `collect_meeting_prep_context`,
  `generate_meeting_prep_analysis` and `_persist_meeting_prep_run` share the
  `signals.py` contract: change a shape, change all three.

**Background builds and notifications.** Get Ready and Regenerate start a
background build and return 202; the one-section rewrite stays synchronous.

- The pending row is the future version: `change_kind="pending"`,
  `delivery_status="queued"`, the request stored in `context_bundle`. The worker
  finalizes the same row, and the version number is assigned at completion, so
  a failed build never reserves one.
- Dedupe: a build already queued or running for the same reader and meeting is
  returned instead of starting another.
- Two clocks: `…_PREP_QUEUED_TTL_MINUTES` (a row never started) and
  `…_PREP_BACKGROUND_TTL_MINUTES` (a run that took too long). Each is applied by a
  conditional update wherever a row is read, including
  `background.expire_stale_for_user` from the notifications poll, so each
  failure notification goes out exactly once.
- Runner `…_PREP_RUNNER = auto|job|inprocess`. Job mode launches the existing
  `deep-prep` job with `-m backend.jobs.meeting_prep_job`. In-process means a
  task on the app's running event loop (`loop.create_task`), never
  `asyncio.run` on a pool thread: the shared genai client binds to the first
  loop that used it, and the tests fake the model, so they will not show it.
- The launch forwards the Service's current source switches as env overrides.
  Any new secret or non-switch setting a meeting-prep source reads must also be
  set on the `deep-prep` job, or background builds lose it silently ("no token"
  looks exactly like "no deals").
- Notifications `meeting_prep_ready` and `meeting_prep_failed` link to
  `/meeting-prep/<urllib.parse.quote(event_id, safe="")>`; event ids are
  free-form, so never build the link unquoted. They are fed by the one app-wide
  notifications poll, never a second timer.

### Attendee intelligence never scrapes linkedin.com

Meeting prep researches each external attendee's public professional presence
(`<pkg>/people/`, `frontend/src/components/AttendeeIntel.tsx`,
`docs/uc47_attendee_intelligence.md`). This is the part most likely to be
"helpfully" reverted:

- **Fetching linkedin.com would get the service's single egress IP blocked**:
  the feature dead for every user at once. It also breaks the site's terms and
  every markup change. Evidence comes from Gemini with Google Search grounding,
  or from a licensed API (`LINKEDIN_DATA_API_KEY`, inert by default).
  `test_nothing_in_the_people_package_scrapes_linkedin_directly` pins it.
- **Observation vs inference is the safety design.** `profile` and `activity` are
  cited facts (activity without a URL is dropped in code); `persona` is the only
  inference and carries a `confidence`. No activity forces `confidence="low"`.
- `MeetingPrepResponse.attendee_intel` is copied from the research step
  verbatim, never re-read from the brief model's JSON, so citations stay
  attached to their claims.
- **Colleagues are never researched**: external domains only, shared mailboxes
  and calendar resources dropped, at most six people, the rest reported as
  gaps. An all-internal meeting makes no research calls.
- Every prompt instruction that points at the block is conditional on the block
  existing (`has_intel`). Turning the feature off must never leave the brief
  worse than before it existed.
- The cache (`attendee_profiles`) is shared across users, keyed on lowercased
  email, 14 days (failures 6 hours). `llm_grounded` and `safe_json` live in
  `<pkg>/models.py`; do not add another copy.

### What's On Your Plate: a stale dated commitment, gated in the orchestrator

Registry key `open_commitments`, kill switch `OPEN_COMMITMENTS_ENABLED`. The
morning briefing answers "what is waiting on me?": replies owed and awaited,
promises with a date, and commitments whose date has stopped being believable.

- **The one shape.** A *stale dated commitment* is an obligation with an
  owner, a date somebody else relies on, and a last-activity time for the
  other side. The card fires when the silence has grown large against the time
  left; it says the date is no longer credible, not that it is late. A deal's
  close date against `notes_last_contacted` is the first instance; nothing in
  the shape is sales-specific, only which facts already exist as columns.
- **`close_date_tier` (`backend/signals/hubspot.py`) is the one definition**
  of the close-date bands. The AE's card (tiers 1 and 2) and the weekly
  manager roll-up (all three) both read it, so a manager never asks about a
  deal whose card the rep never saw. Tier 3 is never a `Signal`.
- **`CLOSE_DATE_AT_RISK` is its own kind**, outside the forward kinds: a card
  whose only anchor is a date already past would otherwise be dropped. It is in
  `SNOOZE_ONLY_PRIORITY_KINDS` (the check mark and the x both write a bounded
  snooze), because a durable resolve on a recurring kind can never be revived.
  The decision-maker does not list an elapsed snooze as "already handled".
- **The gate lives in the orchestrator**, where the in-app regeneration and
  the nightly batch meet, and it passes the ORM `User` row to `feature_allows`,
  never the email string: a string has no department or flags and yields a
  silent `False` for everyone. A denied reader gets silence, never an
  error-shaped digest (an all-errored run is treated as poisoned).
- **The roll-up (`close_date_rollup`) sends exactly once per recipient per
  local day**, guarded by `digest_logs`' unique `(recipient_email, kind,
  local_date)`, not by a pre-check that two racing scheduler runs would both
  pass.
- **Noise budget.** The bundle is five cards; keep any one channel from owning
  the morning, and collapse a long tail into one line rather than more cards.

### Proposal Generator: the brief is the fence, and pricing never passes through prose

`<pkg>/proposals/`, router `backend/routers/proposals.py`, tables `proposals` and
`proposal_versions`, kill switch `PROPOSALS_ENABLED`.

- **Two documents, not one.** The brief (facts: who, service, scope items,
  outcomes with verbatim quotes, pricing lines, notes, open questions) is what
  the AE confirms; the prose is generated from the confirmed brief and keyed to
  its item ids. `brief.normalize_content` discards any id not in the brief, so
  the model has nowhere to put an invented service. Do not merge them.
- **The pricing table is rendered from the version's brief snapshot**, never
  from the prose (`render_pdf`, `render_docx`). Each version snapshots its
  brief, so an old version still prints its own number.
- **Outcome quotes are verified against the transcript.** On extraction, an
  outcome whose quote is not verbatim in the call is dropped; on an AE save or
  revision it is kept with the quote blanked.
- The three model calls run in-request in the threadpool; if a transcript ever
  pushes past the request ceiling, the fix is a worker job and polling, not a
  bigger timeout. Only the AE on the call may start or edit its proposal. The
  app never sends the document.

### Voice input: the browser sends WAV, and the server listens before Gemini does

The mic in the chat box (`frontend/src/components/voice/`,
`backend/routers/voice.py`, `<pkg>/voice/`, kill switch `VOICE_INPUT_ENABLED`).

- **Gemini invents speech from silence and cannot be told not to** (temperature 0
  and a "is anyone speaking" field both failed; no logprobs to threshold). So
  speech is detected from the samples, twice, with one algorithm:
  `voiceActivity.ts` and `<pkg>/voice/signal.py` (50 ms frames, RMS, room level =
  10th-percentile frame, voiced at `max(0.015, 3 × room)`, 250 ms voiced
  needed). Change the constants in both files together.
- **The upload is WAV** because the server image has no audio decoder; 16-bit
  PCM needs only `wave` and numpy. The browser captures raw samples with an
  AudioWorklet (a CSP that ever lands needs `blob:` in `script-src`),
  downsamples to 16 kHz mono and encodes WAV. The router refuses anything else.
- Gemini gets a WAV re-encoded from the measured samples (`signal.to_wav`), never
  the raw upload, so a lying header cannot smuggle unchecked audio past the
  checks.
- The recorder gives the audio engine 3 s to start (iOS can leave `resume()`
  pending forever) and checks after every await that it still owns the attempt.
- The words-per-second backstop catches only long inventions, and uses the
  WAV's true length. The AI Cost row prices audio tokens as text, so treat it as
  a floor.
- Thinking is set to "low" and the model is FLASH (LITE miscapitalised proper
  nouns). The prompt carries every `users.name` so names come back spelled
  right; the transcript is never logged.
- Only a stop the person makes sends (`StopReason` 'user', never 'limit'). While
  the mic is busy it is the send button. Enter and Esc are caught on window in
  the capture phase, only when focus is in the chat box, the mic or body, never
  with a modifier or during IME composition.
- Reusing it: `<VoiceInputButton surface=… onTranscript=…>` plus
  `appendToEditable` or `appendToField`; add the surface to `VoiceSurface`
  (`frontend/src/api/voice.ts`) and `SURFACE_LABELS`
  (`<pkg>/voice/transcribe.py`). See `docs/voice_input/README.md`.

### Voice commands: one forced-choice Gemini call, and the browser makes the change

`frontend/src/components/voice/VoiceCommandBar.tsx`,
`backend/routers/voice_commands.py`, `<pkg>/voice/commands.py`,
`backend/services/voice_actions.py`, kill switch `VOICE_COMMANDS_ENABLED`.

- **One call, forced to choose**: a fixed list of functions with `mode=ANY`, so
  the answer is always something the app can do, never prose. Every function
  carries `heard`.
- **The model only picks; it never names a record.** It returns words;
  `voice_actions.propose` resolves them against what the person can reach
  (owner lists, visible Rocks, mutable ids, team scopes). Several matches → a
  "which one?"; none → a sentence, never a guess.
- **Nothing is written server-side.** A proposal carries the exact request the
  page's own button sends; the browser sends it only after Enter, a click or a
  spoken "yes". Permissions, side effects and audit stamps are the click's own.
  Never add a server-side executor that calls router functions directly:
  router dependencies would be skipped.
- Pages come from the sidebar (`voiceDestinations`); defaults are the forms'
  defaults; the speaker is resolved after the owner list syncs `eos_people`.
- The shortcut lives in its own table (see the lock_timeout note above). A
  modifier-only chord counts only when pressed and released with no other key
  between; listening starts 250 ms into a hold. Only one recorder may hold the
  microphone (`micOwner`). The browser-automation `key` action does not send
  modifier-only chords faithfully: test with dispatched `KeyboardEvent`s.
- **Never test against the full local app unstubbed.** Signing in fires
  `/api/ingest/meet-transcripts`, which reads real people's Meet notes from Drive
  into the local Chroma store. Stub `/api/ingest/*`.
- The panel is portalled to `<body>` so it works during a live L10. After a
  change the bar fires a `voice-changed` window event with the page path, and
  Rocks, To-Dos, Issues and Scorecard refetch.
- Opening one record by name (`backend/services/voice_records.py`) offers a kind
  to the model only when that page's own gate passes; an L10 always asks first
  (opening it starts or rejoins the live session). A question goes to Chat as
  navigation state, not `?q=`, so it stays out of browser history.
- The model call gives up after `VOICE_COMMAND_TIMEOUT_SECONDS`, inside the
  browser's limit. Things that leave the company (email, CRM writes) stay out of
  voice.
- Adding an action: a function in `commands`, a planner in
  `voice_actions.PLANNERS` (a test pins the two lists match), its path in the
  allowed plan paths, a card test that sends the plan to the real endpoint, and
  a line in Help (both homes).

### Loose Ends: one morning scan, closure is detected, mailbox content stays with its owner

`docs/loose_ends/README.md` is the contract and runbook. One central scan each
weekday morning in the business time zone (UTC until set), on the worker job
(`backend.jobs.loop_closure_scan_job`, package `<pkg>/loop_closure/`), reads
each rostered person's Gmail (metadata only) and Slack, reconciles against
HubSpot, and keeps `loop_items`.

- Registry key `loop_closure` decides who is scanned; being on it means the
  morning read of that person's mailbox. It ships open to everyone (spec §4.3.4),
  so with `LOOP_CLOSURE_ENABLED` on every person is scanned until an admin
  narrows it; once narrowed, admins and execs are never swept in by their flags.
- `LOOP_CLOSURE_ENABLED` must be on for both the Service and the worker job.
- The daily Slack post ships off; an admin turns it on and picks the channel in
  Admin → Loose Ends (stored in `workspace_settings`, fails closed). Saving
  refuses "on" unless the bot is in the channel.
- **Privacy rule.** Subjects, snippets and AI headlines are shown only to the
  person they belong to. Team views, the digest, the scorecard and team chat
  answers carry owner, kind, CRM names and age only. `detail` and
  `close_evidence` hold ids and timestamps, never message text. Logs carry error
  type and status only.
- **Closure is detected, never ticked.** The only manual exit is "Not a real
  miss", and a dismissed key is never re-flagged. A source that could not be read
  decides nothing. Open past 60 days → `aged_out`.
- To-dos list at `LOOP_CLOSURE_TODO_GRACE_DAYS` overdue. Changing a grace is safe
  only because `rescheduled` requires the date itself to have moved later.
- **Writes only from a click.** "Log it in HubSpot" logs the person's actual sent
  email (re-read server-side from their mailbox; the browser never supplies
  content) and never writes notes or AI text. "Draft a reply" makes a Gmail
  draft and never sends.

### Files in chat: private to the person who attaches them, by construction

`<pkg>/chat_files/`, `backend/routers/chat_files.py`,
`frontend/src/components/chatFiles/`, kill switch `CHAT_FILE_UPLOAD_ENABLED`,
table `chat_uploads`.

- **Privacy is structural, not a flag.** The files are not `assets` rows, so the
  Library, its admin view, department fallbacks, sharing and offboarding
  hand-off never see them. Every read filters `owner_email == caller`; there is
  no admin path, and impersonation gets a 403. Do not move them into `assets`.
- **One Vertex corpus, fenced per search.** The private corpus is deliberately
  not a registry slug. Retrieval names the caller's own files
  (`RagResource(rag_corpus, rag_file_ids=[…])`); `private_index.search` returns
  `[]` on an empty fence, because no ids means "search everything". Hits are
  re-checked in SQL. `MAX_FENCED_FILES` (100) newest per search.
- Files go in with `rag.upload_file` (synchronous, returns the ragFile id). When
  filing is unavailable the file is still used in the chats it is attached to
  (`chat_only`).
- **Copies of a private answer are private too.** A turn that used attachments
  or `search_my_files` is flagged `private_files`; admin-readable copies
  (interaction logs, feedback snapshots) get a placeholder. A new admin-readable
  copy of answer text must check the same flag.
- **Answers never fetch outside images**: chat markdown renders images only for
  same-origin paths, and file text is fenced as data with its markers defanged.
- Delete removes the ragFile, the object and the row, in that order. Offboarding
  purges private files; they are never handed to a successor.
- Limits guard against decompression and grid bombs (spreadsheet dimensions
  reset, zip size and ratio checked before any library opens it, page and size
  caps on scans). NULs are stripped before Postgres sees them.
- **Publishing to Assets** copies a private file into the shared Library through
  the Assets upload itself (`dam.upload_document`), so dedup, corpus routing and
  ingest are exactly an Assets upload's. The private file stays private;
  deleting it keeps the copy.

### What's New: the reader of the changelog

`docs/whats_new/README.md` is the contract. The release notes come from one
seam, `<pkg>/whats_new/changelog.py` (`ChangelogEntry`, `ChangelogSource`,
`get_changelog_source()`); the drawer, button, spotlight bar and morning post
only ever see entries. Who may see an entry (`feature_key`), when (no future
entries) and whether its link stays in the app are checked by the reader on
every read, so a source cannot widen them.

- **Opens are counted as people, never who.** An open is stored as an HMAC of
  `"<entry id>:<email>"` under a per-workspace key that is generated once and
  never rotated (rotating would double-count everyone). That is pseudonymous,
  not anonymous. No endpoint may return viewer keys or open rows.
- **The morning post** (`digest.post_digest`) is one message per weekday with
  the notes published since the last post (at most 12, under Slack's text
  limit). The first run only sets the mark. An advisory lock means two runs
  never post the same notes. `chat.postMessage` is not idempotent, so it is never
  retried after a timeout: a note is lost rather than posted twice.
- **The tease never fires in a live L10** (`shouldTease` checks
  `isLiveL10Path`), on deep links, under impersonation, on narrow screens or
  more than once a week.
- Per-person "seen" state lives in the browser (`memory.ts`) on purpose.

### Meeting Skills: every point quotes the transcript

Chat is the only surface. A Template is an outline (`meeting_templates`), a
Skill is the whole job (`meeting_skills`, run with `/slug`), a Routine runs a
skill on its own. Code `<pkg>/meeting_skills/`, tools
`<pkg>/tools/meeting_skills_tools.py`, kill switch `MEETING_SKILLS_ENABLED`
(Service and worker job).

- **One run contract** (`executor.run`): load (re-checking
  `meeting_participants`) → one structured Flash call returning items with
  `{m, u, quote}` evidence → `verify.py` (no model) drops items whose quote is
  not in line u±1, clears owners nobody named, removes due dates not in the
  quote, flags figures not in the quote → `render.py` lays out → an optional
  Gmail draft → one `meeting_skill_runs` row. The model never writes the final
  document, so a fluent unsupported sentence cannot reach the person.
- Transcripts are the stored copy of the Drive Doc; when missing, the Doc is
  exported as the person. `normalize.py` parses Gemini notes (the notes tab is
  outline, never evidence), Meet transcripts, Call Capture and Fathom.
- Routines on a clock are scheduled tasks; event routines are queued by
  `on_transcript_ready(doc_id)` into `meeting_routine_fires` (UNIQUE routine +
  doc is the dedupe), dispatched by the scheduled-tasks tick. `active_since`
  stops a new routine from backfilling old meetings.
- A live chat run caps extraction at `MEETING_SKILLS_CHAT_EXTRACT_TIMEOUT_SECONDS`
  so the answer still fits inside chat's time budget. The transcript is fenced
  as untrusted data.

---

## 8. Credentials and logging

### A credential sent as an HTTP header is trimmed at load, and its errors are masked

A token stored with a trailing newline made httpx refuse it as a header and
quote it whole in the exception, which an `exc_info=True` log wrote out.

- **Every Settings credential that becomes a header value is listed in
  `_TRIMMED_SECRETS` (`<pkg>/config.py`)**, which drives both trims: the
  `_trim_header_secrets` validator and the Secret Manager backfill. A new header
  token joins the set in the same change; `test_header_secrets.py` pins it.
- Error text from an authenticated call goes through `<pkg>.logsafe.safe_error`
  before it is logged or raised, and a re-raised transport error uses
  `from None`: with `from e` the chained error prints unmasked.
- Store a secret with `printf '%s' "$T" | gcloud secrets versions add … --data-file=-`,
  never `echo`.

---

## 9. Deployment gotchas

- **Keep the SPA catch-all.** `backend/main.py` falls back to `index.html` for
  client routes; without it, `/login` and every deep link 404 on refresh.
- **`gcloud scheduler jobs update http --update-headers` does not merge.** Pass
  every header the job needs (the scheduler key included) in the one flag, then
  check the header names afterwards (`describe --format=json`, keys only; the
  update's own output prints the key's value). Passing only Content-Type drops
  the key (403s later); passing only the key resets Content-Type.
- A `--source .` deploy uploads uncommitted local files.

---

## 10. Standing decisions

Product decisions that still shape kept features.

- **Chat switches to a backup model when the main one stalls.** When the model
  stalls before it starts writing, chat retries once on a second model instead
  of twice on the same one, and a service failure is reported as "trouble
  reaching the AI service, ask again in a minute", never as "try a simpler
  question" (kept only for a question that genuinely needed more work than one
  answer allows).
- **Admins can view the app as a named person, read-only.** Impersonating a
  person shows their menus, lists and EOS items, but opens no Google account at
  all (no email, calendar or files), shows no call transcripts or anything built
  from them, and saves, sends or writes nothing. Pages not on an approved
  allowlist are closed while impersonating; a new page stays closed until it is
  deliberately added. Chat works in a narrow CRM-only form and keeps nothing in
  the person's history. Every start of an impersonation is audited.
- **Meeting prep keeps every version** and builds in the background with a
  notification (section 7). Deep Prep stays a separate emailed report, outside
  the version chain. Slack, call-capture transcripts, EOS and Smart Teams are not
  read by the brief until each has its own who-may-see-what decision.
- **Attendee research uses public professional sources only**, never personal
  topics (health, religion, politics, family, age, finances, home address,
  personal accounts), even when public. When a profile cannot be tied to the
  right company, no card is shown rather than risk describing the wrong person.
- **Files attached in chat are private to the person who attached them**, and
  publishing one makes a separate shared copy (section 7).
- **AI Cost matches the bill.** It prices the models' thinking tokens at the
  answer rate and shows them in their own column; it records every model call
  of a chat answer or scheduled task (not only the last), using the finished
  count of each call, never the streamed running counts; and it prices cached
  input at the cached rate. A recorded figure keeps the price that applied when
  it was recorded: past figures are never recalculated.
