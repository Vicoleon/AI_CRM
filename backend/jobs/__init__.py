"""Cloud Run Job entrypoints (invoked as ``python -m backend.jobs.<name>``).

Distinct from ``backend/routers/jobs.py`` (the HTTP triggers): modules here are
the code a Cloud Run Job task actually runs, on CPU-allocated tasks — the
durable alternative to FastAPI BackgroundTasks, which the CPU-throttled service
starves after the response.

:data:`WORKER_JOBS` is the registry of everything ``cubie-worker`` knows how to
run. It exists because "which module do I pass to ``--args``?" was previously
answerable only by grepping for ``launch_module_execution`` calls. A job nobody
can spell is a job nobody runs. :func:`worker_job_execute_command` turns an
entry into the exact ``gcloud`` line, so a runbook never hand-assembles the comma-joined
``--args`` string (a stray space there silently runs ``backend.jobs.worker``,
which exits 0 having done nothing).

:attr:`WorkerJob.dispatched_from` is the field that earns this file: it names
the code in THIS repo that launches the job, and an empty one means *nothing
does*. A job with no dispatcher is a feature that ships inert — it deploys, it
runs by hand, and in production it never fires. :func:`undispatched_worker_jobs`
is that list, so a deploy checklist can print it instead of someone remembering
to look.

The registry is documentation with a test attached, NOT a dispatcher: nothing
imports it to decide what to run. Cloud Run invokes the module directly, so an
entry going stale cannot break a running job — it can only mislead the next
person, which is what ``backend/tests/test_worker_jobs_registry.py`` pins.

NOTE ON SCOPE: ``deploy/schedules.yaml`` declares schedule intent; the platform
deploy reconciles it with GCP. This registry does not assert that a live cadence
exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from rubik_core.brand import PRODUCT_NAME

# The Cloud Run Job these all run on. Mirrors launcher.WORKER_JOB_NAME, which is
# the copy the request path uses; a test pins the two together rather than
# importing launcher here (it pulls in settings + FastAPI).
WORKER_JOB_NAME = "cubie-worker"

# The region cubie-worker runs in. The project is the deployment's own
# GCP_PROJECT_ID, read when a runbook command is printed: there is no default.
WORKER_JOB_REGION = "us-central1"

# Each workspace's wall-clock cadence is anchored to its validated control-row
# time zone; Task 14 expands the scheduler entry for every active workspace.
BUSINESS_CLOCK = "in the workspace business time zone"


@dataclass(frozen=True)
class WorkerJob:
    """One thing ``cubie-worker`` can be asked to do."""

    module: str
    # One line, present tense, in the terms an operator would use.
    summary: str
    # The file in THIS repo that launches it, or "" when nothing does. Empty
    # means the job only ever runs when a human types the command below.
    dispatched_from: str = ""
    # The cadence this job WANTS, when it wants one. Naming it does not create
    # it — Cloud Scheduler config lives in GCP, not in git.
    wants_cadence: str = ""
    # Args appended after ``-m <module>`` for the ordinary run.
    default_args: tuple[str, ...] = ()
    # A useful non-default invocation (a first backfill, a repair).
    example_args: tuple[str, ...] = ()

    @property
    def is_dispatched(self) -> bool:
        return bool(self.dispatched_from)


WORKER_JOBS: dict[str, WorkerJob] = {
    "overnight-briefing": WorkerJob(
        module="backend.jobs.overnight_briefing_job",
        summary="Build one workspace user's nightly briefing cards.",
        dispatched_from="backend/routers/jobs.py",
        example_args=("--target-email", "someone@example.com"),
    ),
    "overnight-agent": WorkerJob(
        module="backend.agents.overnight",
        summary="Run the workspace's overnight department agents.",
        dispatched_from="backend/routers/agents.py",
        wants_cadence="Nightly in the workspace business time zone",
    ),
    "ingestion-council": WorkerJob(
        module="backend.agents.ingestion_council",
        summary="Review workspace ingestion quality and deliver its verdict.",
        dispatched_from="backend/routers/jobs.py",
        default_args=("--scope", "nightly"),
    ),
    "transcript-sync": WorkerJob(
        module="rubik_core.scripts.sync_meet_transcripts",
        summary="Pull Google Meet transcripts and ingest them.",
        dispatched_from="backend/routers/ingest.py",
        default_args=("--days", "7"),
    ),
    "fathom-sync": WorkerJob(
        module="backend.jobs.fathom_sync_job",
        summary="Pull recent Fathom recordings and ingest their transcripts.",
        dispatched_from="backend/routers/fathom.py",
        example_args=("--user-email", "someone@example.com"),
    ),
    "l10-summary-email": WorkerJob(
        module="backend.jobs.l10_summary_email_job",
        summary="Send eligible L10 recaps for one workspace.",
        dispatched_from="backend/routers/agents.py",
        wants_cadence="Every 15 minutes",
        example_args=("--hours", "168", "--dry-run"),
    ),
    "dam-ingest": WorkerJob(
        module="backend.jobs.dam_ingest_job",
        summary="Ingest one uploaded knowledge-base document into RAG.",
        dispatched_from="backend/routers/dam.py",
        example_args=(
            "--doc-id",
            "<doc id>",
            "--actor-email",
            "someone@example.com",
        ),
    ),
    "cloud-embedding-rebuild": WorkerJob(
        module="backend.jobs.cloud_embedding_rebuild_job",
        summary="Build one workspace knowledge collection's client-owned vector index.",
        dispatched_from="backend/routers/ai_providers.py",
        example_args=("--run-id", "<rebuild id>"),
    ),
    "eos-reingest": WorkerJob(
        module="backend.jobs.eos_reingest_job",
        summary="Re-ingest one EOS source after a debounced edit burst.",
        dispatched_from="backend/jobs/eos_ingest_hooks.py",
        example_args=("--kind", "issues"),
    ),
    "eos-full-ingest": WorkerJob(
        module="backend.jobs.eos_full_ingest_job",
        summary="Refresh every EOS source in the workspace RAG corpus.",
        dispatched_from="backend/routers/agents.py",
        wants_cadence="Weekly in the workspace business time zone",
    ),
    "crm-sync": WorkerJob(
        module="backend.jobs.crm_sync_job",
        summary="Scan transcripts for proposed HubSpot writes (UC-46).",
        dispatched_from="backend/routers/crm_sync.py",
        example_args=("--all-users", "--since-hours", "25"),
    ),
    "deep-prep": WorkerJob(
        module="backend.jobs.deep_prep_job",
        summary="One Deep Prep run for one meeting.",
        dispatched_from="backend/routers/home.py",
        example_args=(
            "--run-id",
            "<run id>",
            "--user-email",
            "someone@example.com",
            "--event-id",
            "<calendar event id>",
        ),
    ),
    "meeting-prep": WorkerJob(
        module="backend.jobs.meeting_prep_job",
        summary="Build one saved Meeting Prep briefing.",
        dispatched_from="backend/routers/home.py",
        example_args=(
            "--run-id",
            "<run id>",
            "--user-email",
            "someone@example.com",
            "--event-id",
            "<calendar event id>",
        ),
    ),
    "meeting-routine-fire": WorkerJob(
        module="backend.jobs.meeting_routine_fire_job",
        summary="Execute one queued meeting routine fire.",
        dispatched_from="rubik_core/meeting_skills/routines.py",
        example_args=("--fire-id", "<fire id>"),
    ),
    "offboard-sweep": WorkerJob(
        module="backend.jobs.offboard_sweep_job",
        summary="Run one offboarding cleanup sweep.",
        dispatched_from="backend/routers/offboarding.py",
        example_args=("--record-id", "<offboarding record id>"),
    ),
    "xray-deep": WorkerJob(
        module="backend.jobs.xray_deep_job",
        summary="One UC-45 Customer X-Ray deep-research run.",
        dispatched_from="backend/routers/xray.py",
        example_args=(
            "--snapshot-id",
            "<snapshot id>",
            "--company-id",
            "<company id>",
            "--user-email",
            "someone@example.com",
        ),
    ),
    "priorities-on-demand": WorkerJob(
        module="backend.jobs.priorities_on_demand_job",
        summary="Regenerate one user's AI Priorities on request.",
        dispatched_from="backend/routers/home.py",
        example_args=(
            "--run-id",
            "<run id>",
            "--user-email",
            "someone@example.com",
        ),
    ),
    "smart-teams-score": WorkerJob(
        module="backend.jobs.smart_teams_score_job",
        summary=(
            "Score one completed Work Style Interview into a profile version "
            "(Smart Teams)."
        ),
        dispatched_from="backend/routers/smart_teams.py",
        # No default_args on purpose: there is no meaningful bare run. The
        # interview id is required, so a bare execution exits non-zero with a
        # usage message instead of quietly scoring the wrong thing (or, worse,
        # nothing at all while looking like a success).
        default_args=(),
        example_args=("--interview-id", "<st_interviews.id>"),
    ),
    "smart-teams-parse": WorkerJob(
        module="backend.jobs.smart_teams_parse_job",
        summary=(
            "Parse one uploaded official assessment report into a PROPOSAL for "
            "its owner to confirm (Smart Teams). Merges nothing."
        ),
        dispatched_from="backend/routers/smart_teams_uploads.py",
        # No default_args, same reason as smart-teams-score: the upload id is
        # required, so a bare execution exits non-zero with a usage message
        # rather than looking like a success that parsed nothing.
        default_args=(),
        example_args=("--upload-id", "<st_uploads.id>"),
    ),
    "smart-teams-guide": WorkerJob(
        module="backend.jobs.smart_teams_guide_job",
        summary=(
            "The three slow halves of one personal working guide (Smart Teams): "
            "--generate writes the prose, --ingest puts an APPROVED guide in the "
            "companywide corpus, --purge takes an unpublished one back out."
        ),
        dispatched_from="backend/routers/smart_teams_guides.py",
        # No default_args, same reason as the two above: the document id AND
        # exactly one of --generate/--ingest/--purge are both required, so a
        # bare execution exits non-zero with a usage message. A silent default
        # would be worse here than anywhere else in this table — guessing
        # --purge would retract a guide nobody asked to retract.
        default_args=(),
        example_args=("--document-id", "<st_documents.id>", "--generate"),
    ),
    "smart-teams-comparison": WorkerJob(
        module="backend.jobs.smart_teams_comparison_job",
        summary=(
            "Write one PAIR working-relationship guide (Smart Teams ST-7): the "
            "Gemini call that gives it its voice. --generate is the only action "
            "it has, because a pair guide is never approved, never published and "
            "never RAG-ingested."
        ),
        dispatched_from="backend/routers/smart_teams_comparison.py",
        # No default_args, same reason as the three Smart Teams jobs above: the
        # document id AND the action flag are both required, so a bare execution
        # exits non-zero with a usage message rather than acting on a document
        # nobody named.
        default_args=(),
        example_args=("--document-id", "<st_documents.id>", "--generate"),
    ),
    "stale-work-sweep": WorkerJob(
        module="backend.jobs.stale_work_sweep_job",
        summary=(
            "Nightly backstop: report work somebody asked for that never "
            "produced a result (Smart Teams scoring, Deep Prep delivery, Deep "
            "X-Ray runs). Reports only — it never mutates a stuck row."
        ),
        # DELIBERATELY EMPTY, and the empty string is load-bearing: nothing in this repo
        # dispatches this job. It runs only from the Cloud Scheduler entry
        # `stale-work-sweep-dev` (05:30 UTC daily), which is a GCP change. Until that
        # entry exists, all five probes are inert and nobody is ever told about stale
        # work — precisely the failure they were written to end, so this field must not
        # imply otherwise.
        dispatched_from="",
        # A bare run is the real run: every threshold comes from the feature
        # that owns it, so there is nothing to pass.
        default_args=(),
        example_args=("--limit", "50"),
    ),
    "l10-calendar-reconcile": WorkerJob(
        module="backend.jobs.l10_calendar_reconcile_job",
        summary=(
            "Nightly: ask Google Calendar whether every imported L10 still "
            "exists, and write the answer to the meeting row. Stops a cancelled "
            "L10 being promised forever by its stored weekly pattern."
        ),
        # DELIBERATELY EMPTY, and it STAYS empty now that the schedule exists:
        # this field names in-repo dispatchers, and the live trigger is the
        # Cloud Scheduler entry `l10-calendar-reconcile-dev` (05:00 UTC daily),
        # which lives in GCP where git cannot see it.
        # Same shape as `stale-work-sweep` below.
        #
        # What must NOT happen here: pointing `dispatched_from` at
        # GET /api/eos/l10/calendar-status. That endpoint reconciles the SAME
        # meetings, so it looks like a dispatcher — but only the ones a manager
        # opens the board for, which is the exact gap this job closes. Claiming
        # it would record the hole as plugged.
        dispatched_from="",
        # Nightly: a cancelled meeting needs to stop being promised before the
        # next morning's "your next L10" nudge and before the next PTO hand-off
        # brief is written, and neither is faster than daily. Off-peak because
        # it is one Google Calendar round-trip per imported meeting and belongs
        # clear of the priorities generation.
        wants_cadence="nightly, off-peak",
        # A bare run is the real run: the cap and the look-ahead window both
        # come from the module, so there is nothing to pass.
        default_args=(),
        # The FIRST run against real data wants --dry-run. The sweep's whole
        # purpose is to mark meetings cancelled; seeing WHICH ones it would
        # touch before it writes costs one execution and nothing else.
        example_args=("--dry-run",),
    ),
    "smart-teams-skill-sweep": WorkerJob(
        module="backend.jobs.smart_teams_skill_sweep_job",
        summary=(
            "Read internal signals (meetings, documents) and SUGGEST "
            "curated skills for people to confirm or deny (Smart Teams). "
            "Suggests nothing anybody already denied."
        ),
        # POST /api/smart-teams/skills/refresh dispatches this for ONE person,
        # so the module is exercised by the app. The company-wide nightly pass
        # is NOT dispatched from this repo — it needs the Cloud Scheduler entry
        # named below, which lives in GCP. Both facts are true at once and the
        # second one is the one that matters operationally: without that
        # schedule, nobody gets a suggestion unless they press the button.
        dispatched_from="backend/routers/smart_teams_skills.py",
        # Nightly, off-peak. Nightly because evidence accrues at the speed of
        # meetings and a suggestion is not time-critical; off-peak
        # because it is one Vertex call per person and belongs clear of the
        # priorities generation, which is ~98.7% of this app's AI spend.
        wants_cadence="nightly, off-peak",
        # No args: sweep the roster from the stored watermark. Safe as a bare
        # run — unlike the other three Smart Teams jobs, which require an id —
        # because the watermark, the per-person evidence digest and the run lock
        # between them make a repeat run cheap instead of destructive.
        default_args=(),
        # The FIRST run wants both: --since because the watermark starts at a
        # 90-day lookback, and --limit because an uncapped first pass is one
        # Vertex call per person over the whole roster in one execution.
        example_args=("--since", "2026-01-01", "--limit", "10"),
    ),
    "graph-backfill": WorkerJob(
        module="backend.jobs.graph_backfill_job",
        summary=(
            "Fill the relationship graph from transcripts that predate it "
            "(attendance edges from meeting_participants)."
        ),
        # DELIBERATELY EMPTY. This is a one-shot catch-up, not a recurring job:
        # once it has run, the live writer on upsert_participants keeps the
        # graph current, so nothing in the app should be dispatching it. Run it
        # by hand after the first deploy that carries the graph tables — until
        # then the graph only knows meetings ingested since that deploy.
        dispatched_from="",
        # Idempotent (upserts, not inserts), so a repeat run is a no-op rather
        # than a duplication — safe to re-run if one is interrupted.
        default_args=(),
        example_args=("--limit", "500", "--batch-size", "50"),
    ),
    "graph-hubspot-sweep": WorkerJob(
        module="backend.jobs.graph_hubspot_sweep_job",
        summary=(
            "Write the relationship graph's CRM edges (contacts, deals, deal "
            "owners) for the companies we meet."
        ),
        # DELIBERATELY EMPTY, and this one is a standing gap rather than a one-off.
        # These edges were expected to accumulate from X-Ray runs, but X-Ray runs too
        # rarely for that. Nothing dispatches this yet, so CRM edges go stale between
        # manual runs — a nightly schedule is the real fix.
        dispatched_from="",
        # Deals and contacts change daily; attendance does not. Nightly is
        # enough, and off-peak keeps it clear of the priorities generation.
        wants_cadence="nightly, off-peak",
        # No args: sweep every external company the graph already knows.
        default_args=(),
        # Rehearse on a couple of known domains before the full sweep.
        example_args=("--domains", "client-one.example,client-two.example"),
    ),
    # ── Call capture + sales cockpit ──────────────────────────────────────────
    # Seven sweeps, one module, one subcommand each. The subcommand names are the
    # keys below, so `worker_job_execute_command("ccs-sweep")` and
    # `python -m backend.jobs.sales_kaizen ccs-sweep` say the same word.
    #
    # `dispatched_from` means "something in THIS repo launches it as a Cloud Run
    # Job". Only `ccs-sweep` does. The other six are NOT inert: their live path
    # is a Cloud Scheduler POST to `backend/routers/agents.py::scheduler_router`,
    # which runs them synchronously in a threadpool (bounded work, well under
    # Cloud Run's 300s cap — see backend/jobs/sales_kaizen.py's table). They are
    # registered here because a hand-run repair after an outage, and a
    # `--dry-run` review before flipping a flag, both need the exact command —
    # and because the module they share is Job-dispatched, so it has to be in
    # this table regardless.
    #
    # ⚠️ Cloud Scheduler config lives in GCP, not git. `wants_cadence` below is a
    # statement of INTENT: each of the seven needs its own
    # `gcloud scheduler jobs create http` line, and until a human runs them,
    # none of this fires.
    "ccs-sweep": WorkerJob(
        module="backend.jobs.sales_kaizen",
        summary=(
            "Poll every AE's Google Meet conference records: ingest new "
            "transcripts, run the extractor, and mark silent calls "
            "no_transcript. The PRIMARY call-capture ingest path."
        ),
        # POST /api/agents/jobs/ccs/sweep dispatches this for every live run;
        # only ?dry_run=true answers inline. It is a Job because one pass can
        # make 5 Gemini Pro calls per AE plus Vertex RAG imports that serialize
        # per corpus, which exceeds a 300s request on a busy morning.
        dispatched_from="backend/routers/agents.py",
        # Matches the existing Meet transcript sync. 🔴 Google deletes conference
        # records, transcripts and transcript entries 30 days after the
        # conference ends, so a permanently missed window is a permanently lost
        # call — and CCS_POLL_LOOKBACK_HOURS defaults to 24 against this cadence
        # precisely so a scheduler outage is survivable. Overlap is free: ingest
        # is idempotent on (call_session_id, source, external_id).
        wants_cadence="every 15 minutes",
        default_args=("ccs-sweep",),
        # A catch-up after an outage. Widen the window, never narrow it.
        example_args=("ccs-sweep", "--lookback-hours", "72"),
    ),
    "cockpit-recompute": WorkerJob(
        module="backend.jobs.sales_kaizen",
        summary=(
            "Age every live commitment one step along the escalation ladder "
            "(open -> at_risk -> breached -> escalated -> exec_visible)."
        ),
        # Lives on the scheduler endpoint: one query plus in-memory transitions,
        # no external calls at all.
        dispatched_from="",
        wants_cadence="every 15 minutes",
        default_args=("cockpit-recompute",),
        # Idempotent — transitions only move forward, so a dry run and a real
        # run differ only in whether the transaction is committed.
        example_args=("cockpit-recompute", "--dry-run"),
    ),
    "cockpit-stage-lag": WorkerJob(
        module="backend.jobs.sales_kaizen",
        summary=(
            f"Flag deals whose HubSpot stage is behind what {PRODUCT_NAME} heard on the "
            f"call. Reads only the deals {PRODUCT_NAME} already holds evidence for."
        ),
        dispatched_from="",
        wants_cadence="hourly",
        default_args=("cockpit-stage-lag",),
        # --limit caps HubSpot reads; the remainder reports as deals_deferred
        # and is picked up next run, so a capped run skips nothing.
        example_args=("cockpit-stage-lag", "--limit", "25"),
    ),
    "cockpit-sow-sent": WorkerJob(
        module="backend.jobs.sales_kaizen",
        summary=(
            "Scan the Sent mail of AEs holding a live SOW commitment for "
            "evidence it went out, and SUGGEST a close. Never closes anything."
        ),
        dispatched_from="",
        # ⚠️ Intent only, and hold off on creating this one: the detector returns
        # suggestions and nothing stores them (`commitments` has no column for
        # one), so on a schedule this spends Gmail quota to write a log line. The
        # durable path is the cockpit reading the detector live for the row it
        # renders. Verify the lane by hand meanwhile.
        wants_cadence="hourly (blocked: nothing persists a suggestion yet)",
        default_args=("cockpit-sow-sent",),
        # --dry-run reports WHICH AEs hold a live SOW commitment and reads no
        # mailbox at all — the right first command, since the whole point of the
        # detector's ordering is that a mailbox read needs a reason that exists
        # before the read.
        example_args=("cockpit-sow-sent", "--dry-run"),
    ),
    "cockpit-daily-digest": WorkerJob(
        module="backend.jobs.sales_kaizen",
        summary=(
            "One daily Slack digest per person, capped at three items, "
            "enforced one-per-kind-per-local-day by digest_logs."
        ),
        # Runs on the SERVICE today, via the scheduler endpoint — that is the
        # path exercised in production. (An earlier note here claimed a Job
        # physically could not DM anyone for want of a SLACK_BOT_TOKEN; that
        # was wrong. Nothing reads such an env var: the bot token comes from
        # Cloud SQL, which cubie-worker has.) --dry-run renders without
        # sending, which is the review path for COCKPIT_DIGEST_ENABLED.
        dispatched_from="",
        wants_cadence=f"weekdays 08:00 {BUSINESS_CLOCK}",
        default_args=("cockpit-daily-digest",),
        example_args=("cockpit-daily-digest", "--dry-run"),
    ),
    "close-date-rollup": WorkerJob(
        module="backend.jobs.close_date_rollup",
        summary=(
            "One weekly Slack DM to named recipients: the five open deals "
            "whose estimated close date is least believable given how long "
            f"the client has been silent. Read-only — no buttons, no {PRODUCT_NAME} "
            "link, because a manager acting on it would silence the rep who "
            "owns the deal."
        ),
        dispatched_from="",
        # ⚠️ Cloud Scheduler config lives in GCP, not git (see the note above
        # this registry). Weekly, and deliberately NOT a weekday check in
        # code: a Monday-only slot is a scheduler concern, and a code check
        # cannot express the offset needed to stay clear of the other Monday
        # sweeps sharing the Slack rate limit.
        wants_cadence="Mondays, after the morning priorities DM",
        default_args=("close-date-rollup",),
        example_args=("close-date-rollup", "--dry-run"),
    ),
    "scheduled-task-run": WorkerJob(
        module="backend.jobs.scheduled_task_run_job",
        summary=(
            f"Run ONE user-scheduled {PRODUCT_NAME} task and deliver the result to their "
            "chat, Slack DM and (if asked) email."
        ),
        # The launch itself is in the dispatcher, which the scheduler tick
        # (backend/routers/agents.py::scheduled_tasks_tick) and the "run now"
        # endpoint both call.
        dispatched_from="rubik_core/scheduled_tasks/dispatch.py",
        # The TICK wants a cadence, not this job: one execution is one due
        # task, launched by the tick that found it.
        wants_cadence="",
        example_args=("--run-id", "<scheduled_task_runs.id>"),
    ),
    "cockpit-weekly-aging": WorkerJob(
        module="backend.jobs.sales_kaizen",
        summary=(
            "The weekly aging roll-up, kind='weekly' — a separate digest_logs "
            "key from the daily one, which is why Monday can carry both."
        ),
        # Same delivery path as the daily digest (see the note above).
        dispatched_from="",
        wants_cadence=f"Mondays 08:10 {BUSINESS_CLOCK}",
        default_args=("cockpit-weekly-aging",),
        example_args=("cockpit-weekly-aging", "--dry-run"),
    ),
    # ── Loose Ends (docs/loose_ends/README.md) ────────────────────────────────
    "loop-closure-scan": WorkerJob(
        module="backend.jobs.loop_closure_scan_job",
        summary=(
            "The AE loop-closure scan: read each rostered AE's Gmail and Slack, "
            "reconcile against HubSpot, flag and re-check their loose ends, then "
            "post the one daily digest. Never writes HubSpot."
        ),
        # POST /api/agents/jobs/loop-closure/scan dispatches every live run,
        # passing the Service's LOOP_CLOSURE_* values as env; only a one-AE
        # ?dry_run=true answers inline. ⚠️ The Cloud Scheduler entry that calls
        # that endpoint lives in GCP — the exact `gcloud scheduler jobs create
        # http loop-closure-scan` line is in the README's runbook, and until a
        # human runs it nothing fires.
        dispatched_from="backend/routers/agents.py",
        # After the 06:00 UTC priorities batch (the promises it reads are
        # fresh) and clear of the :00/:15 transcript sync.
        wants_cadence=f"weekdays 07:10 {BUSINESS_CLOCK}",
        # A bare run is the real run: the roster comes from Feature Access and
        # every threshold from settings.
        default_args=(),
        # The FIRST run against real mailboxes wants --dry-run: it reports what
        # would be flagged and closed and rolls everything back, digest included.
        example_args=("--dry-run",),
    ),
}


def worker_job_args(name: str, *extra: str) -> list[str]:
    """The container-arg list for ``name``: ``["-m", module, *args]``.

    ``extra`` REPLACES ``default_args`` rather than appending to them — an
    override is a different invocation, not a bigger one, and silently keeping
    a default the caller did not ask for is how a "just this one" run turns
    into a full sweep.
    """
    job = WORKER_JOBS[name]
    args = tuple(extra) if extra else job.default_args
    return ["-m", job.module, *args]


def worker_job_execute_command(
    name: str,
    *extra: str,
    workspace: str,
    project: str | None = None,
    region: str = WORKER_JOB_REGION,
) -> str:
    """The exact one-liner to run ``name`` once, right now.

    Copy-pasteable from any working directory — the whole point of not printing
    a bare ``gcloud run jobs execute cubie-worker``, which runs
    ``backend.jobs.worker`` (a no-op usage hint) and looks like a success.
    ``--args`` is comma-joined with NO spaces: gcloud splits that string on
    commas itself, and a stray space becomes part of the argument, so the
    module is not found and the task exits non-zero with an import error.

    ``workspace`` is the registered workspace to run against and is included
    in the one-off execution override. ``project`` defaults to the configured
    ``GCP_PROJECT_ID``; with neither,
    :class:`rubik_core.config.GcpProjectNotSet` is raised rather than printing
    a command aimed at some other deployment's project.
    """
    from rubik_core.tenancy.context import _canonical_slug

    _canonical_slug(workspace)
    if not project:
        from rubik_core.config import gcp_project_id  # local: keep leaf light

        project = gcp_project_id()
    joined = ",".join(worker_job_args(name, *extra))
    return (
        f"gcloud run jobs execute {WORKER_JOB_NAME} "
        f"--project={project} --region={region} "
        f'--args="{joined}" '
        f'--update-env-vars="CUBIE_WORKSPACE={workspace}"'
    )


def undispatched_worker_jobs() -> list[str]:
    """Registry names that nothing in this repo launches.

    Each one runs only when a human types :func:`worker_job_execute_command`.
    For a job that wants a cadence, that makes it inert in production.
    """
    return sorted(name for name, job in WORKER_JOBS.items() if not job.is_dispatched)
