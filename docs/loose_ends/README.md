# Loose Ends — the AE loop-closure scan

Built for **every AE**, not a one-AE pilot, and placed in Sales / My Day.

> This file is both the design contract the code was built against and the
> runbook. If the code and this file disagree, fix one of them in the same PR.

## 1. What it does, in one paragraph

Every weekday morning one central scan (a cubie-worker run, not a per-AE
assistant) reads each AE's own Gmail and Slack, reconciles what it finds
against HubSpot, and keeps a durable list of **loose ends** per AE:

| Kind | Key | What it means | Cleared when (detected, never self-reported) |
|---|---|---|---|
| Not in HubSpot | `unlogged` | The AE emailed a HubSpot contact (or someone at a HubSpot company with a deal in the company's pipelines), or reported a client conversation in Slack, and **no** email, call, meeting or note is on the contact, its company or its deals since then | A HubSpot email / call / meeting / note appears on the contact, company or any associated deal at or after (event − 1 day), or the AE logs it from Cubie |
| Waiting on your reply | `open_loop` | A client wrote directly to the AE and the AE has not answered in 2 business days | The thread shows a later message from the AE (or from a colleague replying to the client, e.g. a manager replying to all) — a saved draft never counts — or a HubSpot engagement is logged on the contact / company after the inbound message |
| You said you would | `todo` | A promise Cubie heard (email, Slack, meeting) is 5 days past its date (5 days if undated), or a HubSpot task owned by the AE is 5+ days overdue (only tasks that went overdue in the last 30 days, `LOOP_CLOSURE_TASK_LOOKBACK_DAYS`). Raised from 3 days on 2026-10-13: the list shows only to-dos 5 or more days overdue | The promise is kept / shows evidence / is withdrawn, or the HubSpot task is completed, deleted or reassigned. A date moved later (snoozed promise, rescheduled task) closes it `rescheduled` — not counted as fixed — and the same row re-opens if the new date also passes |

The list lives in Cubie (`loop_items`), so ages accumulate and closure is
re-checked against the system of record on every run. Nothing closes because it
merely fell out of the scan window.

Surfaces:

1. **Sales → Loose Ends** (`/sales/loose-ends`) — the AE's own list, with one
   real action per row (log it in HubSpot, draft the reply in Gmail, open the
   task). A **Team** tab for Sales leaders shows the owner-attributed aging view
   (the L10 artifact).
2. **My Day panel** — the three oldest loose ends, under the five blocks.
3. **Daily Slack digest** to one shared channel, `#sales`.
   It ships **off**: a Cubie admin turns it on and off, and changes the
   channel, in **Admin → Loose Ends** — no env var, no redeploy (§5 "Admin").
4. **Scorecard** — the weekly "Loose ends over 7 days" number per AE, written
   into the Sales scorecard measurables that carry that exact name.
5. **Ask Cubie** — "what loose ends do I have?" / "which loose ends on my team
   are over 7 days?"

## 2. Rules this feature keeps (and the ones it consciously changes)

* **Mailbox content never leaves its owner.** The scan reads each AE's mailbox
  centrally, which the rhythm checks never do for a team view. That is the
  explicit ask of the spec, so it is done under a narrower rule: **subjects,
  snippets and AI headlines are shown only to the AE they belong to.** The team
  view, the Slack digest, the scorecard and the team chat answer carry only the
  owner, the kind, the company / contact name (CRM data) and the age. Under
  impersonation the AE view hides titles and headlines too.
* **Who is scanned is data.** Registry key `loop_closure` (default: everyone
  in the workspace; `admin_allowed=False`, `exec_allowed=False`, so once an
  admin narrows it to a team, admins are never swept in by accident). Everyone
  on it gets the morning scan of their Gmail and Slack. Team view:
  `loop_closure_team` (ships with nobody; managers of the departments an admin
  adds, judged by `managed_only`, plus people added by email).
* **No leaderboard.** People are listed A–Z, items oldest first, every person
  in the same brand colour. No ranking, no "worst AE".
* **Every number carries its denominator** ("3 of 41 flags were marked not a
  real miss"; "checked 52 email threads, 31 Slack messages").
* **"Could not look" is never "all clear".** A mailbox that could not be read
  or was only partly read, Slack not connected or not searchable, a HubSpot
  owner that does not match, a HubSpot read that failed or came back partial, a
  judge backlog left for the next run, a per-AE scan that crashed — each is a
  named coverage state on the page, and none of them closes anything. The
  digest names the people it could not check (and leaves them out of "of N
  people scanned"); the scorecard leaves their cell alone instead of writing 0.
* **Closure is detected.** The AE cannot mark an item done. The only manual exit
  is **"Not a real miss"** (with a reason), which is counted as noise — the
  spec's success metric — and can mute that sender or domain for that AE.
* **HubSpot writes only from a click, in the request path, and only the AE's
  own sent email.** "Log it in HubSpot" logs the AE's actual email(s) — full
  text — as HubSpot EMAIL engagements on the contact, company and deal, the same
  record HubSpot's Gmail extension writes, with the body. An email engagement
  also moves the contact's "last contacted" date, which a note never did.
  **Loose Ends never writes notes** (call notes come from Fathom, which is why
  CRM Sync no longer writes them either) **and never writes model text into
  HubSpot.** Attachments are not logged. The page
  shows a preview of exactly those emails first (read from the AE's own
  mailbox), with a box per email: only the ones sent to the item's contact
  start ticked, so a forward to someone else lands on the client's record
  only if the AE ticks it. The POST names the ticked ones by Gmail message id
  and the server re-reads them, so the browser never supplies the content. The write goes through
  `retrieval/hubspot_write.py` (`create_email`, fenced by `EMAIL_WRITABLE`,
  outgoing only) with the write token that only the Service has, after a
  duplicate probe (`find_logged_email`). Slack items have no log button: they
  open the record in HubSpot. Jobs never write HubSpot.
* **Draft-only email.** "Draft a reply" creates a Gmail draft in the thread
  (with `In-Reply-To` / `References` from the client's message, so it threads
  on their side too); it never sends. The item stays open until the reply is
  actually sent: DRAFT (and TRASH / SPAM) messages are dropped when a thread is
  read, so a saved draft is never taken for a reply or for a sent email.
* **One digest a day, quiet days post nothing**, guarded by `loop_digest_posts`
  (unique channel + local date). This is the second
  sanctioned sales sender next to `rubik_core/cockpit/digest.py`, which says so.
* **The digest is off until an admin turns it on.** The switch and the channel
  are data (`workspace_settings` key `loop_closure_digest`,
  `loop_closure/digest_settings.py`), set in Admin → Loose Ends. A settings read
  that fails reads as OFF — a database hiccup never posts — and switching it ON
  is refused unless Slack knows the channel and the Cubie bot is in it.
* **AI does the judging, code does the rendering.** One Flash call per AE per
  morning decides which unanswered emails really expect a reply and which Slack
  messages report a client conversation; the model can only cite tags it was
  handed. Cost category `loop_closure` ("Loose Ends").

## 3. Data model (`rubik_core/database/loop_closure.py`, tables in the baseline migration)

House conventions: uuid4-hex string ids, naive-UTC `DateTime`, `JSON` (not
JSONB), string + `CheckConstraint(in_check(...))` instead of enums, lower-cased
email columns with `lower_check` and **no FK to users** (offboarding keeps the
rows). Constraints inline in `op.create_table`; the migration sets
`SET LOCAL lock_timeout` (it creates indexes).

### `loop_items`
| column | type | notes |
|---|---|---|
| id | String(32) pk | uuid4 hex |
| owner_email | String(320) | lower, indexed |
| kind | String(16) | `unlogged` \| `open_loop` \| `todo` |
| source | String(16) | `gmail` \| `slack` \| `promise` \| `hubspot_task` |
| source_key | String(255) | see below |
| status | String(16) | `open` \| `closed` \| `dismissed` |
| title | String(300) | owner-only (subject / task subject / promise title) |
| headline | String(300) null | AI one-liner, owner-only |
| counterparty_email | String(320) null | lower |
| counterparty_label | String(200) null | person name |
| company_label | String(200) null | HubSpot company name |
| hubspot_contact_id / hubspot_company_id / hubspot_deal_id | String(32) null | |
| thread_id | String(64) null | Gmail thread id |
| message_id | String(128) null | Gmail message id (open loop: the inbound one) |
| link | String(500) null | Gmail / Slack / HubSpot link for the owner |
| opened_at | DateTime | when the underlying event happened (sent / received / due) |
| first_flagged_at | DateTime | first scan that flagged it |
| last_checked_at | DateTime | last scan that re-checked it |
| closed_at | DateTime null | |
| close_reason | String(32) null | `logged_in_hubspot`, `logged_from_assistant`, `replied`, `logged_follow_up`, `task_completed`, `task_gone` (deleted, or reassigned to another owner), `promise_kept`, `promise_withdrawn`, `rescheduled`, `aged_out`. `rescheduled` and `aged_out` are never counted as fixed |
| close_evidence | JSON null | ids + timestamp only, e.g. `{"type":"notes","id":"123","at":"…"}` for a detected close; `{"type":"emails","ids":["…"],"message_ids":["…"],"at":"…","hubspot_url":"…"}` for `logged_from_assistant` |
| dismissed_at | DateTime null | |
| dismiss_reason | String(32) null | `not_a_client`, `automated`, `internal`, `handled_elsewhere`, `not_a_promise`, `other` |
| dismissed_by | String(320) null | |
| reopened_count | Integer default 0 | |
| detail | JSON null | ids / flags only (judge mode, match kind, Slack channel id …) plus `crm_contact_name`, the HubSpot contact's name — the only contact name a team surface shows. Never message text |
| created_at / updated_at | DateTime | |

Unique `(owner_email, kind, source_key)`; indexes `(owner_email, status)` and
`(status, opened_at)`.

`source_key` granularity:
* unlogged email — `gmail:<thread_id>` (one item per thread; if it was closed
  and a **new** unlogged AE email appears in that thread after `closed_at`, the
  item re-opens with `reopened_count += 1` and a new `opened_at`).
* unlogged Slack — `slack:<channel_id>:<ts>`.
* open loop — `gmail:<thread_id>:<inbound message id>` (each unanswered inbound
  message is its own loop; never re-opened).
* todo — `promise:<promise id>` or `hstask:<task id>`.

**A dismissed item is never re-opened or re-flagged** (same key). A todo
closed `rescheduled` re-opens (`reopened_count += 1`, `opened_at` = the new
date) when a later scan finds it past its new date's grace days.

### `loop_scan_runs`
`id, started_at, finished_at null, status (running|completed|failed), trigger
(scheduler|admin|cli), dry_run bool, report JSON (named buckets), coverage JSON
({owner_email: Coverage})`. Used for "last scanned at", per-AE coverage and the
run lock (a `running` row younger than 60 minutes blocks a second run).

`Coverage` (per AE, per run): `{"gmail": "ok|partial|unavailable", "slack":
"ok|not_connected|unavailable", "hubspot": "ok|no_owner|unavailable|partial",
"ai": "ok|unavailable", "threads_scanned": int, "slack_messages_scanned": int,
"promises_scanned": int, "tasks_scanned": int, "new": int, "closed": int,
"judge_deferred": int (only when > 0)}`. An AE whose scan crashed gets
`{"gmail": "unavailable", "slack": "unavailable", "hubspot": "unavailable",
"ai": "unavailable", "scan": "failed"}` — no counts, so nothing reads as an
empty inbox.

The run lock: a `running` row records `report.execution`
(`CLOUD_RUN_EXECUTION`). A Cloud Run retry (`CLOUD_RUN_TASK_ATTEMPT` > 0) that
finds a young `running` row of its OWN execution marks it failed
(`task_retried`) and scans; a row from any other execution still blocks. A
retry that does end up `locked` exits 1.

### `loop_judgements`
`id, owner_email (lower), kind, source_key, verdict ("declined"), judged_at`;
unique `(owner_email, kind, source_key)`. A candidate the judge declined is
never sent again (an open-loop key carries the inbound message id, so a new
client message is a new key). Pruned per AE once older than lookback + 2 days.
Ids and a time only.

### `loop_mutes`
`id, owner_email (lower), pattern (lower; "jane@acme.example" or "@acme.example"),
created_at, created_by`; unique `(owner_email, pattern)`.

### `loop_digest_posts`
`id, channel_key String(64) (case preserved), local_date String(10),
posted_at null, message_ts null, item_count int, created_at`; unique
`(channel_key, local_date)`. Insert + flush → post → stamp + commit; roll back
on failure so tomorrow (or a retry) can post.

## 4. The scan (`rubik_core/loop_closure/`)

```
rubik_core/loop_closure/
  __init__.py
  constants.py      kinds, statuses, reasons, labels, thresholds() from settings
  roster.py         roster_emails(db) — feature_allows("loop_closure") over users
  gmail_reader.py   per-AE thread gather (metadata only) → Thread/Message dataclasses
  hubspot_index.py  one index per run: contacts by email, companies by domain,
                    associations, engagement timestamps per record, owner tasks
  detect.py         pure functions: unlogged / open-loop / todo candidates + closure
  judge.py          the one Flash call per AE (@attribute_to("loop_closure"))
  store.py          the ONLY writer of loop_items / loop_mutes / loop_judgements
  scan.py           run_scan(db, *, emails=None, dry_run=False, now=None, deps=None)
  digest.py         Slack channel digest (metadata only) + send-once guard,
                    render_today() for the admin preview, last_post()
  digest_settings.py  the digest's on/off + channel (workspace_settings
                    "loop_closure_digest"): load() fails closed, save() checks
                    the channel and the bot's membership with Slack
  scorecard.py      weekly per-AE upsert + setup_metrics()
  views.py          read models for the router and the chat tool (mine / team)
  drafts.py         reply drafting for the "Draft a reply" action
```

### 4.1 Roster
`roster_emails(db) -> list[str]`: all `users` rows (with
`selectinload(User.roles)`) where `feature_allows("loop_closure", u, db)`,
lower-cased, filtered by `is_eligible_priorities_email` (no fixtures, no
off-domain) and minus `deactivated_priorities_emails` (fails open). Only the
morning scan runs the Slack / Google deactivation lookup; read paths (the Team
tab, the team chat answer) and roster-membership checks pass
`deactivated_lookup=lambda emails: set()`.

`run_scan(emails=[…])` and the scheduler's `?email=` never widen the roster: a
named address that is not on it is not scanned (`report.not_on_roster`; the
endpoint answers 400).

### 4.2 Gmail (per AE, DWD `GmailClient(email)`, metadata only)
Lanes over `LOOP_CLOSURE_LOOKBACK_DAYS` (14):
`in:inbox newer_than:14d -in:chats -category:promotions -category:social -category:forums`
and `in:sent newer_than:14d -in:chats`, up to 300 threads each (paging
`threads.list`; a lane that still has more past the cap makes coverage
`gmail: partial` — what was read still counts, and the page says the oldest
threads were not checked), de-duplicated. DRAFT, TRASH and SPAM messages are
dropped. Each message: id, thread id, from, to, cc, subject, date
(internalDate, aware UTC), snippet, labelIds. A client that cannot be built → coverage
`gmail: unavailable`, no Gmail findings and **no Gmail-based closures** for
that AE this run.

External = address domain not among the workspace's verified control domains,
not automated (`open_loops._is_automated` patterns +
no-reply), not muted for that AE.

### 4.3 HubSpot index (once per run, all AEs)
* `owner_directory()` → AE email → owner id (unmatched → `no_owner`; the
  record-centric checks still run, the task leg does not).
* Contacts for every external address seen: search `email IN [...]` in chunks;
  contact → companies / deals via `batch_associations`; deals filtered to
  the workspace's configured HubSpot pipelines.
* Companies by domain (`companies_for_domains`) for addresses that are not
  contacts; a company counts as a client only if it has a deal in one of
  the workspace's configured HubSpot pipelines.
* **Engagement index**: portal-wide search of `emails`, `calls`, `meetings`,
  `notes` with `hs_timestamp >= window_start`, in 7-day slices (a slice that
  hits the 5,000 cap marks the index `partial`), then
  `batch_associations(<type> → contacts|companies|deals)`. Result:
  `record ("contact:123" | "company:9" | "deal:77") → [(timestamp, type, id)]`.
  `window_start = min(now − lookback, opened_at of the oldest open
  unlogged/open-loop item) − 1 day` (an exact instant, not whole days),
  never earlier than `now − (LOOP_CLOSURE_MAX_AGE_DAYS + 1)` days.
* AE tasks: owner's tasks with `hs_task_status != COMPLETED` and
  `hs_timestamp < today − 5 days` (`LOOP_CLOSURE_TODO_GRACE_DAYS`); plus
  `batch_read` of the task ids of open `hstask:` items with `hs_task_status`,
  `hs_timestamp`, `hubspot_owner_id` (completed → close; missing or reassigned
  → `task_gone`; due date moved later, inside the grace → `rescheduled`).
* Any HubSpot failure → `hubspot: unavailable` for everyone: no unlogged
  findings, no HubSpot-based closures. `partial` → findings allowed, but no
  `unlogged` item is created when the evidence is missing only because of the cap
  (say so in coverage).
* Reads retry 429 / 5xx / transport errors (`rhythm_checks._retry`).

### 4.4 Detection rules (pure, in `detect.py`)
* **unlogged (email)**: a thread with at least one message **sent by the AE**
  to an external address that resolves to a HubSpot contact (or to a domain whose
  company has a deal in the company's pipelines), sent more than `LOOP_CLOSURE_LOG_GRACE_BUSINESS_DAYS`
  (1) business day ago, with no engagement on the contact / company / deals at
  or after `sent_at − 1 day`. `opened_at` = earliest such unlogged AE message.
* **unlogged (Slack)**: the AE's own Slack messages (`heard.fetch_own_slack`,
  last 7 days) that name a company on one of the AE's open deals
  (normalised name match, legal suffixes stripped, ≥ 4 chars), which the judge
  says **report a client conversation / call / meeting**, with no engagement on
  that company or its deals at or after `message time − 1 day`.
* **open loop**: a thread whose newest message is from an external, non-automated
  person, with the AE in **To** (not only Cc / a list), received more than
  `LOOP_CLOSURE_OPEN_LOOP_BUSINESS_DAYS` (2) business days ago
  (`business_hours_between`), with no later AE message; kept only if the judge
  says it expects a reply **and** the sender is a HubSpot contact / client
  domain or the judge says it is a business request from a client or prospect.
* **todo**: heard promises (`source in HEARD_SOURCES`, status in suggested / open /
  slipping, confidence ≥ 0.75) past `due_local + 5 days`, or undated and said
  ≥ 5 days ago; and the AE's overdue HubSpot tasks (above).
* Roster AEs that the priorities batch does not cover (`list_active_users`) get
  `run_accountability_pass(email)` first so their promises are heard too.
* Slack: an unknown Slack member id or a token without `search:read` is
  `slack: unavailable` (never "read, nothing there").

### 4.5 Closure re-check (every run, every open item of a scanned AE)
* unlogged → engagement-index hit on contact / company / deal at or after
  `opened_at − 1 day` → `logged_in_hubspot` (evidence: type, id, at).
* open loop → re-read the thread metadata (drafts dropped): an AE message after
  the inbound one → `replied` (evidence `gmail_message`); else a later message
  from an internal, non-automated colleague with the client among its
  recipients → `replied` (evidence `gmail_message_internal`); else an engagement on contact / company after the
  inbound message → `logged_follow_up`.
* todo → promise kept / evidence_found → `promise_kept`; dropped / handed_off →
  `promise_withdrawn`; still unkept but no longer past its grace because its
  date moved LATER (snoozed or re-dated) → `rescheduled`; task completed →
  `task_completed`; task missing, or now owned by someone else → `task_gone`;
  task due date moved later, inside the grace → `rescheduled`. Only a date
  that moved later is a reschedule (`detect.date_moved_later`): when the
  grace is RAISED, a to-do flagged under the old line stays open, the read
  models leave it out until it crosses the new one (`views.listed`), and it
  never shows "Reopened".
* Older than 60 days and still open → `aged_out` (reported separately, never
  counted as fixed).
* A source that could not be read this run decides nothing.
* The scan closes with a conditional `UPDATE … WHERE status = 'open'` and
  re-reads each AE's rows just before deciding, so a dismissal or a "Log it in
  HubSpot" committed while the scan was reading is kept.

### 4.6 The judge (one Flash call per AE)
Input: open-loop candidates tagged `E1..` (sender name + domain, HubSpot match
label, subject, last ≤ 4 messages as `from / date / snippet ≤ 300 chars`) and
Slack candidates tagged `S1..` (channel name, date, text ≤ 400 chars, the
company it named). Output JSON:
`{"emails":[{"tag","expects_reply":bool,"business_request":bool,"headline"}],
"slack":[{"tag","reports_client_touch":bool,"headline"}]}`. Unknown or
duplicate tags are dropped; headlines are plain text ≤ 120 chars. Model:
`settings.VERTEX_LLM_MODEL_FLASH` via the shared client, `temperature=0`.
If the call fails — or parses but carries no verdict for any tag it was handed
(a reply in the wrong shape; a single-element list wrapping the object is
unwrapped first) — no **new** open-loop or Slack items this run (existing ones
are still closure-checked) and coverage `ai: unavailable`.

Caps: 40 emails and 30 Slack messages per call, oldest first. A candidate the
judge declined is stored in `loop_judgements` and never sent again, so the cap
rotates through a backlog instead of re-asking about the same oldest threads.
Fresh candidates past the cap wait for the next run and are counted
(`report.judge.over_cap`, coverage `judge_deferred`, and a sentence on the
page). A tag the reply skipped is not stored and is asked again.

### 4.7 Thresholds (all in `config.py`)
| setting | default |
|---|---|
| `LOOP_CLOSURE_ENABLED` | False (pinned true in deploy.sh + deploy_worker_job.sh) |
| `LOOP_CLOSURE_LOOKBACK_DAYS` | 14 |
| `LOOP_CLOSURE_OPEN_LOOP_BUSINESS_DAYS` | 2 |
| `LOOP_CLOSURE_LOG_GRACE_BUSINESS_DAYS` | 1 |
| `LOOP_CLOSURE_TODO_GRACE_DAYS` | 5 (was 3) |
| `LOOP_CLOSURE_TODO_UNDATED_DAYS` | 5 |
| `LOOP_CLOSURE_TASK_LOOKBACK_DAYS` | 30 (new HubSpot task loose ends only for tasks overdue within this many days) |
| `LOOP_CLOSURE_ESCALATION_DAYS` | 7 |
| `LOOP_CLOSURE_MAX_AGE_DAYS` | 60 |
| Admin → Workspace operations → Loose Ends Scorecard metric | Exact measurable name for this workspace; blank disables Scorecard setup/writeback. |

Age chip (cockpit `AgeChip` states): `at_risk` while days open < escalation
days, `breached` at or over it. Label: `"4 days"` / `"1 day"`. Days open are
whole 24-hour days, except a HubSpot task, which counts calendar days in the
business timezone (the way the scan flags it: due Oct 8 is 5 days overdue on
Oct 13 at any hour). The scorecard's "over 7 days" uses the same count.

**Listing rule** (`views.listed`): every read model — the AE's page, the Team
tab, the digest, the chat answer and the scorecard — lists a to-do only once
it is `todo_grace_days` overdue (`todo_undated_days` for an undated promise),
so the counts on every surface agree with the rows shown.

## 5. HTTP contract

Router `backend/routers/loose_ends.py`, prefix `/sales/loose-ends`, mounted on
the `core` parent. Gates: `LOOP_CLOSURE_ENABLED` off → 404; not on
`loop_closure` → 403 (team endpoints: `loop_closure_team`). `/api/users/me`
folds the kill switch into both keys. `response_model_exclude_none=True`, so a
value that is `None` (no scan yet, no headline, no noise rate) is **left out**
of the JSON, not sent as `null`; clients test with `== null`.

### `GET /api/sales/loose-ends` (mine)
```json
{
  "scanned_at": "2026-10-07T12:04:11",            // naive UTC; absent = never scanned
  "coverage": {"gmail":"ok","slack":"not_connected","hubspot":"ok","ai":"ok",
               "threads_scanned":52,"slack_messages_scanned":0,
               "promises_scanned":4,"tasks_scanned":7},   // absent before first scan
  "notes": ["Slack is not connected, so Slack messages were not checked. Connect it in Settings → Slack."],
  "totals": {"open":6,"over_escalation":2,"flagged_this_week":5,"closed_this_week":3},
  "groups": [
    {"kind":"unlogged","label":"Not in HubSpot","hint":"…","empty_line":"…","count":3,"items":[LooseEnd]},
    {"kind":"open_loop","label":"Waiting on your reply","hint":"…","empty_line":"…","count":2,"items":[…]},
    {"kind":"todo","label":"You said you would","hint":"…","empty_line":"…","count":1,"items":[…]}
  ],
  "thresholds": {"open_loop_business_days":2,"todo_grace_days":5,"todo_undated_days":5,"escalation_days":7},
  "mutes": [{"id":"…","pattern":"@acme.example","created_at":"…"}],
  "content_hidden": false                          // true under impersonation
}
```
`LooseEnd`:
```json
{"id":"…","kind":"open_loop","source":"gmail",
 "title":"Re: quarterly review pricing",           // "" when content_hidden
 "headline":"Asks for updated pricing before Friday",   // omitted when none/hidden
 "counterparty":"Jane Doe","company":"Acme Corp",
 "opened_at":"…","first_flagged_at":"…","days_open":4,
 "age":{"state":"at_risk","label":"4 days"},
 "reopened":false,
 "primary_action":{"kind":"draft_reply","label":"Draft a reply"},
 "secondary_action":{"kind":"open_link","label":"Open in Gmail","href":"https://mail.google.com/…"}
}
```
No row carries what "Log it in HubSpot" would log: the page asks for it
(`GET …/log-preview`) when the AE clicks, read fresh from their own mailbox.
Action kinds (closed set, mirrored in `frontend/src/api/looseEnds.ts`):
`log_in_hubspot` (GET …/log-preview, then POST …/log), `draft_reply` (POST
…/draft-reply), `open_link` (href, new tab). Primary by kind: unlogged email →
`log_in_hubspot` only when a HubSpot **contact** id is known (an email
engagement on no contact shows on no record), else `open_link` "Open in
HubSpot" to the company / deal (secondary: Gmail), else the Gmail link;
unlogged Slack → `open_link` "Open in HubSpot" to the company / deal record
(secondary: the Slack message) — **never a log button**; open_loop →
`draft_reply`; todo → `open_link` (thread / HubSpot task). A row
with no link of its own falls back to "Open My To-Dos" (`/todos`) only when
the reader holds the `todos` feature; otherwise it is an `open_link` with no
`href` ("No link to open"), which the page renders disabled with a reason.

### Mutations (own items only; 404 for someone else's id)
* `GET /api/sales/loose-ends/{id}/log-preview` →
  ```json
  {"emails":[{"message_id":"…","subject":"Re: quarterly review pricing",
              "to":["jane@acme.example"],"cc":[],"sent_at":"2026-10-05T15:00:00",
              "excerpt":"first 600 characters of the text…","chars":1840,
              "truncated":false,"plain_text_only":false,"to_contact":true,
              "already_logged":false}],
   "contact":"Jane Doe","company":"Acme Corp","deal":"Quarterly review 2026",
   "hubspot_url":"https://app.hubspot.com/contacts/…"}
  ```
  Reads the thread from the AE's OWN mailbox (`format="full"`) and lists each
  email the AE sent (From the owner, labelled SENT, not DRAFT / TRASH / SPAM)
  since `opened_at` − 1 minute, to the counterparty (on any address) or to
  another possible client contact: a business address outside
  the workspace's verified control domains that is not automated and not a personal
  mailbox (`PUBLIC_EMAIL_DOMAINS`), so a forward to `@gmail.com` — the AE's
  own included — never qualifies. `to_contact` is true when To or Cc holds the
  counterparty; the page ticks only those by default and logs only what the
  AE ticks, so a forward to a partner goes on the client's record only if the
  AE ticks it. Body: the MIME tree is walked, attachments skipped,
  `text/plain` preferred (text derived from `text/html` when there is none).
  HubSpot caps a text property at 65,536 characters: the text is cut at
  65,000 (`truncated`), and an HTML body over 65,000 is left out rather than
  cut mid-tag, so the email is logged as its plain text (`plain_text_only`).
  `already_logged` is, first, the ids Cubie itself wrote for this still-open
  item (`detail["cubie_logged"]`, `{message id: HubSpot email id}`, stored
  after a failure partway or a batch with `more`: HubSpot's search lags a
  fresh write by up to a minute), then the duplicate probe
  (`logged_email_candidates`: email engagements within ±5 minutes of the sent
  time, same subject, on the item's contact, closest first, backing off on a
  429), matched one to one across the thread so one HubSpot email never
  stands for two sent ones (a same-subject resend three minutes later). `sent_at` is
  naive UTC; `deal` is named only when the deal is in one of the company's pipelines (absent
  otherwise — the email is then logged on the contact and company). Own items
  only (404); an open unlogged **email** item only (409 with a sentence); 409
  under impersonation (this GET reads a mailbox, which the impersonation
  middleware would allow); Gmail unreadable or the HubSpot probe failed → 502
  with a sentence. Writes nothing.
* `POST /api/sales/loose-ends/{id}/log` body `{"message_ids":["…"],"more":false}`
  (1–20 ids, else 400; the page sends more than 20 ticked emails in batches,
  `more: true` on all but the last) → `{"status":"logged","hubspot_url":"…","logged":n_created,"already":n_already}`
  (`"status":"in_progress"` for a batch with `more`: the item stays open with
  this batch remembered in `detail["cubie_logged"]`).
  Item must be `open`, `unlogged` and from Gmail (409 otherwise; 400 for
  another kind). The thread is RE-READ server-side; an id that is not one of
  the AE's loggable sent emails is a 400. Each email is probed and, unless
  HubSpot already has it, written with `create_email`: `hs_timestamp` = sent
  time (epoch ms), `hs_email_direction` `EMAIL`, `hs_email_status` `SENT`,
  subject, full text, HTML when present, and `hs_email_headers` (from / to /
  cc), owned by the AE's HubSpot owner, associated to the contact, the company
  and — if in one of the company's pipelines and still there; a refusal, or a deal deleted
  since the scan (`HubSpotDealGone`), drops the deal and retries once — the
  deal. The POST is retried on a 429 only: a timeout or a 5xx can arrive after
  HubSpot stored the email, so it raises instead of posting it twice. Closes
  the item `logged_from_assistant` with ids only as evidence (every batch's)
  (`{"type":"emails","ids":[…],"message_ids":[…],"hubspot_url":…}`, never a
  subject or body). Idempotent: the row is claimed with `SELECT … FOR UPDATE`
  before the write, so a concurrent second call waits and then answers with
  the stored result (`logged: 0`, `already: n`) (SQLite ignores the lock; the
  re-check after the claim still applies). If a write fails the item stays
  open: 502 with `friendly_write_error` text. The ids of any emails written
  before the failure are stored on the item first (`detail["cubie_logged"]`),
  so the page's refreshed preview shows them as already in HubSpot and a
  retry never writes them again, even before HubSpot's search shows them.
  Refused (409) under impersonation.
* `POST /api/sales/loose-ends/{id}/draft-reply` → `{"status":"drafted","draft_url":"…"}`.
  Item must be `open` and `open_loop`. Reads the thread (own mailbox), one Flash
  call, `GmailClient.create_draft(..., thread_id=…)`. Refused under
  impersonation. The item stays open.
* `POST /api/sales/loose-ends/{id}/dismiss` body
  `{"reason":"not_a_client","mute":"sender"|"domain"|null}` → `{"status":"dismissed"}`.
* `DELETE /api/sales/loose-ends/mutes/{mute_id}` → `{"status":"removed"}`.

### `GET /api/sales/loose-ends/team`
```json
{
  "scanned_at":"…",
  "people":[{"email":"…","name":"Jane Smith","open":6,
             "by_kind":{"unlogged":3,"open_loop":2,"todo":1},
             "aging":{"d0_2":1,"d3_7":3,"d8_14":1,"d15_plus":1},
             "over_escalation":2,"closed_this_week":3,
             "coverage":{"gmail":"ok","slack":"not_connected","hubspot":"ok","ai":"ok"},
             "items":[{"id":"…","kind":"open_loop","who":"Jane Doe · Acme Corp",
                       "days_open":4,"age":{"state":"at_risk","label":"4 days"}}]}],
  "roster_size":9,
  "health":{"window_days":30,"flagged":41,"dismissed":3,"noise_rate":0.073,
            "dismissed_by_reason":{"automated":2,"not_a_client":1},
            "closed":22,"closed_by_reason":{"replied":9,"logged_in_hubspot":8,"logged_from_assistant":5,"rescheduled":2},
            "median_days_to_close":2.5,
            "weekly":[{"week_start":"2026-09-14","flagged":9,"closed":7,"median_days_to_close":3.0}]},
  "scorecard":{"metric_name":"Loose ends over 7 days","metrics_found":0,"people_without_metric":9,
               "people_without_seat":["new.rep@acme.example"],"can_setup":true},
  "thresholds":{"open_loop_business_days":2,"todo_grace_days":5,"todo_undated_days":5,"escalation_days":7}
}
```
People sorted A–Z by name; items oldest first (max 25 per person); **no titles,
headlines, subjects or snippets** in this response. `who` is HubSpot names
only (`detail.crm_contact_name` and `company_label`, else "No contact named") —
never a mail-header name, a raw address or a model-extracted name. Every read
writes an audit row (cross-person read) naming the real reader: under
impersonation the admin (`request.state.impersonation.actor_email`), with
`details.impersonating`.

`health` is ONE cohort, the items first flagged in the last `window_days`:
`dismissed` and `closed` are out of `flagged`. `closed` counts fixes only;
`closed_by_reason` also lists `rescheduled` (never in `closed` or the median;
the page prints it as "N, not in the 22"). `scorecard.can_setup` is true only
when someone with an Accountability Chart seat lacks the row;
`people_without_seat` are the ones setup always skips.

* `POST /api/sales/loose-ends/team/scorecard-setup` → creates the missing
  per-AE measurables in the Sales scorecard (owner = the AE's `eos_people` row;
  AEs without one are listed back), `value_type="number"`, `comparator="<="`,
  `target_value="0"`, weekly, with a description saying Cubie fills it.
  Returns `{"created":n,"skipped_no_person":[emails],"already":n}`.

### Admin (`backend/routers/loose_ends_admin.py`)
Prefix `/admin/loose-ends`, mounted under `/api` like `sales_admin` (not on
`core`); every endpoint is `admin_required` (403 otherwise). Admin-only by
ROLE, so there is no Feature Access key. Client: `frontend/src/api/
looseEndsAdmin.ts` (Admin → Loose Ends tab); `looseEndsAdmin.test.ts` pins the
paths and field names against this router.

* `GET /api/admin/loose-ends/digest` →
  `{"enabled","channel_name","channel_id","bot_in_channel","channel_archived",
  "updated_by","updated_at","scan_enabled","last_post"}`. Fresh install:
  `enabled:false`, `channel_name:"#sales"`, `channel_id:null`,
  `bot_in_channel:null` (not checked yet), `channel_archived:false`.
  `scan_enabled` is `LOOP_CLOSURE_ENABLED` here (an "on" post with the
  scan off never goes out). `last_post` is the latest row of
  `loop_digest_posts` with a `posted_at` —
  `{"local_date","posted_at","item_count","channel_name"}` — or `null`.
* `PUT /api/admin/loose-ends/digest` `{"enabled": bool, "channel": str}` →
  the same shape. `channel` is `sales`, `#sales` or an id (`C0123ABCD`: typed
  bare, upper-case, with a digit — `#CUSTOMERSUCCESS` is a name, and an
  id-shaped token Slack has no id for is retried as a lower-case name); the
  Cubie bot looks it up (`SlackBotClient.find_channel`: `conversations.info`
  for an id, which answers for an archived channel too and returns
  `is_archived`; `conversations.list` public + private, archived left out, by
  name). Every save asks Slack again, so saving unchanged values is the
  re-check after an invite (the card offers "Save and re-check" while the bot
  check is not a ✓). 400 with a
  sentence as `detail` when: the name is impossible; Slack cannot see the
  channel ("Cubie can't see a channel called #x …" — for a NEW channel, even
  while off; turning the CURRENT channel off always works); switching ON with
  the bot not in it ("Invite Cubie to #sales first: type /invite @Cubie in
  #sales, then turn this on."); switching ON an archived channel; switching ON
  with no Slack app installed or Slack not answering (when the post was
  already on, the sentence says nothing changed and it is still on for the old
  channel). Nothing is stored on a 400. Switching OFF is always
  allowed and stores `bot_in_channel` as found. Every save writes a
  `LOOSE_ENDS_DIGEST_SETTINGS` audit row, `details` = enabled + channel name /
  id only.
* `POST /api/admin/loose-ends/digest/preview` →
  `{"text","would_post","reason"}`: today's post from the same builder, over
  the roster the scan posts over (`roster_emails` with its default "has left"
  lookup) and the same "could not check" rule. No Slack call, no guard row.
  `text` is `""` on a quiet day; `would_post` is whether the next morning's
  scan will post it; `reason` says why not (quiet, off, the scan switched off
  on this server, the channel archived, already posted today) — or, with
  `would_post:true`, is the caveat that Slack refused the last post because
  the bot was not in the channel (the scan tries again; Save re-checks now).

The digest's statuses (`digest.STATUSES`, in the scan report's `digest`):
`off` (not switched on — normal, quiet, touches nothing) | `quiet` | `posted` |
`already_posted` | `bot_unavailable` | `not_in_channel` (Slack refused:
`not_in_channel` / `channel_not_found`; no guard row, and the card's bot check
flips to the invite fix) | `channel_archived` (Slack refused: `is_archived`;
no guard row, and the card says to pick another channel) | `failed` |
`dry_run`. What a post learns is written onto the settings only while they
still point at the channel it posted to, under a row lock
(`digest_settings._record`).

### Scheduler
`POST /api/agents/jobs/loop-closure/scan` on `scheduler_router`
(`X-Scheduler-Key` or an admin). Flag off → 200
`{"status":"disabled"}` (never 404). `?email=x` must be on the `loop_closure`
roster (400 otherwise, dry run included). A run started by a person (not the
scheduler key) writes a `LOOSE_ENDS_MANUAL_SCAN` audit row (actor, target
email or `roster`, `dry_run`). `?dry_run=true&email=x` runs inline for that
one AE and rolls back. Otherwise launches
`backend.jobs.loop_closure_scan_job` on cubie-worker with the Service's flag
values passed as env, and `--trigger scheduler|admin` so `loop_scan_runs.trigger`
records who started it (a hand run defaults to `cli`).

## 6. Runbook

Turning it on (after the guarded `scripts/platform/deploy.sh --env <private-env>`
release, which applies the Job and schedule manifests):

1. Check the roster in Admin → Feature Access → **Loose Ends** (default:
   everyone). Everyone on it gets the morning scan of their Gmail and Slack, so
   narrow it to the team that works client threads first if that is who it is for.
2. The daily Slack post ships **off**. When the team is ready for it: in
   `#sales`, type `/invite @Cubie`; then in **Admin → Loose Ends** press
   **Preview today's post** first, read it, switch the post on (channel
   `#sales`) and Save. Save checks with Slack that Cubie can see the channel
   and is in it, and refuses with the exact fix if not. To stop it, switch it
   off and Save — no deploy either way.
3. Dry run for one AE:
   `curl -X POST "$SERVICE/api/agents/jobs/loop-closure/scan?dry_run=true&email=<ae>" -H "X-Scheduler-Key: $KEY"`
4. Create the schedule (weekday 07:10 in the company's business time zone, after the 06:00 UTC
   priorities batch and clear of the :00/:15 transcript sync):
   ```
   gcloud scheduler jobs create http loop-closure-scan \
     --project "$GCP_PROJECT_ID" --location us-central1 \
     --schedule "10 7 * * 1-5" --time-zone "<business time zone>" \
     --uri "https://<service-url>/api/agents/jobs/loop-closure/scan" \
     --http-method POST \
     --headers "X-Scheduler-Key=$(gcloud secrets versions access latest --secret=SCHEDULER_API_KEY --project="$GCP_PROJECT_ID")" \
     --attempt-deadline 300s
   ```
5. Sales leaders: Sales → Loose Ends → Team → **Add the scorecard rows** once.
6. Read a run: `[loop-closure] done` in cubie-worker logs (named buckets). A
   run in which every AE's scan failed is `status: failed`
   (`error: every_ae_failed`): no digest, no scorecard, exit 1.
   Stop everything: `gcloud scheduler jobs pause loop-closure-scan --location us-central1 --project "$GCP_PROJECT_ID"`.

Week-one tuning (from the spec): watch the noise rate on the Team tab. If it is
over 20% because of newsletters / internal CCs / auto-replies, raise
`LOOP_CLOSURE_OPEN_LOOP_BUSINESS_DAYS` to 3 before touching anything else.

What it cannot measure: **catch-rate** needs a hand-checked sample (pick one AE,
scrub a day by hand, compare); **manual hours saved** is an estimate for the
team to make.
