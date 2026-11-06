UC-42 — Move the priorities batch to Cloud Run Jobs
======================================================

Status: SCOPING (this PR also implements)
Depends on: UC-40 / UC-40.1 / UC-40.2 / UC-40.3 — pipeline-side fixes
Blocks: UC-40 actually working end-to-end at scale

Problem
-------

The UC-40 fixes (filter, OAuth lock, partial-channel
results, per-user timeout) made the priorities pipeline run cleanly for
individual users. But the **deployment model** is still wrong:

- ``POST /api/jobs/priorities/run-all`` queues a FastAPI ``BackgroundTask``
  on a Cloud Run **Service** instance.
- The instance has ``cpu-throttling=true`` (CPU only allocated while
  handling a request) and ``minScale=0`` (scales to zero when idle).
- After the response is sent, the background task continues in-process,
  but Cloud Run sees the instance as "no active request" and starts the
  idle countdown. Within ~10-30 min the instance gets SIGTERM and any
  in-flight pipeline work is abandoned.

Today's live evidence (run_id ``dbf569a1``):

  - Orchestrator processed 6 users in 25 min wall-clock
  - 0 ``[uc40] user=...`` per-user completion logs from the wrapper
  - 0 ``persist_bundle`` activity in Cloud Logging
  - Instance died around 21:50 UTC mid-processing user 6

Cloud Run Services were never the right tool for this batch — they're
optimized for request/response, not for hour-long batch jobs. Cloud Run
**Jobs** is the right tool: each Job execution runs to completion in
its own instance with a dedicated CPU budget, supports up to 24 h
timeouts, and isn't subject to scale-to-zero in the middle of work.

This is also the architectural pattern OpenAI's Codex uses: one task =
one isolated execution environment that lives for the task's whole
lifetime.

Scope
-----

1. **New Cloud Run Job ``priorities-batch``.**
   - Built from the same container image as the ``cubie`` service
     (same dependencies, same code, same secrets, same VPC).
   - Runs a new entrypoint:
     ``python -m rubik_core.priorities.batch_runner``.
   - Container args: optional ``--target-email <email>`` to run for a
     single user; default is "all eligible users" (filtered by
     UC-40.1's domain + department rules).
   - Job-level config: ``--max-retries=1``, ``--task-timeout=3600``
     (1 hour), parallelism=1 (sequential users; tune later).

2. **``rubik_core/priorities/batch_runner.py``.**
   - Module entrypoint (``if __name__ == "__main__"``) that:
     - Parses ``--target-email``, ``--max-priorities`` flags.
     - Loads the user list using the existing ``_list_active_users``
       helper if no target.
     - For each user: runs ``PrioritiesOrchestrator().generate()``,
       calls ``persist_bundle()``, logs per-user outcome.
     - Aggregates the run summary in a single final log line.
   - **No FastAPI / no HTTP / no BackgroundTask** — pure synchronous
     Python that the Cloud Run Jobs container can run end-to-end.
   - Reuses ``_run_priorities_for_user`` and the per-user timeout
     wrapper from ``backend/routers/jobs.py`` so the logic stays
     identical to the in-process path.

3. **Endpoint rewrite.**
   - ``POST /api/jobs/priorities/run-all`` becomes a thin "kick off
     the Job execution" wrapper. It calls the Cloud Run Admin API
     (``run.googleapis.com/v2/projects/.../jobs/.../run``) and returns
     the execution name immediately.
   - The endpoint stays auth-gated by ``X-Scheduler-Key`` so Cloud
     Scheduler can trigger it.
   - Optional ``target_email`` body param to run for one user only —
     useful for ad-hoc admin re-runs of a specific user.

4. **Cloud Scheduler entry.**
   - The existing ``priorities-run-all`` job stays pointed at the HTTP
     endpoint (no change there). The endpoint internally launches the
     Cloud Run Job.
   - Alternatively, we can later move Cloud Scheduler to directly
     target the Job (Scheduler → Cloud Run Job is a supported native
     trigger). Out of scope for this PR — the indirect path works
     fine and is easier to debug.

5. **Tests.**
   - Unit test the new ``batch_runner`` ``main()``: argparse
     handling, single-user mode, all-users mode, per-user isolation.
   - Test the endpoint refactor: calls Cloud Run Admin API with the
     right body and returns the execution name.

Non-goals
---------

- Parallel user execution. Sequential is fine — even 30 users × 3 min
  is 90 min, well inside a 1-hour task timeout per execution.
  Parallelism is a UC-43+ tuning step.
- Streaming progress to the HTTP caller. The endpoint returns the
  execution name; the caller can poll the Cloud Run Admin API or
  Cloud Logging for progress.
- Replacing the legacy ``overnight-sales-agent`` Cloud Scheduler /
  ``/api/agents/overnight/run`` endpoint. Different pipeline,
  different concern.

Operational notes
-----------------

After merge + deploy:

  ```
  # 1. Create the Cloud Run Job (one-time)
  gcloud run jobs deploy priorities-batch \
    --image=us-central1-docker.pkg.dev/<project-id>/cloud-run-source-deploy/cubie:latest \
    --region=us-central1 \
    --project=<project-id> \
    --command="python" \
    --args="-m,rubik_core.priorities.batch_runner" \
    --task-timeout=3600 \
    --max-retries=1 \
    --parallelism=1 \
    --service-account=<runtime-sa>@<project-id>.iam.gserviceaccount.com \
    --set-cloudsql-instances=<project-id>:us-central1:<sql-instance> \
    --set-secrets=google-workspace-key=google-workspace-key:latest \
    --set-env-vars="APP_MODE=gcp,GCP_PROJECT_ID=<project-id>,CUBIE_WORKSPACE=<workspace-slug>,..."
  ```

  The workspace slug must resolve to an active control workspace with verified
  domains; the Job does not set a company domain by environment variable.
  (Full env-var list mirrors the Service's; can be scripted in a new
  ``scripts/deploy_priorities_job.sh``.)

  ```
  # 2. Verify
  gcloud run jobs execute priorities-batch --region=us-central1
  # then watch logs
  ```

  **IAM: overrides need more than `run.invoker`.** The trigger endpoint
  (``_launch_priorities_job_execution``) always passes a ``run_id`` for log
  correlation, so its ``:run`` call always sends
  ``overrides.containerOverrides`` — and executing *with* overrides requires
  ``run.jobs.runWithOverrides``, which ``roles/run.invoker`` does **not**
  grant. Grant ``roles/run.developer`` (superset, includes both
  ``run.jobs.run`` and ``run.jobs.runWithOverrides``) on the job to the
  service's runtime SA. `run.invoker` alone is only sufficient for an
  override-free ``gcloud run jobs execute`` with no flags.

Acceptance criteria
-------------------

- ``priorities-batch`` Cloud Run Job exists and accepts executions
- ``gcloud run jobs execute priorities-batch`` produces per-user
  ``[uc40] user=... succeeded=True`` logs AND HomeCard rows in the DB
  for all UC-40.1-eligible users
- ``POST /api/jobs/priorities/run-all`` returns a 200 with the
  execution name within 2 seconds
- Cloud Scheduler's existing entry continues to work without changes
- ``priorities_batch_runner`` test suite green
