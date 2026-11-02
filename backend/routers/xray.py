"""UC-45 Customer X-Ray HTTP API.

Every endpoint is gated to Sales-or-admin via ``require_sales_or_admin`` at the
router level; the router is ALSO mounted under the pilot-gated ``core`` parent
in ``backend/main.py`` (belt-and-suspenders — the sales gate is the effective
rule).

Connection discipline (docs/ENGINEERING_NOTES.md): handlers do NOT declare ``Depends(get_db)``
for the long paths. The heavy fetch/LLM work runs via ``run_in_threadpool`` and
the service layer opens its own short sessions — so no request holds a DB
connection idle across model work. Short read/serialize blocks use explicit
``SessionLocal()`` context managers.

Deep runs: ``POST …/deep`` creates the pending row (409 if one is active),
dispatches ``service.run_deep_job`` to a small module-level thread pool, and
returns 202 immediately; the frontend polls ``GET …/runs/{id}``.

⚠️ HONEST CAVEAT (spec §6): this run is DETACHED — the POST
does not stay open. On Cloud Run, detached threads are CPU-throttled once no
request is active (the Ingestion-Council/overnight-job failure mode), so a deep
run progresses only in the brief windows the 5s polls create and may be slow or
stall under sparse traffic. For reliable prod deep runs, deploy the service with
``--no-cpu-throttling`` OR move ``run_deep_job`` to a Cloud Run Job (it is
already job-callable). ``persistence.active_deep_run`` uses a TTL so a stalled
run never permanently 409-blocks future runs, and the frontend caps polling.
Fast X-Ray is unaffected (it runs in-request via run_in_threadpool).

Both run endpoints also stamp the company's HubSpot record with a link back to
its X-Ray page (``_stamp_crm_link`` → ``rubik_core/xray/crm_link.py``). That
call belongs HERE rather than in the service layer: ``HUBSPOT_WRITE_TOKEN`` is
mounted on the ``cubie`` Service and deliberately not on the ``cubie-xray-deep``
Job, so the write has to happen inside the request, not at the end of the run.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user, require_sales_or_admin
from backend.jobs.launcher import reject_support_background_work
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import XraySnapshot, XrayTimelineEvent
from rubik_core.tenancy.threads import ContextThreadPoolExecutor
from rubik_core.xray import config, crm_link, fetchers, persistence, service
from rubik_core.xray.text import plain_text

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/xray",
    tags=["xray"],
    dependencies=[Depends(require_sales_or_admin)],
)

# Small pool for out-of-request deep runs. See the module docstring for the
# Cloud Run CPU-throttling caveat + the Cloud Run Job escape hatch.
_DEEP_POOL = ContextThreadPoolExecutor(
    max_workers=config.DEEP_RUNNER_MAX_WORKERS,
    thread_name_prefix="xray-deep",
)


class XrayRunRequest(BaseModel):
    focus: str | None = None
    company_name: str | None = None
    company_domain: str | None = None


# ── Serialization ─────────────────────────────────────────────────────────────
def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _serialize_snapshot(snap: XraySnapshot) -> dict[str, Any]:
    return {
        "id": snap.id,
        "company_id": snap.company_id,
        "company_name": snap.company_name,
        "company_domain": snap.company_domain,
        "version": snap.version,
        "base_snapshot_id": snap.base_snapshot_id,
        "kind": snap.kind,
        "status": snap.status,
        "error": snap.error,
        "focus": snap.focus,
        "sections": snap.sections or {},
        "signals": snap.signals or {},
        "covered_through": _iso(snap.covered_through),
        "progress": snap.progress or {},
        "partial_errors": snap.partial_errors or [],
        "model_id": snap.model_id,
        "created_by": snap.created_by,
        "created_at": _iso(snap.created_at),
        "updated_at": _iso(snap.updated_at),
    }


def _serialize_event(ev: XrayTimelineEvent) -> dict[str, Any]:
    # title/detail run through plain_text again on the way out: the fetcher now
    # strips HubSpot's HTML note bodies, but events persisted before that fix
    # still hold raw ``<div style=…>`` markup, and the timeline is append-only
    # (dedupe on event_key), so a refresh will never rewrite them.
    return {
        "id": ev.id,
        "event_key": ev.event_key,
        "ts": _iso(ev.ts),
        "source": ev.source,
        "kind": ev.kind,
        "title": plain_text(ev.title),
        "detail": plain_text(ev.detail) or None,
        "evidence": ev.evidence,
    }


def _serialize_history_row(snap: XraySnapshot) -> dict[str, Any]:
    churn = (snap.signals or {}).get("churn") or {}
    return {
        "id": snap.id,
        "version": snap.version,
        "kind": snap.kind,
        "status": snap.status,
        "created_by": snap.created_by,
        "created_at": _iso(snap.created_at),
        "churn": {"score": churn.get("score"), "band": churn.get("band")},
    }


def _load_snapshot_dict(
    snapshot_id: str, *, with_timeline: bool
) -> dict[str, Any] | None:
    with SessionLocal() as db:
        snap = persistence.get_snapshot(db, snapshot_id)
        if snap is None:
            return None
        data = _serialize_snapshot(snap)
        if with_timeline and snap.status == "ready":
            data["timeline"] = [
                _serialize_event(e)
                for e in persistence.timeline(db, snap.company_id, limit=100)
            ]
        return data


async def _require_owned_company(company_id: str) -> None:
    """Live HubSpot ownership is required even for a previously saved X-Ray."""
    if not await run_in_threadpool(fetchers.company_available, company_id):
        raise HTTPException(status_code=404, detail="X-Ray customer not found.")


def _context_blocks(user: UserProfile) -> tuple[str, str]:
    """Personalization + WGLL blocks, built in a short session then released.

    Both are best-effort — a failure never blocks a run (mirrors the priorities
    ``_resolve_wgll_text`` fail-safe posture).
    """
    personalization = ""
    wgll = ""
    try:
        with SessionLocal() as db:
            try:
                from backend.services.user_context import build_user_context_block

                personalization = build_user_context_block(user, db) or ""
            except Exception:  # noqa: BLE001
                logger.debug("xray personalization unavailable", exc_info=True)
            try:
                from rubik_core.feedback.wgll import build_wgll_context

                wgll = (
                    build_wgll_context(db, surface="xray", user_email=user.email) or ""
                )
            except Exception:  # noqa: BLE001
                logger.debug("xray wgll unavailable", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.debug("xray context blocks failed", exc_info=True)
    return personalization, wgll


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.get("/customers")
async def list_customers(
    q: str = Query("", description="company name/domain query"),
    _: UserProfile = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Live HubSpot company search merged with our snapshot index."""
    if not q.strip():
        return []
    return await run_in_threadpool(service.customer_search, q, limit=10)


@router.get("/recent")
async def recent_xrays(
    _: UserProfile = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Most-recently generated X-Rays (one per company) for the grid."""
    with SessionLocal() as db:
        rows = persistence.recent_ready(db, limit=24)
        out: list[dict[str, Any]] = []
        for snap in rows:
            sig = snap.signals or {}
            churn = sig.get("churn") or {}
            out.append(
                {
                    "company_id": snap.company_id,
                    "name": snap.company_name or sig.get("company_name") or "",
                    "domain": snap.company_domain or sig.get("company_domain") or "",
                    "client_status": sig.get("client_status") or "",
                    "csm": sig.get("csm") or "",
                    "latest_snapshot": {
                        "id": snap.id,
                        "kind": snap.kind,
                        "version": snap.version,
                        "churn_band": churn.get("band"),
                        "churn_score": churn.get("score"),
                        "created_at": _iso(snap.created_at),
                    },
                }
            )
    visible = await run_in_threadpool(
        fetchers.visible_company_ids, [row["company_id"] for row in out]
    )
    return [row for row in out if row["company_id"] in visible]


async def _stamp_crm_link(company_id: str) -> None:
    """Point the HubSpot company record at this customer's X-Ray page.

    **Called from the router, deliberately — not from the service layer.** The
    write has to happen inside the request a person triggered: the write token
    is mounted on the ``cubie`` Service only, so the same call made from
    ``run_deep_job`` would be refused whenever that runs on the ``cubie-xray-deep``
    Cloud Run Job. Here it runs on the Service in both dispatch modes.

    Best-effort by construction (``crm_link.publish_research_link`` never
    raises), and off the event loop because it makes blocking HTTP calls.
    """
    await run_in_threadpool(crm_link.publish_research_link, company_id)


@router.post("/{company_id}/fast")
async def generate_fast(
    company_id: str,
    body: XrayRunRequest | None = None,
    user: UserProfile = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate a Fast X-Ray inline (target p50 ≤ 20s) and return the snapshot."""
    await _require_owned_company(company_id)
    body = body or XrayRunRequest()
    personalization, wgll = _context_blocks(user)
    snapshot_id = await run_in_threadpool(
        service.run_fast,
        company_id,
        requesting_user=user.email,
        focus=(body.focus or ""),
        personalization=personalization,
        wgll=wgll,
        company_name=(body.company_name or ""),
        company_domain=(body.company_domain or ""),
    )
    data = _load_snapshot_dict(snapshot_id, with_timeline=True)
    if data is None:
        raise HTTPException(status_code=500, detail="X-Ray generation failed.")
    await _require_owned_company(company_id)
    # Only stamp a run that produced something to read. A `failed` snapshot
    # (no CRM data for the company) would link reps to an empty page.
    if data.get("status") == "ready":
        await _stamp_crm_link(company_id)
    return data


@router.post("/{company_id}/deep", status_code=202)
async def generate_deep(
    company_id: str,
    body: XrayRunRequest | None = None,
    user: UserProfile = Depends(get_current_user),
) -> dict[str, Any]:
    """Kick off a Deep Research run. 202 + snapshot_id; poll /runs/{id}."""
    reject_support_background_work()
    await _require_owned_company(company_id)
    if not service.deep_runner_enabled():
        raise HTTPException(status_code=503, detail="Deep Research is disabled.")
    body = body or XrayRunRequest()
    try:
        snapshot_id = await run_in_threadpool(
            service.create_pending_deep,
            company_id,
            requesting_user=user.email,
            focus=(body.focus or ""),
            company_name=(body.company_name or ""),
            company_domain=(body.company_domain or ""),
        )
    except service.XrayConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Stamp now, not when the run lands: the link is the same stable URL either
    # way, and the pipeline finishes inside a Cloud Run Job that holds no write
    # token. Worst case a run then fails and the link leads to "no X-Ray yet"
    # until the next successful one — recoverable, and self-correcting.
    await _stamp_crm_link(company_id)

    personalization, wgll = _context_blocks(user)

    # Prod-grade reliability like Deep Meeting Prep: on GCP, run the pipeline on
    # a CPU-allocated Cloud Run Job (never starved). Locally / single warm
    # instance, run it in the app's thread pool. Either way the contract is
    # 202 + poll /runs/{id}.
    if config.deep_runner_mode() == "job":
        try:
            _launch_xray_deep_job_execution(
                snapshot_id=snapshot_id,
                company_id=company_id,
                user_email=user.email,
                focus=body.focus or "",
                company_name=body.company_name or "",
                company_domain=body.company_domain or "",
            )
            logger.info("xray deep %s dispatched to Cloud Run Job", snapshot_id)
            return {"snapshot_id": snapshot_id, "status": "pending"}
        except Exception:  # noqa: BLE001 - degrade to in-process, never 500 the launch
            logger.exception(
                "xray deep %s: Cloud Run Job launch failed — falling back to "
                "in-process (create the '%s' Cloud Run Job for prod-grade "
                "reliability)",
                snapshot_id,
                config.XRAY_DEEP_JOB_NAME,
            )

    _DEEP_POOL.submit(
        _run_deep_safe,
        snapshot_id,
        company_id,
        user.email,
        body.focus or "",
        personalization,
        wgll,
        body.company_name or "",
        body.company_domain or "",
    )
    return {"snapshot_id": snapshot_id, "status": "pending"}


def _run_deep_safe(
    snapshot_id: str,
    company_id: str,
    requesting_user: str,
    focus: str,
    personalization: str,
    wgll: str,
    company_name: str,
    company_domain: str,
) -> None:
    """Thread-pool entry — never lets an exception escape the worker."""
    try:
        service.run_deep_job(
            snapshot_id,
            company_id,
            requesting_user=requesting_user,
            focus=focus,
            personalization=personalization,
            wgll=wgll,
            company_name=company_name,
            company_domain=company_domain,
        )
    except Exception:  # noqa: BLE001
        logger.exception("xray deep pool job crashed for snapshot %s", snapshot_id)


def _launch_xray_deep_job_execution(
    *,
    snapshot_id: str,
    company_id: str,
    user_email: str,
    focus: str,
    company_name: str,
    company_domain: str,
) -> dict[str, Any]:
    """Trigger a Cloud Run Job execution for one Deep Research run via the Cloud
    Run Admin v2 REST API, passing per-run params as container arg overrides.

    Mirrors ``_launch_deep_prep_job_execution`` in backend/routers/home.py. The
    ``cubie-xray-deep`` job must exist in GCP (same image as the service). Its
    spec of record is ``deploy/jobs.yaml``; the guarded platform deploy applies
    that manifest after the Service is serving the new image digest.

    Raises on failure so the caller can fall back to the in-process runner.
    """
    from backend.jobs.launcher import JobLaunchError, run_job_request
    from rubik_core.xray import config

    # containerOverrides.args REPLACES the container args, so re-supply the
    # module invocation (command stays "python").
    args = [
        "-m",
        "backend.jobs.xray_deep_job",
        "--snapshot-id",
        snapshot_id,
        "--company-id",
        company_id,
        "--user-email",
        user_email,
    ]
    if focus:
        args += ["--focus", focus]
    if company_name:
        args += ["--company-name", company_name]
    if company_domain:
        args += ["--company-domain", company_domain]

    body = {"overrides": {"containerOverrides": [{"args": args}]}}
    try:
        return run_job_request(config.XRAY_DEEP_JOB_NAME, body)
    except JobLaunchError as exc:
        raise RuntimeError("cubie-xray-deep job launch failed") from exc


@router.get("/runs/{snapshot_id}")
async def run_status(
    snapshot_id: str,
    _: UserProfile = Depends(get_current_user),
) -> dict[str, Any]:
    """Poll a deep run. The frontend polls every 5s (plain polling, no SSE/WS)."""
    data = _load_snapshot_dict(snapshot_id, with_timeline=True)
    if data is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    await _require_owned_company(data["company_id"])
    out: dict[str, Any] = {
        "status": data["status"],
        "progress": data["progress"],
        "error": data["error"],
    }
    if data["status"] == "ready":
        out["snapshot"] = data
    return out


# ───────────── X-Ray docs: editable Word download + save-to-Assets ──────────
# Multi-segment paths, so the single-segment `/{company_id}` catch-all below
# can't swallow them. Snapshots are a team asset (router-level
# require_sales_or_admin), so any sales user may export/save a ready one.


def _load_ready_snapshot(snapshot_id: str) -> XraySnapshot:
    with SessionLocal() as db:
        snap = db.get(XraySnapshot, snapshot_id)
        if snap is None or snap.status != "ready":
            raise HTTPException(status_code=404, detail="X-Ray snapshot not found.")
        db.expunge(snap)
        return snap


def _render_snapshot_docx(snap: XraySnapshot, prepared_for: str) -> tuple[bytes, str]:
    import re

    from rubik_core.xray.docx import render_xray_docx

    company = snap.company_name or "Customer"
    data = render_xray_docx(
        company_name=company,
        signals=snap.signals or {},
        sections=snap.sections or {},
        prepared_for=prepared_for,
        generated_at=(snap.created_at or datetime.utcnow()).strftime("%b %d, %Y"),
    )
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    # The page can open any earlier version, so two exports of one customer
    # must not share a filename (on disk or on the Assets page).
    return data, f"customer-xray-{slug or 'customer'}-v{snap.version}.docx"


@router.get("/snapshots/{snapshot_id}/docx")
async def download_xray_docx(
    snapshot_id: str,
    user: UserProfile = Depends(get_current_user),
):
    """The X-Ray brief as an editable Word doc (tweak before forwarding)."""
    from fastapi import Response

    from backend.services.generated_docs import DOCX_MIME

    snap = _load_ready_snapshot(snapshot_id)
    await _require_owned_company(snap.company_id)
    data, filename = _render_snapshot_docx(snap, user.email)
    return Response(
        content=data,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/snapshots/{snapshot_id}/save-to-assets")
async def save_xray_to_assets(
    snapshot_id: str,
    user: UserProfile = Depends(get_current_user),
):
    """Persist the X-Ray as a Word doc on the caller's Assets page."""
    from backend.services.generated_docs import save_generated_doc

    snap = _load_ready_snapshot(snapshot_id)
    await _require_owned_company(snap.company_id)
    data, filename = _render_snapshot_docx(snap, user.email)
    asset, created = save_generated_doc(
        user.email,
        title=f"Customer X-Ray — {snap.company_name or 'Customer'} (v{snap.version})",
        filename=filename,
        data=data,
        kind="customer-xray",
        source_company_id=snap.company_id,
        # Scope to the saver's department so RBAC/exec-oversight apply (a
        # department-less asset is ungoverned — see has_document_access).
        department=user.department,
    )
    return {
        "doc_id": asset.doc_id,
        "created": created,
        "filename": asset.original_filename,
    }


@router.get("/{company_id}")
async def get_latest(
    company_id: str,
    _: UserProfile = Depends(get_current_user),
) -> dict[str, Any]:
    """Latest ready snapshot + timeline for a company."""
    await _require_owned_company(company_id)
    with SessionLocal() as db:
        snap = persistence.latest_ready(db, company_id)
        if snap is None:
            raise HTTPException(
                status_code=404, detail="No X-Ray generated for this customer yet."
            )
        data = _serialize_snapshot(snap)
        data["timeline"] = [
            _serialize_event(e) for e in persistence.timeline(db, company_id, limit=100)
        ]
        return data


@router.get("/{company_id}/history")
async def get_history(
    company_id: str,
    _: UserProfile = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await _require_owned_company(company_id)
    with SessionLocal() as db:
        return [_serialize_history_row(r) for r in persistence.history(db, company_id)]


@router.get("/{company_id}/timeline")
async def get_timeline(
    company_id: str,
    source: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    before: str | None = None,
    _: UserProfile = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await _require_owned_company(company_id)
    before_dt: datetime | None = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid 'before' timestamp."
            ) from exc
    with SessionLocal() as db:
        events = persistence.timeline(
            db, company_id, source=source, limit=limit, before=before_dt
        )
        return [_serialize_event(e) for e in events]
