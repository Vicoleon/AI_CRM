# Insights: Cubie's BI dashboards

Insights is one place to see what Cubie produces and how it is used: adoption,
Ask Cubie, AI Priorities, HubSpot updates, proposals, follow-through, AI spend,
knowledge and quality. Every dashboard can be filtered by department.

EOS health is not repeated here: the EOS Dashboard (`/eos/dashboard`) answers
it. An EOS Health dashboard shipped once and was removed as redundant.

This file is the contract between the backend (`rubik_core/insights/`,
`backend/routers/insights.py`) and the frontend (`frontend/src/pages/insights/`,
`frontend/src/components/insights/`, `frontend/src/api/insights.ts`). Change it
in the same PR as either side.

## 1. Who sees what

- **Feature keys** (Admin → Feature Access, `rubik_core/features/registry.py`):
  - `insights` opens the dashboards.
  - `insights_people` adds the per-person tables and the person profile.
  - Both ship with admins and execs allowed, `membership="managed_only"`.
- **Kill switch:** `INSIGHTS_ENABLED`. When it is off, every endpoint returns
  404 and the nav hides the area.
- **Data scope (structural, in `rubik_core/insights/scope.py`):**
  - Admins and org-wide execs (`UserProfile.sees_all_departments`) see every
    department.
  - Anyone else sees their managed departments plus their primary
    department, as RAG slugs.
  - A `dept=` value outside the viewer's scope returns 403. A person outside
    it returns 404.
- **People in scope:**
  - Included: `users` rows whose department slug is in scope, with an email
    on the Workspace domain.
  - Excluded: `general`, the virtual impersonation placeholder, and the
    workspace's `insights_excluded_emails` operations policy.
  - Departed people resolve through the `offboard_records.department` snapshot.
- **Never shown:**
  - Any department figure covering fewer than 3 people, where it would
    identify someone (see "Withheld values" in §4).
  - Per-person Call Capture or wrap-up numbers (Google Meet API terms); these
    are department-level only.
  - Any Smart Teams data.
  - Any message, email, prompt or transcript content. Counts only.

## 2. Dashboards

| slug | title | group | phase |
|---|---|---|---|
| `overview` | Overview | company | 1 |
| `adoption` | Adoption & Engagement | company | 1 |
| `ask-cubie` | Ask Cubie | work | 1 |
| `priorities` | AI Priorities | work | 1 |
| `follow-through` | Follow-through | work | 2 |
| `crm` | CRM & HubSpot | sales | 2 |
| `proposals` | Proposals & Prep | sales | 2 |
| `ai-spend` | AI Spend | platform | 3 |
| `knowledge` | Knowledge & Content | platform | 3 |
| `quality` | Quality & Trust | platform | 3 |

Groups render in this order: company "Company", work "Work", sales "Sales",
platform "Platform".

A dashboard that has not shipped yet is still listed by `/meta`, with
`available: false`. The frontend hides it from navigation.

## 3. Filters (query string, identical on every GET)

| param | values | default |
|---|---|---|
| `dept` | comma-separated department slugs; empty = everything in scope | empty |
| `range` | `7d`, `30d`, `90d`, `qtd`, `ytd`, `custom` | `30d` |
| `start`, `end` | `YYYY-MM-DD`, inclusive, calendar days in the business time zone; required when `range=custom`; at most 366 days | — |
| `refresh` | `1` bypasses the 10-minute cache | — |

- **Previous period:** the same number of days immediately before `start`.
- **Buckets:** daily when the period is 31 days or fewer, otherwise weekly
  (weeks start on Monday).
- **Time zone:** all days are in the active workspace's control-row IANA
  business time zone.

## 4. Endpoints (all under `/api`, mounted on `core`)

### `GET /insights/meta`
```json
{
  "departments": [{"slug": "sales", "label": "Sales", "people": 15}],
  "all_departments": true,
  "can_view_people": true,
  "dashboards": [{"slug": "overview", "title": "Overview", "group": "company", "available": true, "description": "…"}],
  "metrics": {"active_people": {"id": "active_people", "label": "Active people", "definition": "…", "denominator": "people with Cubie access in scope", "unit": "count", "good_direction": "up", "part_unit": null}},
  "tz": "UTC",
  "ranges": ["7d", "30d", "90d", "qtd", "ytd", "custom"]
}
```

Each metric's `part_unit` is the unit of a `pct` KPI's `numerator` and
`denominator` when they are not counts, and `null` otherwise. AI Spend's
"Spend tied to a person" is a share of dollars (`part_unit: "usd"`), so it
reads "$57 of $215 AI spend in the period", not "57.1 of 215.4".

### `GET /insights/dashboards/{slug}?dept=&range=&start=&end=&refresh=`
Returns a **DashboardPayload**:
```json
{
  "dashboard": "priorities",
  "title": "AI Priorities",
  "generated_at": "2026-10-08T15:42:00Z",
  "cached": false,
  "period": {"start": "2026-09-09", "end": "2026-10-08", "prev_start": "2026-08-10", "prev_end": "2026-09-08",
             "days": 30, "bucket": "day", "tz": "UTC", "label": "Last 30 days"},
  "scope": {"departments": ["sales"], "labels": ["Sales"], "all": false, "people": 15},
  "kpis": [Kpi, …],
  "sections": [Section, …],
  "notes": ["Slack delivery is tracked from Oct 9, 2026."]
}
```

**Kpi**
```json
{"id": "priorities_acted_rate", "value": 0.33, "prev_value": 0.29,
 "numerator": 212, "denominator": 640, "spark": [0.2, 0.31, …], "href": "/insights/priorities"}
```
- `id` must exist in the metric catalog, which supplies the label, unit,
  definition and good direction.
- `pct` values are 0–1.
- Every `pct` KPI carries `numerator` and `denominator`.
- `prev_value`, `spark` and `href` are optional. On Overview, `href` points to
  the dashboard that owns the number.
- A `spark` has one point per bucket. Its points are all `null` when the
  number has no honest daily value, and `prev_value` is `null` when the
  previous period cannot be measured the same way (a source whose history
  starts later than the previous period, `tracked_from`).
- `value` may be `null` when it cannot be computed (for example, the
  denominator is 0).

**Section**: `{"id", "kind", "title", "subtitle"?, "width": "full"|"half"|"third", "drill": "department"|"person"|null, "empty_hint"?, "data"}`

The shape of `data` depends on `kind`:

- `timeseries`
  ```json
  {"x": ["2026-09-09", …], "chart": "line"|"bar", "stacked": false,
   "series": [{"key": "dau", "label": "Daily active", "unit": "count", "values": [3, 5, …]}]}
  ```
- `bar` (a ranked horizontal list)
  ```json
  {"unit": "count"|"pct"|…, "items": [{"key": "sales", "label": "Sales", "value": 0.8, "numerator": 12, "denominator": 15}]}
  ```
  - Drill `department` means `key` is a department slug; clicking an item
    filters to it.
  - Drill `person` means `key` is an email; clicking opens the profile.
- `funnel`
  ```json
  {"mode": "sequential"|"breakdown", "steps": [{"key": "surfaced", "label": "Surfaced", "value": 640}, …]}
  ```
  The rate shown for each step is relative to the first step.
  - `sequential` (the default): each step is a subset of the one before, so
    the frontend also shows the step-to-step rate.
  - `breakdown`: the first step is the whole and the rest split it into
    outcomes that add up to it (for example, what happened to each surfaced
    card). No step-to-step rate is shown.
- `matrix` (covers heatmaps and cohorts too)
  ```json
  {"unit": "pct"|"count", "scale": "sequential"|"diverging",
   "rows": [{"key": "sales", "label": "Sales"}], "cols": [{"key": "chat", "label": "Ask Cubie"}],
   "cells": [[{"value": 0.8, "numerator": 12, "denominator": 15} | null, …], …]}
  ```
  When drill is `department`, clicking a row filters to it.
- `table`
  ```json
  {"columns": [{"key": "name", "label": "Person", "unit": "text"|"count"|"pct"|"usd"|"ms"|"days"|"date"|"datetime"|"ratio"|"score"}],
   "rows": [{"key": "avery.stone@acme.example", "cells": {"name": "Avery …", "department": "Sales", "active_days": 12}}],
   "row_link": "person"|null, "default_sort": {"key": "active_days", "dir": "desc"}}
  ```
  Tables that name people are only included when the viewer has
  `insights_people`. A table whose rows are not people (Quality & Trust's
  trust scorecard, one row per measure) has `row_link: null` and no drill.

**Withheld values.** A bar item or matrix cell can come back as
`{"value": null, "note": "Hidden: fewer than 3 people"}`. The server does this
when a group is so small that its number would identify a person:

- Call Capture department figures, for every viewer.
- Department bars, cohort rows and Overview department rows, for viewers
  without `insights_people`.

The frontend shows "Hidden" with the note, never "—" or 0.

**Units:** `count`, `pct` (0–1), `usd`, `ms`, `days`, `ratio` (for example 2.4 turns), `score` (0–1), `text`, `date`, `datetime`.

### `GET /insights/people/{email}?range=`
Requires `insights_people`. The person must be in the viewer's scope (404
otherwise). Each read writes an `INSIGHTS_PERSON_VIEW` audit row.
```json
{
  "person": {"email": "…", "name": "…", "department": "sales", "department_label": "Sales", "last_active": "2026-10-07"},
  "period": {…same as dashboards…},
  "calendar": [{"date": "2026-07-11", "count": 5}],
  "kpis": [Kpi, …],
  "cards": [{"dashboard": "priorities", "title": "AI Priorities", "kpis": [Kpi, …]}],
  "features": [{"key": "loop_closure", "label": "Loose Ends", "has_access": true, "used": false}],
  "notes": []
}
```
`calendar` always covers the 90 days ending at `period.end`.

### `POST /insights/dashboards/{slug}/summary?<same filters>`
- Recomputes the payload (served from cache when it can) and sends Gemini the
  department-level KPIs and notes only. It never sends names.
- Response:
  ```json
  {"available": true, "headline": "…", "bullets": ["…"], "watch": ["…"], "generated_at": "…", "cached": false}
  ```
- When AI is unavailable: `{"available": false, "reason": "…"}`.
- AI Cost category: `insights_summary`.

## 5. Data sources and their history

| Source | Feeds | History |
|---|---|---|
| `activity_daily` (new) | Adoption, person calendar | from the first deploy of Insights |
| `priority_events` (new) | Priorities funnel, time to action, Slack delivery | from the first deploy of Insights |
| `home_cards.metadata_json.priorities[]` | Priorities "surfaced" | full |
| `priority_memory` | Priorities state before `priority_events` | latest state only |
| `login_events`, `interaction_logs`, `chat_sessions`, `llm_usage_events` (user-attributed rows), `audit_logs`, `meeting_prep_runs`, `proposals`, `crm_sync_suggestions`, `promise_events` | "Active" signals | full |
| `assistant_feedback` | ratings | full (settled ratings, not clicks) |
| `crm_sync_suggestions` | CRM: suggestions, acceptance (approved ÷ approved or dismissed, for the suggestions made in the period), time to approve, writes and failures | full |
| `call_hubspot_writes` + `call_sessions.ae_email`, `call_extraction_items` | CRM: Call Capture writes and wrap-up outcomes, **department only** | full |
| `commitment_nudges` (sent with a HubSpot task), `commitment_stage_lags` (applied) | CRM: Cockpit nudges and deal stage corrections, by sender / resolver | full |
| `loop_items` | Follow-through: flagged, closed (fixed reasons only), "not a real miss" (noise), days to close, open aging; CRM: emails logged from Loose Ends | from the first Loose Ends scan (Oct 7, 2026); the note is computed from the earliest `first_flagged_at` |
| `promises`, `promise_events` | Follow-through: kept ÷ (kept + dropped), slipping now (a snapshot), promoted to EOS | full |
| `commitments` | Follow-through: missed ÷ due, **department only** (call wrap-ups) | full |
| `proposals`, `proposal_versions` | Proposals: started → confirmed → drafted, revisions, deal link, days to draft | full |
| `meeting_prep_runs` (`mode`, `change_kind`, `delivery_status`) | Proposals: briefs, deep share, edit rate, failure rate | briefs full; edits, rewrites and failures from the first row with a `change_kind` (Meeting Prep versions) |
| `xray_snapshots` | Proposals: X-Ray snapshots | full |
| `call_session_scripts` + `call_sessions.ae_email` | Proposals: call scripts used, **department only** | full |
| `llm_usage_events` (feature, model, cost, person; grouped in SQL) | AI Spend, Overview's spend tiles | full for the whole company; spend tied to a person complete from the first nightly AI Priorities row that carries one (the Insights release) |
| `assets`, `asset_versions` | Knowledge & Content | full |
| `chat_messages.metadata_json["citations"]` (its length only, computed in SQL) | Knowledge & Content | full |
| `assistant_feedback` (every surface), `meeting_prep_runs` versions, `proposal_versions`, `crm_sync_suggestions`, `loop_items` | Quality & Trust: rated useful, briefs rewritten, proposals revised, CRM Sync approved ÷ decided (by the day decided), Loose Ends noise | brief versions from Oct 1, 2026; Loose Ends from Oct 7, 2026; the rest full |

**Department-only sources.** Anything from a recorded call (the rows marked
"department only" above) never reaches a people table or a person's card. A
department's figure from them is withheld for every viewer when the group
behind it is under 3 people: the department's people who can reach Call
Capture (Feature Access) plus anyone whose calls produced rows
(`rubik_core/insights/sources.py`). When a department is withheld, its rows
are left out of the view's totals too, and the payload's `notes` say so.

**Never read** from these sources: CRM Sync payloads, evidence and HubSpot
responses; call write request and response bodies; nudge task text; stage-lag
evidence; a loose end's title, AI headline, counterparty, company, link or
evidence; proposal briefs, prose and revision instructions; meeting titles,
context and briefs; X-Ray sections; promise titles and details; document
titles, descriptions, file names and version notes; knowledge entry subjects,
titles, summaries and drafts; chat message
text and citation titles (only how many citations an answer carries).

"Active on a day" means at least one recorded signal from the "Active"
signals sources above (plus `activity_daily` and `priority_events`) on that
day in the business time zone.

### Phase 3 rules (AI Spend, Knowledge & Content, Quality & Trust)

- **AI spend.** The whole-company view (an all-departments viewer, no filter)
  counts every dollar. A department view and a person's page count only spend
  tied to a person in view, and their KPIs drop previous values and
  sparkline points from before attribution began (`tracked_from`). Features
  reported for the whole company only (keys starting `smart_teams` or `st_`)
  are never named and never split: their dollars are in the company total
  and the generic "Other features" line, and in no department, person or
  filtered figure. Feature labels are Admin → AI Cost's.
- **Knowledge.** Meeting transcripts can be recorded client calls, so they
  are counted per department only, never per person, and a department of
  fewer than 3 is hidden from every viewer (as for Call Capture).
- **Quality.** Ask Cubie questions and the AI Priorities numbers are read
  through those dashboards' modules. Loose Ends noise is dismissed ÷ flagged,
  as on the Loose Ends page; CRM Sync acceptance is approved ÷ (approved +
  dismissed), by the day decided.

### How the two new tables are written

**`activity_daily`**
- Written by a buffered middleware (`rubik_core/insights/activity.py`):
  - one upsert per (person, business-time-zone day, area);
  - a flush every 25 events or every 30 seconds.
  - Call `flush_activity()` before reading the table.
- The route-to-area map reads FastAPI's effective route path, which includes
  the include prefix. On FastAPI 0.139, `scope["route"].path` drops the
  prefix, so it is not used.
- **Not counted:**
  - health checks, `/users/me`, `/auth`, notifications, `/recent`,
    `/departments`;
  - login-time auto-ingests and scheduler, webhook and Slack callbacks;
  - pure status polls, such as the L10 live state and the priorities run poll;
  - **all `/api/smart-teams` traffic**;
  - anything outside `/api`.
- Unmapped `/api` routes count as area `other`.
- Requests made under person or department impersonation are skipped.

**`priority_events`**
- Written only after the state change commits, in the event's own session.
  Recording never fails the action.
- **Card-level numbers count distinct `(user_email, priority_id)`.** A card
  that covers several entities writes one row per entity, all with the same
  `priority_id`. Entity-wide transitions have a NULL `priority_id`; count
  those by `(user_email, entity_kind, entity_id)`.
- Kinds:
  - `auto_resolved`: evidence closed the card (actor `system`).
  - `resolved`: the person marked it done.
  - `dismissed` and `snoozed`: the person's own action.
  - `reopened`: a reopen by a person, or an expired snooze coming back
    (actor `system`).
  - `slack_followup`, `ticket_start`, `ticket_resolve`, `ticket_reassign`,
    `ticket_reply`: recorded after the action succeeds.
  - `delivered_slack`: one row per priorities DM delivered (actor `system`).
- "Acted on by the person" means `actor == user_email`. When a ticket action
  quiets someone else's card, the actor is whoever acted.

**`llm_usage_events.user_email`**
- Filled for every request-path LLM call on `core`, set by the async
  `bind_usage_user` dependency.
- Filled for every priorities run (the `attribute_to(..., user_arg=...)`
  decorator).
- AI spend by department is therefore complete from this release on, and
  partial before it.

## 6. Local demo

`scripts/seed_insights_demo.py` fills a **local SQLite** database with ~25
made-up people in Sales, Marketing, Engineering and Operations (plus one departed
person) and 60 days of rows in every table the dashboards read (Phase 2's
are appended by `_seed_phase2` and Phase 3's by `_seed_phase3`, each with its
own random stream), so every dashboard and the person page have something
realistic to show. It refuses to run
against anything but SQLite. `--reset` drops and recreates the tables first.

```
cd "$(git rev-parse --show-toplevel)" && \
DATABASE_URL=sqlite:////tmp/insights_demo.db CLOUDSQL_CONNECTION_NAME= \
  CONTROL_DATABASE_URL=sqlite:////tmp/insights_control.db CUBIE_WORKSPACE=demo \
  .venv/bin/python scripts/seed_insights_demo.py --reset
```

First provision an active `demo` workspace with a verified `company.com` domain
in the control database. Run the backend against the same tenant and control
databases. The demo admin is
`noel.vance@<domain>` (Engineering, admin role); `yael.hart@<domain>` is an
org-wide exec; `avery.stone@<domain>` is a Sales exec who manages Sales only.
The backend tests seed the same data (`backend/tests/test_insights_router.py`).
