UC-30 — Scheduled jobs framework
================================

Status: SUPERSEDED (2026-09-08) for per-user schedules — never implemented as
        specified. What shipped instead is **user-authored** scheduled tasks:
        `rubik_core/scheduled_tasks/` + `backend/routers/scheduled_tasks.py`,
        created by asking in Chat, one Cloud Scheduler tick claiming due rows,
        one `cubie-worker` execution per run. It departs from this document on
        the two decisions below; the rest (scheduler auth, the Job-not-Service
        rule) it kept.
          - OWNERSHIP: this doc says "per-user cron customization — out of
            scope. Admins manage the master schedule." The shipped feature is
            the inverse: each user owns their own schedules, and admins control
            only WHO may schedule, from Admin → Feature Access.
          - SCHEDULE SHAPE: no `cron_expr`. A structured schedule (kind + local
            time + day selector) is validatable when the model proposes it and
            can be read back in plain English for the user to confirm.
        An admin-owned, fan-out-across-users job framework (the LinkedIn
        workflow problem below) is still unbuilt and this remains its spec.
Depends on: UC-20 (priorities orchestrator), UC-23 (playbooks)
Blocks:    UC-32, UC-33, UC-34, UC-35, UC-36

Problem
-------

The UC-20 → UC-25 arc made the AI Priorities pipeline reactive: it
fires when a user opens the homefeed. The LinkedIn workflows we want
to replicate (daily call prep, stale-deal alert, deal risk audit,
discovery pattern review, prospect finder) all run on a cadence and
deliver to a channel the user doesn't have to refresh. The current
"overnight-briefing" Cloud Scheduler hooks straight into `OvernightAgent`
— there's no generalized "run job X for user Y on schedule Z and
deliver to channel W" abstraction.

Scope
-----

A small framework for scheduled jobs that reuses the UC-20 orchestrator
+ UC-23 playbooks as building blocks and delegates delivery to UC-31.

Concretely:

1. New DB table ``scheduled_job(id, name, cron_expr, target_user_emails[],
   playbook_id NULL, priorities_filter JSON, delivery JSON, enabled,
   created_by, updated_by, created_at, updated_at)``.
2. New endpoint ``POST /api/jobs/run/{job_name}`` — Cloud Scheduler
   target. Verifies caller (Cloud Scheduler service account OR admin
   token), looks up the row, fans out across ``target_user_emails``,
   runs the appropriate pipeline (priorities or a per-job custom),
   pipes output through UC-31 delivery.
3. Cloud Scheduler config-as-code in ``infra/scheduler/`` — yaml file
   per job, terraform-free, applied via a small ``scripts/sync_schedulers.sh``.
4. Admin UI section under **Admin → AI Priorities → Jobs**: list of
   jobs, enable/disable, edit cron + target users + delivery channel +
   playbook attachment.

Out of scope
------------

- Reactive (event-driven) jobs. This is for scheduled only.
- Per-user cron customization. Admins manage the master schedule.

Data model
----------

::

    CREATE TABLE scheduled_job (
        id              TEXT PRIMARY KEY,            -- uuid
        name            TEXT UNIQUE NOT NULL,        -- e.g. "daily_call_prep"
        description     TEXT,
        cron_expr       TEXT NOT NULL,               -- crontab-style
        target_user_emails JSON NOT NULL,            -- list[str]; "*" → all active users
        playbook_id     TEXT REFERENCES priority_playbook(id),
        priorities_filter JSON,                      -- {kinds: ["follow_up_debt", "neglect_debt"], …}
        delivery        JSON NOT NULL,               -- {channel: "slack_dm" | "homefeed_card", …}
        enabled         BOOLEAN NOT NULL DEFAULT TRUE,
        last_run_at     TIMESTAMP,
        last_run_status TEXT,                        -- 'success' | 'partial' | 'failed'
        last_run_audit  JSON,                        -- per-user breadcrumb
        created_by      TEXT NOT NULL,
        updated_by      TEXT NOT NULL,
        created_at      TIMESTAMP NOT NULL,
        updated_at      TIMESTAMP NOT NULL
    );

API
---

``POST /api/jobs/run/{job_name}``
  Cloud Scheduler target. Authenticated via the ``X-CloudScheduler: true``
  header (same trick the overnight endpoint uses today) OR an
  admin-required path for manual runs. Fans out across users,
  catches per-user failures, records audit + last_run_at, returns
  a per-user run summary.

``GET /api/admin/jobs``
  List jobs (admin-only).

``GET /api/admin/jobs/{id}``
  Full row.

``POST /api/admin/jobs``
  Create.

``PUT /api/admin/jobs/{id}``
  Partial update.

``DELETE /api/admin/jobs/{id}``
  Hard delete.

``POST /api/admin/jobs/{id}/run-now``
  Manual fire (mostly for testing).

Test plan
---------

- CRUD admin-only; non-admin 403.
- ``run/{job_name}`` resolves the job + fans out + collects per-user
  results.
- ``target_user_emails = ["*"]`` expands to all active users.
- Per-user crash is captured in ``last_run_audit`` but the job
  marks ``last_run_status = "partial"``, not "failed".
- A disabled job returns 404 from ``run/{job_name}``.

Migration / rollout
-------------------

1. Alembic migration creating ``scheduled_job``.
2. Job-run endpoint behind feature flag ``JOBS_FRAMEWORK_ENABLED``.
3. Wire UC-32 through UC-36 to this framework one at a time.

Open questions
--------------

- Should ``last_run_audit`` keep history beyond the latest run?
  Recommend: only latest in this table; if we want history, add a
  ``scheduled_job_run`` event-log table later.

Estimate
--------

Medium. ~4-6h including the admin UI section.
