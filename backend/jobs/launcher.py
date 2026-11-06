"""Trigger-side launcher for Cloud Run Job executions (#9 background-job hardening).

The web service must never run long background work in-process: Cloud Run
throttles the Service's CPU to near-zero the instant the HTTP response
returns, so FastAPI ``BackgroundTasks`` / ``asyncio.create_task`` work is
silently starved and may never finish. Durable work belongs on a Cloud Run
*Job* task, which runs CPU-allocated to completion.

Jobs in play (each reuses the cubie Service image — see scripts/):

- ``cubie-priorities-batch`` — the UC-42 priorities job (deploy_priorities_job.sh).
- ``cubie-deep-prep``        — per-request Deep Prep runs (deploy_deep_prep_job.sh).
- ``cubie-worker``           — general-purpose single-task worker for everything
  else (deploy_worker_job.sh): transcript sync, Fathom pulls, DAM ingest,
  debounced EOS re-ingest (backend/jobs/eos_ingest_hooks.py).

``submit_background_work`` is the standard entry point for endpoint-triggered
fire-and-forget work: it launches ``python -m <module> <args>`` as one Job
execution and, if the launch fails (job not deployed yet / IAM / local dev),
falls back to the legacy in-process BackgroundTask so the feature keeps
working — loudly, because the fallback is the throttling-prone path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy

from fastapi import BackgroundTasks, HTTPException

from rubik_core.automation import (
    AUTOMATIONS_PAUSED_MESSAGE,
    AutomationsPausedError,
    automations_enabled,
    require_automations_enabled,
)
from rubik_core.config import AppMode, GcpProjectNotSet, gcp_project_id, settings
from rubik_core.tenancy.context import (
    NoWorkspaceContext,
    current_workspace,
    use_workspace,
)
from rubik_core.tenancy.support_context import current_support_session

logger = logging.getLogger(__name__)

PRIORITIES_JOB_NAME = "cubie-priorities-batch"
WORKER_JOB_NAME = "cubie-worker"


class JobLaunchError(RuntimeError):
    """A Cloud Run Job execution could not be launched."""


def require_automations_http() -> None:
    """Refuse paused work before pending rows, dispatch or inline fallbacks."""
    if not automations_enabled():
        raise HTTPException(status_code=503, detail=AUTOMATIONS_PAUSED_MESSAGE)


def _with_workspace_override(body: dict) -> dict:
    """Copy a Job run body and pin its container to the selected workspace."""
    try:
        slug = current_workspace()
    except NoWorkspaceContext as exc:
        raise JobLaunchError("A workspace is required to launch a Job") from exc
    if not isinstance(body, dict):
        raise JobLaunchError("A Job run body must be an object")
    selected = deepcopy(body)
    overrides = selected.setdefault("overrides", {})
    if not isinstance(overrides, dict):
        raise JobLaunchError("Job overrides must be an object")
    containers = overrides.setdefault("containerOverrides", [{}])
    if not isinstance(containers, list) or len(containers) != 1:
        raise JobLaunchError("A Job must have exactly one container override")
    container = containers[0]
    if not isinstance(container, dict):
        raise JobLaunchError("A Job container override must be an object")
    env = container.setdefault("env", [])
    if not isinstance(env, list) or any(not isinstance(item, dict) for item in env):
        raise JobLaunchError("Job environment overrides must be a list")
    existing = [item for item in env if item.get("name") == "CUBIE_WORKSPACE"]
    if existing:
        if len(existing) != 1 or existing[0].get("value") != slug:
            raise JobLaunchError("Conflicting Job workspace override")
    else:
        env.append({"name": "CUBIE_WORKSPACE", "value": slug})
    return selected


def reject_support_background_work() -> None:
    """Keep support requests from launching work that loses their auth scope.

    A Cloud Run Job gets a fresh process and a detached task may run after the
    request-local support grant is gone. Both could then use customer Google
    delegation. Call this before any pending row or fallback is scheduled.
    """
    if current_support_session() is not None:
        raise HTTPException(
            status_code=403,
            detail="Platform support sessions cannot start background work.",
        )


def run_job_request(job_name: str, body: dict) -> dict:
    """POST a Cloud Run Admin v2 ``jobs.run`` request; return the LRO payload.

    Authenticates as the service's RUNTIME service account via the metadata
    server — NOT ``google.auth.default()``. ``rubik_core.config`` once
    exported ``GOOGLE_APPLICATION_CREDENTIALS`` pointing at the Workspace
    domain-wide-delegation key, which silently repointed ADC for every
    ``google.auth.default()`` caller in this process and made job launches
    fail with 403. The global export is gone (ADC-hygiene note
    in ``rubik_core/config.py``), but the metadata-server path stays as an
    explicit belt-and-braces: it can never be repointed by env vars, and the
    IAM grants for launching jobs live on the runtime SA.

    Raises :class:`JobLaunchError` on any failure so callers outside the
    router layer aren't forced to depend on FastAPI's ``HTTPException``.
    """
    reject_support_background_work()
    require_automations_enabled()
    body = _with_workspace_override(body)

    import google.auth
    import google.auth.compute_engine
    import google.auth.transport.requests
    import requests

    try:
        project = gcp_project_id()
    except GcpProjectNotSet as exc:
        raise JobLaunchError(
            f"Cannot launch Cloud Run Job {job_name!r}: {exc}"
        ) from exc
    region = getattr(settings, "GCP_REGION", None) or "us-central1"
    url = (
        f"https://run.googleapis.com/v2/projects/{project}/locations/{region}"
        f"/jobs/{job_name}:run"
    )

    auth_request = google.auth.transport.requests.Request()
    try:
        creds = google.auth.compute_engine.Credentials()
        creds.refresh(auth_request)
    except Exception as exc:  # noqa: BLE001 — no metadata server (local dev)
        logger.warning(
            "[jobs] metadata-server credentials unavailable (%s); falling back "
            "to google.auth.default() — fine locally, but on Cloud Run this "
            "can resolve to the wrong service account",
            exc,
        )
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        creds.refresh(auth_request)

    response = requests.post(
        url,
        json=body,
        headers={
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        logger.error(
            "[jobs] Cloud Run Jobs run-trigger failed: job=%s status=%s body=%s",
            job_name,
            response.status_code,
            response.text[:500],
        )
        raise JobLaunchError(
            f"Failed to launch Cloud Run Job {job_name!r} "
            f"({response.status_code}): {response.text[:200]}"
        )
    return response.json()


def launch_module_execution(
    module: str,
    args: list[str],
    *,
    job_name: str = WORKER_JOB_NAME,
    task_count: int | None = 1,
    env: dict[str, str] | None = None,
) -> dict:
    """Run ``python -m <module> <args>`` as one execution of ``job_name``.

    ``containerOverrides.args`` REPLACES the job's configured args entirely,
    so the ``-m <module>`` prefix must be re-supplied here (the job's command
    stays ``python``). ``task_count=1`` pins the execution to a single task —
    load-bearing when targeting ``cubie-priorities-batch``, which is deployed with
    ``--tasks=4`` for the sharded daily batch: without the override a
    single-user run would boot four tasks for one user's work.

    Sending overrides requires ``run.jobs.runWithOverrides``
    (``roles/run.developer``) on the runtime SA — already granted for the
    UC-42 single-user path, which uses the same mechanism.

    ``env`` sets those env vars for this execution only (Cloud Run's
    ``containerOverrides.env``, ``[{name, value}]``); the job's other env stays
    as deployed. It is how the Service hands its CURRENT value of a switch to a
    Job deployed with its own env (the Deep Prep launcher does the same).
    """
    reject_support_background_work()
    require_automations_enabled()
    container: dict = {"args": ["-m", module, *args]}
    if env:
        container["env"] = [
            {"name": str(name), "value": str(value)} for name, value in env.items()
        ]
    overrides: dict = {"containerOverrides": [container]}
    if task_count is not None:
        overrides["taskCount"] = task_count
    return run_job_request(job_name, {"overrides": overrides})


def submit_background_work(
    background_tasks: BackgroundTasks,
    *,
    module: str,
    args: list[str],
    fallback: Callable[[], None],
    label: str,
    job_name: str = WORKER_JOB_NAME,
    env: dict[str, str] | None = None,
) -> tuple[str, str | None]:
    """Dispatch fire-and-forget work durably; degrade to a BackgroundTask.

    Returns ``(executor, execution_name)`` where executor is ``"job"`` (a
    Cloud Run Job execution was launched — CPU-allocated, survives instance
    recycling) or ``"inline"`` (the zero-arg ``fallback`` was scheduled on
    FastAPI ``BackgroundTasks`` — correct off-GCP, throttling-prone on the
    deployed service, hence the loud log).

    ``env`` goes to the Job execution only (see :func:`launch_module_execution`);
    the inline fallback runs in this process, which already has the Service's
    values.
    """
    reject_support_background_work()
    # Deliberately outside the launch try/except: a policy hold must never
    # degrade to the in-process fallback.
    require_automations_http()
    try:
        slug = current_workspace()
    except NoWorkspaceContext as exc:
        raise JobLaunchError(
            "A workspace is required to schedule background work"
        ) from exc
    if env and env.get("CUBIE_WORKSPACE", slug) != slug:
        raise JobLaunchError("Conflicting background-work workspace override")
    if settings.APP_MODE == AppMode.GCP:
        try:
            execution = launch_module_execution(
                module, args, job_name=job_name, env=env
            )
            execution_name = execution.get("name") or execution.get("metadata", {}).get(
                "name"
            )
            logger.info(
                "[jobs] %s dispatched to Cloud Run Job %s (execution=%s)",
                label,
                job_name,
                execution_name,
            )
            return "job", execution_name
        except AutomationsPausedError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception:  # noqa: BLE001 — degrade, never break the endpoint
            logger.exception(
                "[jobs] %s: Cloud Run Job launch failed (job=%s) — falling "
                "back to an in-process BackgroundTask, which Cloud Run "
                "CPU-throttles after the response and may starve. If the job "
                "does not exist yet, restore it through the guarded platform deploy",
                label,
                job_name,
            )

    def scoped_fallback() -> None:
        require_automations_enabled()
        with use_workspace(slug):
            fallback()

    background_tasks.add_task(scoped_fallback)
    return "inline", None
