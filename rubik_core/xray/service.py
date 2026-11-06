"""X-Ray service — the entry points the router calls.

Composes checkpoint → fetch → churn → synthesis → persist for both flavors and
enforces the connection rule in docs/ENGINEERING_NOTES.md: NO DB session is held during fetch or
LLM/agent work. Each phase opens a short session, does its read/write, and
closes — the heavy work in between holds nothing.

Contract:
* ``run_fast`` runs inline (router awaits it via run_in_threadpool) and returns
  the new snapshot id.
* ``create_pending_deep`` inserts the pending row (409 if one is already
  running) and returns its id; ``run_deep_job`` then executes the pipeline for
  that row out-of-request and flips it to ready/failed. Splitting the two lets
  the router return 202 immediately and the frontend poll ``/runs/{id}``.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any

from rubik_core.ai_providers.registry import IncompatibleModelRoute
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.operations import get_operations_config
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.database.db import SessionLocal
from rubik_core.xray import checkpoint, config, fetchers, opportunities, persistence
from rubik_core.xray import churn as churn_mod
from rubik_core.xray import properties as xray_properties
from rubik_core.xray.agents import analyst as analyst_mod
from rubik_core.xray.agents.base import llm_json, model_label, resolve_xray_route
from rubik_core.xray.schemas import ChurnResult, CompanyBundle

logger = logging.getLogger(__name__)


class XrayConflict(Exception):
    """A deep run is already pending/running for this company (→ HTTP 409)."""


# ── Fast X-Ray ────────────────────────────────────────────────────────────────
@attribute_to("customer_xray")
def run_fast(
    company_id: str,
    *,
    requesting_user: str,
    focus: str = "",
    personalization: str = "",
    wgll: str = "",
    company_name: str = "",
    company_domain: str = "",
) -> str:
    """Generate a Fast X-Ray and return the new snapshot id."""
    # Phase 1 — checkpoint (short session).
    with SessionLocal() as db:
        ckpt = checkpoint.load(db, company_id)

    # Phase 2 — fetch + compute (NO session held).
    bundle = fetchers.fetch_company_bundle(company_id, cursors=ckpt.cursors)
    if company_name and not bundle.company_name:
        bundle.company_name = company_name
    if company_domain and not bundle.company_domain:
        bundle.company_domain = company_domain

    base_timeline = fetchers.build_timeline_events(bundle)

    if _is_empty(bundle):
        return _persist_failed(
            company_id,
            kind="fast",
            ckpt=ckpt,
            requesting_user=requesting_user,
            focus=focus,
            bundle=bundle,
            error=(
                bundle.errors[0] if bundle.errors else "No CRM data for this customer."
            ),
        )

    the_churn = churn_mod.score(bundle)
    whitespace = opportunities.whitespace(bundle)

    # Phase 3 — one synthesis LLM call (NO session held).
    from rubik_core.xray import prompts

    system, prompt = prompts.build_fast_prompt(
        bundle,
        the_churn,
        whitespace,
        base_timeline,
        prev_sections=ckpt.prev_sections,
        focus=focus,
        personalization=personalization,
        wgll=wgll,
    )
    partial_errors = list(bundle.errors)
    # Resolve once and reuse for the persisted record below: two calls could
    # straddle an admin save and record a model this run did not actually use.
    fast_model = config.model_fast_synth()
    used_model = ""  # no model is credited if route resolution fails
    try:
        fast_route = resolve_xray_route(fast_model)
        used_model = model_label(fast_route)
        raw = llm_json(system, prompt, model_id=fast_model, route=fast_route)
        sections = analyst_mod.normalize_sections(raw)
    except Exception:  # noqa: BLE001 - degrade to the deterministic core
        # Model SDK and parse exceptions can contain customer text. The same
        # bounded explanation goes to the log and visible Intelligence Gaps.
        logger.warning("xray fast synthesis unavailable for %s", company_id)
        sections = analyst_mod.normalize_sections({})
        sections["executive_summary"] = (
            "Automated summary unavailable this run; churn score and signals below "
            "are computed directly from CRM data."
        )
        partial_errors.append(
            "X-Ray analysis unavailable. Check the selected AI provider in Admin."
        )

    # Deterministic guarantees.
    the_churn.rationale_md = sections.get("churn_rationale_md") or ""
    if not sections.get("opportunities"):
        sections["opportunities"] = [
            o.as_dict() for o in opportunities.fallback_opportunities(bundle)
        ]
    sections = checkpoint.carry_forward_sections(
        ckpt.prev_sections,
        sections,
        new_kind="fast",
        prev_created_at=ckpt.prev_created_at,
    )

    signals = _build_signals(bundle, the_churn)

    # Phase 4 — persist (short session).
    return _persist_ready(
        company_id,
        kind="fast",
        ckpt=ckpt,
        requesting_user=requesting_user,
        focus=focus,
        bundle=bundle,
        sections=sections,
        signals=signals,
        base_timeline=base_timeline,
        partial_errors=partial_errors,
        model_id=used_model,
    )


# ── Deep Research ─────────────────────────────────────────────────────────────
def create_pending_deep(
    company_id: str,
    *,
    requesting_user: str,
    focus: str = "",
    company_name: str = "",
    company_domain: str = "",
) -> str:
    """Insert the pending deep-run row (409 if one is active). Returns its id."""
    with SessionLocal() as db:
        if persistence.active_deep_run(
            db, company_id, max_age_minutes=config.DEEP_RUN_TTL_MINUTES
        ):
            raise XrayConflict("A deep research run is already in progress.")
        version = persistence.next_version(db, company_id)
        prev = persistence.latest_ready(db, company_id)
        snap = persistence.create_snapshot(
            db,
            company_id=str(company_id),
            company_name=company_name or None,
            company_domain=company_domain or None,
            version=version,
            base_snapshot_id=(prev.id if prev else None),
            kind="deep",
            status="pending",
            focus=focus or None,
            progress={ch: "queued" for ch in config.DEEP_CHANNELS},
            created_by=requesting_user,
            model_id=None,
        )
        return snap.id


@attribute_to("customer_xray")
def run_deep_job(
    snapshot_id: str,
    company_id: str,
    *,
    requesting_user: str,
    focus: str = "",
    personalization: str = "",
    wgll: str = "",
    company_name: str = "",
    company_domain: str = "",
) -> str:
    """Execute the deep pipeline for a pending snapshot; flip it ready/failed.

    Safe to call out-of-request (thread pool) OR from a Cloud Run Job — it holds
    no session during the multi-agent work (the documented escape hatch, spec §6).
    """
    from rubik_core.xray import orchestrator

    # Mark running + load the checkpoint (from the latest READY, not this row).
    with SessionLocal() as db:
        persistence.update_snapshot(db, snapshot_id, status="running")
        ckpt = checkpoint.load(db, company_id)

    try:
        routes = orchestrator.resolve_routes()
        with SessionLocal() as db:
            persistence.update_snapshot(
                db, snapshot_id, model_id=model_label(routes.analyst)
            )
        bundle = fetchers.fetch_company_bundle(company_id, cursors=ckpt.cursors)
        if company_name and not bundle.company_name:
            bundle.company_name = company_name
        if company_domain and not bundle.company_domain:
            bundle.company_domain = company_domain

        if _is_empty(bundle):
            with SessionLocal() as db:
                persistence.update_snapshot(
                    db,
                    snapshot_id,
                    status="failed",
                    error=(
                        bundle.errors[0]
                        if bundle.errors
                        else "No CRM data for this customer."
                    ),
                )
            return snapshot_id

        base_timeline = fetchers.build_timeline_events(bundle)
        whitespace = opportunities.whitespace(bundle)
        news_after = str(ckpt.cursors.get("news_after") or "")

        def _progress(p: dict[str, Any]) -> None:
            with SessionLocal() as db:
                persistence.update_snapshot(db, snapshot_id, progress=p)

        result = orchestrator.run_deep(
            bundle,
            whitespace,
            base_timeline,
            requesting_user=requesting_user,
            prev_sections=ckpt.prev_sections,
            focus=focus,
            personalization=personalization,
            wgll=wgll,
            news_after=news_after,
            progress_cb=_progress,
            routes=routes,
        )
    except (IncompatibleModelRoute, ProviderRequestError):
        logger.warning("xray deep selected AI provider unavailable for %s", company_id)
        with SessionLocal() as db:
            persistence.update_snapshot(
                db,
                snapshot_id,
                status="failed",
                error="Selected AI provider unavailable for X-Ray analysis. Check its connection in Admin.",
            )
        return snapshot_id
    except Exception as exc:  # noqa: BLE001 - a failed run is recorded, not silent
        # SDK/parser exceptions can echo the customer's data. Keep only the
        # exception type in logs and a fixed, actionable message in snapshots.
        logger.warning(
            "xray deep run failed for %s (%s)", company_id, type(exc).__name__
        )
        with SessionLocal() as db:
            persistence.update_snapshot(
                db,
                snapshot_id,
                status="failed",
                error="X-Ray analysis failed. Please retry or check the workspace AI provider.",
            )
        return snapshot_id

    signals = _build_signals(bundle, result.churn, digests=result.digests)
    cursors = dict(bundle.cursors)
    if bundle.covered_through:
        cursors["news_after"] = bundle.covered_through.date().isoformat()

    # Persist final (short session): flip the pending row to ready + append timeline.
    with SessionLocal() as db:
        persistence.update_snapshot(
            db,
            snapshot_id,
            status="ready",
            company_name=bundle.company_name or None,
            company_domain=bundle.company_domain or None,
            sections=result.sections,
            signals=signals,
            covered_through=bundle.covered_through,
            source_cursors=cursors,
            progress=result.progress,
            partial_errors=result.partial_errors,
            model_id=result.model_id,
            error=None,
        )
        persistence.append_timeline(
            db, company_id, result.timeline_drafts, snapshot_id=snapshot_id
        )

    # Deliver like Deep Meeting Prep: render a PDF and email it to the
    # requesting user. The snapshot is still persisted (team checkpoint), but the
    # deliverable is the emailed PDF — not an on-screen render.
    _deliver_deep_pdf(
        snapshot_id,
        company_name=bundle.company_name or company_name,
        signals=signals,
        sections=result.sections,
        requesting_user=requesting_user,
        base_progress=result.progress,
    )
    return snapshot_id


def _xray_pdf_filename(company_name: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (company_name or "customer").lower()).strip("-")
    return f"customer-xray-{slug or 'customer'}.pdf"


def _deliver_deep_pdf(
    snapshot_id: str,
    *,
    company_name: str,
    signals: dict[str, Any],
    sections: dict[str, Any],
    requesting_user: str,
    base_progress: dict[str, Any],
) -> None:
    """Render the brief to PDF and email it (mirrors Deep Prep). Best-effort —
    records the delivery outcome in ``progress`` and never raises."""
    delivery = "failed"
    email_meta: dict[str, Any] = {}
    try:
        from rubik_core.tools.email_sender import EmailMessageSpec, send_email
        from rubik_core.xray.pdf import render_xray_pdf

        pdf_bytes = render_xray_pdf(
            company_name=company_name,
            signals=signals,
            sections=sections,
            prepared_for=requesting_user,
            generated_at=datetime.utcnow().strftime("%b %d, %Y"),
        )
        band = (signals.get("churn") or {}).get("band") or ""
        subject = f"Your Customer X-Ray: {company_name}" + (
            f" — {band}" if band else ""
        )
        text_body = (
            f"Your Deep Research Customer X-Ray for {company_name} is attached as a "
            f"PDF.\n\nGenerated by {PRODUCT_NAME}. It stays grounded in your records — "
            "verify anything before you rely on it.\n"
        )
        html_body = (
            '<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:560px">'
            "<p>Your Deep Research Customer X-Ray for "
            f"<strong>{html.escape(company_name)}</strong> "
            "is attached as a PDF.</p>"
            '<p style="color:#6d6467;font-size:13px">'
            f"Generated by {PRODUCT_NAME}. It stays "
            "grounded in your records — verify anything before you rely on it.</p></div>"
        )
        impersonate = (
            get_operations_config().deep_prep_sender_mailbox or requesting_user
        )
        result = send_email(
            EmailMessageSpec(
                to=[requesting_user],
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                pdf=pdf_bytes,
                pdf_filename=_xray_pdf_filename(company_name),
            ),
            impersonate=impersonate,
        )
        # "disabled" = PDF built fine but autosend is off (dark rollout).
        delivery = {"sent": "emailed", "disabled": "generated"}.get(
            result.status, "failed"
        )
        email_meta = {
            "email_status": result.status,
            "message_id": result.message_id,
            "error": result.error,
        }
        logger.info(
            "xray deep %s delivery: %s (email=%s)",
            snapshot_id,
            delivery,
            result.status,
        )
    except Exception as exc:  # noqa: BLE001 - delivery never fails the run
        logger.exception("xray deep %s PDF/email delivery failed", snapshot_id)
        email_meta = {"error": str(exc)[:300]}

    try:
        prog = dict(base_progress or {})
        prog["delivery"] = delivery
        prog["email_to"] = requesting_user
        if email_meta:
            prog["email"] = email_meta
        with SessionLocal() as db:
            persistence.update_snapshot(db, snapshot_id, progress=prog)
    except Exception:  # noqa: BLE001
        logger.exception("xray deep %s: could not record delivery status", snapshot_id)


# ── Shared helpers ────────────────────────────────────────────────────────────
def _is_empty(bundle: CompanyBundle) -> bool:
    """No company profile, no deals, no engagements → nothing to snapshot."""
    return not bundle.company and not bundle.deals and not bundle.all_engagements()


def _persist_ready(
    company_id: str,
    *,
    kind: str,
    ckpt,
    requesting_user: str,
    focus: str,
    bundle: CompanyBundle,
    sections: dict[str, Any],
    signals: dict[str, Any],
    base_timeline: list,
    partial_errors: list[str],
    model_id: str,
) -> str:
    cursors = dict(bundle.cursors)
    if bundle.covered_through and "news_after" not in cursors:
        cursors["news_after"] = bundle.covered_through.date().isoformat()
    with SessionLocal() as db:
        snap = persistence.create_snapshot(
            db,
            company_id=str(company_id),
            company_name=bundle.company_name or None,
            company_domain=bundle.company_domain or None,
            version=ckpt.version,
            base_snapshot_id=ckpt.base_snapshot_id,
            kind=kind,
            status="ready",
            focus=focus or None,
            sections=sections,
            signals=signals,
            covered_through=bundle.covered_through,
            source_cursors=cursors,
            partial_errors=partial_errors,
            model_id=model_id,
            created_by=requesting_user,
        )
        persistence.append_timeline(db, company_id, base_timeline, snapshot_id=snap.id)
        return snap.id


def _persist_failed(
    company_id: str,
    *,
    kind: str,
    ckpt,
    requesting_user: str,
    focus: str,
    bundle: CompanyBundle,
    error: str,
) -> str:
    with SessionLocal() as db:
        snap = persistence.create_snapshot(
            db,
            company_id=str(company_id),
            company_name=bundle.company_name or None,
            company_domain=bundle.company_domain or None,
            version=ckpt.version,
            base_snapshot_id=ckpt.base_snapshot_id,
            kind=kind,
            status="failed",
            focus=focus or None,
            error=error[:500],
            partial_errors=list(bundle.errors),
            created_by=requesting_user,
        )
        return snap.id


def _build_signals(
    bundle: CompanyBundle,
    the_churn: ChurnResult,
    *,
    digests: list | None = None,
) -> dict[str, Any]:
    """Dashboard numbers for the signal row + churn gauge."""
    now = datetime.utcnow()
    open_deals = [d for d in bundle.deals if str(d.get("hs_is_closed")) != "true"]

    pipeline = 0.0
    for d in open_deals:
        try:
            pipeline += float(d.get("amount") or 0)
        except (TypeError, ValueError):
            pass

    renewals = [fetchers._parse_ts(xray_properties.renewal_date(d)) for d in open_deals]
    renewals = [r for r in renewals if r]
    renewal_days = min(((r - now).days for r in renewals), default=None)

    dates = churn_mod._activity_dates(bundle)
    last = dates[-1] if dates else churn_mod._company_last_activity(bundle)
    days_since = (now - last).days if last else None

    win = config.ACTIVITY_TREND_WINDOW_DAYS
    recent = sum(1 for d in dates if (now - d).days <= win)
    prior = sum(1 for d in dates if win < (now - d).days <= 2 * win)
    direction = "up" if recent > prior else ("down" if recent < prior else "flat")

    products = opportunities.owned_products_display(bundle)
    signals: dict[str, Any] = {
        "churn": the_churn.as_dict(),
        "days_since_last_touch": days_since,
        "active_products_count": len(products),
        "active_products": products,
        "open_pipeline_value": round(pipeline, 2),
        "open_deals_count": len(open_deals),
        "renewal_countdown_days": renewal_days,
        "activity_trend": {
            "last_90d": recent,
            "prior_90d": prior,
            "direction": direction,
        },
        "client_status": bundle.client_status,
        "csm": bundle.csm,
        "company_name": bundle.company_name,
        "company_domain": bundle.company_domain,
    }
    for d in digests or []:
        if d.channel == "gmail" and not d.error and d.attrs:
            signals["email"] = d.attrs
    return signals


def customer_search(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Live HubSpot company search merged with our snapshot index."""
    rows = fetchers.search_companies(query, limit=limit)
    if not rows:
        return rows
    ids = [r["company_id"] for r in rows]
    with SessionLocal() as db:
        idx = persistence.snapshot_index(db, ids)
        for r in rows:
            snap = idx.get(r["company_id"])
            r["latest_snapshot"] = _snapshot_stub(snap) if snap else None
    return rows


def _snapshot_stub(snap) -> dict[str, Any]:
    churn = (snap.signals or {}).get("churn") or {}
    return {
        "id": snap.id,
        "kind": snap.kind,
        "version": snap.version,
        "churn_band": churn.get("band"),
        "churn_score": churn.get("score"),
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
    }


def deep_runner_enabled() -> bool:
    return bool(getattr(settings, "XRAY_DEEP_ENABLED", True))
