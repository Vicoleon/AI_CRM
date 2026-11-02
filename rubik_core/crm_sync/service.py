"""The CRM Sync scan: transcripts in, reviewable suggestions out.

One pass per transcript — gather the HubSpot records for everyone on the call,
make a single extraction call, validate hard, and persist whatever survives as
`pending` suggestions for the AE to review. Nothing here writes to HubSpot.

Deliberately sequential. The volume is bounded (a scan covers one person's week
of external calls) and HubSpot has no retry budget to spare: the base client
does no 429 handling of its own, so a fan-out here would trade a few seconds of
wall clock for rate-limit failures during the nightly batch.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from rubik_core.ai_providers.registry import IncompatibleModelRoute, resolve_model_route
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.crm_sync import run_state
from rubik_core.crm_sync.llm import llm_json
from rubik_core.crm_sync.prompts import extraction_prompt
from rubik_core.crm_sync.validators import ScanContext, validate_suggestions
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import CrmSyncSuggestion
from rubik_core.sales.model_config import model_for
from rubik_core.tenancy.hubspot_config import get_hubspot_config

logger = logging.getLogger(__name__)

# Extraction reads further into a call than the signal layer's 50KB keyword
# scan does: commitments, next steps and pricing tend to land in the last ten
# minutes, which a 50KB head-read would miss entirely on a long call.
TRANSCRIPT_MAX_BYTES = 150_000

MAX_TRANSCRIPTS_PER_RUN = 25
MAX_EXTERNAL_DOMAINS_PER_TRANSCRIPT = 3
MAX_SUGGESTIONS_PER_RUN = 60

STATUS_PENDING = "pending"
STATUS_APPLYING = "applying"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_DISMISSED = "dismissed"
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"

# Statuses that mean "this exact change is already in front of the user, or was
# already done" — a re-scan proposing it again is a duplicate, not news.
_BLOCKING_STATUSES = (STATUS_PENDING, STATUS_APPLYING, STATUS_APPLIED, STATUS_FAILED)


def _domain(email: str) -> str:
    return (email or "").split("@")[-1].strip().lower()


def _external_attendees(participants: list[str], ae_email: str) -> list[str]:
    """Attendees whose email domain differs from the AE's.

    Same rule the transcript signal source uses; reimplemented here because
    those helpers are module-private to ``backend/signals/transcript.py``.
    """
    ae_domain = _domain(ae_email)
    if not ae_domain:
        return []
    seen: dict[str, None] = {}
    for email in participants or []:
        addr = (email or "").strip().lower()
        if not addr or "@" not in addr:
            continue
        if _domain(addr) == ae_domain:
            continue
        seen.setdefault(addr, None)
    return list(seen)


def _expire_stale_pending(user_email: str) -> int:
    """Sweep pending suggestions that have gone stale. Never raises.

    Runs at scan start rather than on its own schedule: the queue only matters
    when someone is looking at it, and a suggestion drafted against two-week-old
    CRM state has probably been overtaken by reality.
    """
    cutoff = datetime.utcnow() - timedelta(days=settings.CRM_SYNC_PENDING_EXPIRE_DAYS)
    try:
        with SessionLocal() as db:
            count = (
                db.query(CrmSyncSuggestion)
                .filter(CrmSyncSuggestion.user_email == user_email)
                .filter(CrmSyncSuggestion.status == STATUS_PENDING)
                .filter(CrmSyncSuggestion.created_at < cutoff)
                .update(
                    {"status": STATUS_EXPIRED, "updated_at": datetime.utcnow()},
                    synchronize_session=False,
                )
            )
            db.commit()
            return int(count or 0)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[crm-sync] expiring stale suggestions failed for %s", user_email
        )
        return 0


def _lookup_existing(db, user_email: str, dedupe_key: str) -> CrmSyncSuggestion | None:
    """The row for this exact change, if it has ever been proposed before."""
    return (
        db.query(CrmSyncSuggestion)
        .filter(CrmSyncSuggestion.user_email == user_email)
        .filter(CrmSyncSuggestion.dedupe_key == dedupe_key)
        .first()
    )


def _still_suppressed(row: CrmSyncSuggestion) -> bool:
    """True when this exact change should not be shown to the AE again yet."""
    if row.status in _BLOCKING_STATUSES:
        # Already in front of them, or already done.
        return True
    if row.status == STATUS_DISMISSED:
        # A dismissal usually means "not now" rather than "never". After the
        # suppression window a fact still being asserted on new calls earns a
        # fresh ask — with new evidence attached.
        window = timedelta(days=settings.CRM_SYNC_DISMISS_SUPPRESS_DAYS)
        return (row.updated_at or row.created_at or datetime.utcnow()) > (
            datetime.utcnow() - window
        )
    # superseded / expired never suppress.
    return False


def _reoffer(row: CrmSyncSuggestion, run_id, record, item, payload, truncated) -> None:
    """Bring a retired suggestion back as pending, on its own row.

    Re-offering has to reuse the row rather than insert a second one: uniqueness
    on ``(user_email, dedupe_key)`` is absolute, which is what makes concurrent
    scans safe. Keeping one row per exact change also keeps its history in one
    place — the AE can see this was raised, dismissed, and has come up again.
    """
    row.run_id = run_id
    row.doc_id = record.doc_id
    row.meeting_id = record.meeting_id
    row.meeting_title = record.original_filename
    row.meeting_date = record.created_at
    row.payload = payload
    row.evidence = {**item.evidence, "truncated": truncated}
    row.confidence = item.confidence
    row.status = STATUS_PENDING
    row.error = None
    row.updated_at = datetime.utcnow()


def _supersede_older(db, user_email: str, target_key: str, keep_id: str) -> int:
    """Retire earlier pending cards for the same target.

    Without this, a scan that proposes a newer value for a field the AE hasn't
    reviewed yet would leave two live cards for one field and let them approve
    the stale one.
    """
    return int(
        db.query(CrmSyncSuggestion)
        .filter(CrmSyncSuggestion.user_email == user_email)
        .filter(CrmSyncSuggestion.target_key == target_key)
        .filter(CrmSyncSuggestion.status == STATUS_PENDING)
        .filter(CrmSyncSuggestion.id != keep_id)
        .update(
            {"status": STATUS_SUPERSEDED, "updated_at": datetime.utcnow()},
            synchronize_session=False,
        )
        or 0
    )


def build_context_bundle(
    client,
    *,
    record,
    ae_email: str,
    ae_owner_id: str | None,
    external_attendees: list[str],
    pipeline_stages: dict,
    owner_names: dict,
    transcript_text: str,
    truncated: bool,
) -> tuple[dict, ScanContext]:
    """Fetch the live HubSpot records this call could plausibly touch.

    Returns the prompt-facing bundle and the validator-facing context. They are
    built together on purpose: the validator's idea of "records that exist" must
    be exactly the set the model was shown, or object-id closure would either
    reject valid ids or admit ones we never fetched.
    """
    from rubik_core.crm_sync.hubspot_context import (
        fetch_companies_for_domains,
        fetch_contacts_for_emails,
        fetch_deals_for_companies,
    )
    from rubik_core.retrieval.hubspot_api import CONTACT_SEARCH_PROPERTIES

    contacts = fetch_contacts_for_emails(
        client, external_attendees, properties=CONTACT_SEARCH_PROPERTIES
    )
    domains = []
    for email in external_attendees:
        domain = _domain(email)
        if domain and domain not in domains:
            domains.append(domain)
    domains = domains[:MAX_EXTERNAL_DOMAINS_PER_TRANSCRIPT]

    companies = fetch_companies_for_domains(client, domains)
    deals = fetch_deals_for_companies(
        client,
        [c["hubspot_id"] for c in companies],
        ae_owner_id=ae_owner_id,
        stage_labels={
            sid: lbl
            for stages in pipeline_stages.values()
            for sid, lbl in stages.items()
        },
        owner_names=owner_names,
    )

    bundle = {
        "meeting": {
            "title": record.original_filename,
            "date": record.created_at.strftime("%Y-%m-%d")
            if record.created_at
            else None,
            "ae_email": ae_email,
            "ae_owner_id": ae_owner_id,
            "external_attendees": external_attendees,
            "truncated": truncated,
        },
        "contacts": contacts,
        "companies": companies,
        "deals": deals,
        "pipeline_stages": pipeline_stages,
        "transcript_text": transcript_text,
    }

    ctx = ScanContext(
        ae_email=ae_email,
        ae_owner_id=ae_owner_id,
        transcript_text=transcript_text,
        external_attendees=external_attendees,
        contacts_by_id={c["hubspot_id"]: c for c in contacts if c.get("hubspot_id")},
        contact_id_by_email={
            c["email"].lower(): c["hubspot_id"]
            for c in contacts
            if c.get("hubspot_id") and c.get("email")
        },
        company_ids={c["hubspot_id"] for c in companies},
        deals_by_id={d["hubspot_id"]: d for d in deals},
        pipeline_stages=pipeline_stages,
    )
    return bundle, ctx


@attribute_to("crm_sync")
def run_scan(
    run_id: str,
    user_email: str,
    *,
    since_days: int | None = None,
    since: datetime | None = None,
    transcript_service=None,
    hubspot_client=None,
    llm=None,
) -> dict:
    """Scan one user's recent external calls and queue CRM suggestions.

    Records its outcome on the run row and never raises: the caller is a Cloud
    Run Job whose non-zero exit would trigger a retry of an expensive LLM
    pipeline whose failure is already recorded.
    """
    user_email = (user_email or "").lower()
    stats = {
        "transcripts_scanned": 0,
        "external_meetings": 0,
        "suggestions_created": 0,
        "duplicates_skipped": 0,
        "validation_rejected": 0,
        "superseded": 0,
        "expired": 0,
    }

    try:
        allowed_pipelines = set(get_hubspot_config().pipeline_ids)
        if not allowed_pipelines:
            run_state.mark_finished(
                run_id,
                succeeded=False,
                error="CRM Sync scan refused: no HubSpot deal pipelines configured for this workspace",
                stats=stats,
            )
            return stats

        if since is None:
            days = since_days or settings.CRM_SYNC_SCAN_DAYS
            since = datetime.now(timezone.utc) - timedelta(days=days)

        stats["expired"] = _expire_stale_pending(user_email)

        if transcript_service is None:
            from backend.services.transcript_service import TranscriptService

            transcript_service = TranscriptService(user_email)

        records = transcript_service.list_transcripts_for_signals(since=since)
        records = records[:MAX_TRANSCRIPTS_PER_RUN]

        if hubspot_client is None:
            from rubik_core.retrieval.hubspot_api import HubSpotAPIClient

            hubspot_client = HubSpotAPIClient()

        # Fetched once per run, not per transcript: these are the two calls that
        # would otherwise repeat for every meeting and burn the rate limit.
        owner_directory = hubspot_client.owner_directory()
        owner_names = {oid: info.get("name") for oid, info in owner_directory.items()}
        ae_owner_id = next(
            (
                str(oid)
                for oid, info in owner_directory.items()
                if (info.get("email") or "").lower() == user_email
            ),
            None,
        )
        pipeline_stages = {
            pid: stages
            for pid, stages in hubspot_client.deal_pipeline_stages().items()
            if pid in allowed_pipelines
        }

        if llm is None:
            default_model = model_for("crm_sync")
            route = resolve_model_route("crm_sync", "json", default_model=default_model)

            # One route for the whole scan; each call verifies it is still the
            # live route before sending another transcript to a provider.
            def extract(system, prompt):
                return llm_json(system, prompt, model_id=default_model, route=route)
        else:
            extract = llm

        for record in records:
            stats["transcripts_scanned"] += 1
            externals = _external_attendees(record.participants, user_email)
            if not externals:
                # Internal meeting — nothing here belongs in a customer CRM,
                # and skipping before the LLM call keeps the scan cheap.
                continue
            stats["external_meetings"] += 1

            if stats["suggestions_created"] >= MAX_SUGGESTIONS_PER_RUN:
                logger.info(
                    "[crm-sync] run %s hit the %d-suggestion cap; "
                    "%d transcripts not scanned",
                    run_id,
                    MAX_SUGGESTIONS_PER_RUN,
                    len(records) - stats["transcripts_scanned"],
                )
                break

            try:
                text = transcript_service.read_transcript_text(
                    record, max_bytes=TRANSCRIPT_MAX_BYTES
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[crm-sync] could not read transcript %s: %s",
                    record.doc_id,
                    type(exc).__name__,
                )
                continue
            if not (text or "").strip():
                continue
            truncated = len(text.encode("utf-8", "ignore")) >= TRANSCRIPT_MAX_BYTES
            attendees = externals[: MAX_EXTERNAL_DOMAINS_PER_TRANSCRIPT * 3]

            try:
                bundle, ctx = build_context_bundle(
                    hubspot_client,
                    record=record,
                    ae_email=user_email,
                    ae_owner_id=ae_owner_id,
                    external_attendees=attendees,
                    pipeline_stages=pipeline_stages,
                    owner_names=owner_names,
                    transcript_text=text,
                    truncated=truncated,
                )
                system, prompt = extraction_prompt(bundle)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[crm-sync] context failed for transcript %s: %s",
                    record.doc_id,
                    type(exc).__name__,
                )
                continue

            raw = None
            try:
                raw = extract(system, prompt)
            except (IncompatibleModelRoute, ProviderRequestError):
                # A selected client route is a billing and processing choice.
                # Never report a scan as ready after its credential/model fails.
                raise
            except Exception as exc:  # noqa: BLE001
                if llm is None and route.provider != "vertex_managed":
                    # Unexpected adapter errors must fail closed too. The
                    # original exception may contain a provider response or
                    # private transcript text, so do not chain or log it.
                    raise ProviderRequestError(
                        "Selected AI provider request failed during CRM Sync"
                    ) from None
                # One unreadable meeting (HubSpot hiccup, malformed LLM reply)
                # must not cost the user the rest of the scan.
                logger.warning(
                    "[crm-sync] extraction failed for transcript %s: %s",
                    record.doc_id,
                    type(exc).__name__,
                )

            if raw is not None:
                accepted, rejections = validate_suggestions(
                    raw, ctx, doc_id=record.doc_id
                )
                stats["validation_rejected"] += len(rejections)
                if rejections:
                    logger.info(
                        "[crm-sync] %s: rejected %d suggestion(s)",
                        record.doc_id,
                        len(rejections),
                    )
                _queue(stats, run_id, user_email, record, accepted, truncated)

        run_state.mark_finished(run_id, succeeded=True, stats=stats)
        logger.info("[crm-sync] run %s for %s finished: %s", run_id, user_email, stats)
        return stats

    except (IncompatibleModelRoute, ProviderRequestError) as exc:
        logger.warning(
            "[crm-sync] selected AI route failed for run %s: %s",
            run_id,
            type(exc).__name__,
        )
        run_state.mark_finished(
            run_id,
            succeeded=False,
            error=(
                "Selected AI provider is unavailable for CRM Sync. "
                "Ask a workspace admin to check Admin → Access → Google Workspace → AI provider credentials."
            ),
            stats=stats,
        )
        return stats
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[crm-sync] run %s failed for %s: %s",
            run_id,
            user_email,
            type(exc).__name__,
        )
        run_state.mark_finished(
            run_id,
            succeeded=False,
            error="CRM Sync scan failed. Try again or ask a workspace admin to check the connections.",
            stats=stats,
        )
        return stats


def _queue(stats: dict, run_id, user_email, record, items, truncated) -> None:
    """Persist ``items`` under the per-run cap, updating ``stats``."""
    for item in items:
        if stats["suggestions_created"] >= MAX_SUGGESTIONS_PER_RUN:
            break
        queued, superseded = _persist(run_id, user_email, record, item, truncated)
        if queued:
            stats["suggestions_created"] += 1
            stats["superseded"] += superseded
        else:
            stats["duplicates_skipped"] += 1


def _persist(run_id, user_email, record, item, truncated) -> tuple[bool, int]:
    """Queue one suggestion.

    Returns ``(queued, superseded_count)`` — ``queued`` is False when this exact
    change is already in front of the user or was recently handled.
    """
    payload = {"properties": item.properties}
    # create_deal carries the records to link the new deal to; apply._write
    # reads them from this key.
    if item.associations:
        payload["associations"] = item.associations
    if item.contact_email:
        payload["contact_email"] = item.contact_email
    if item.rationale:
        payload["rationale"] = item.rationale

    try:
        with SessionLocal() as db:
            existing = _lookup_existing(db, user_email, item.dedupe_key)
            if existing is not None:
                if _still_suppressed(existing):
                    return False, 0
                _reoffer(existing, run_id, record, item, payload, truncated)
                superseded = _supersede_older(
                    db, user_email, item.target_key, existing.id
                )
                db.commit()
                return True, superseded

            row = CrmSyncSuggestion(
                id=uuid.uuid4().hex,
                user_email=user_email,
                run_id=run_id,
                doc_id=record.doc_id,
                meeting_id=record.meeting_id,
                meeting_title=record.original_filename,
                meeting_date=record.created_at,
                change_type=item.change_type,
                hubspot_object_type=item.hubspot_object_type,
                hubspot_object_id=item.hubspot_object_id,
                target_key=item.target_key,
                dedupe_key=item.dedupe_key,
                payload=payload,
                evidence={**item.evidence, "truncated": truncated},
                confidence=item.confidence,
                status=STATUS_PENDING,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(row)
            db.flush()
            superseded = _supersede_older(db, user_email, item.target_key, row.id)
            db.commit()
            return True, superseded
    except IntegrityError:
        # The unique constraint caught a race with a concurrent scan (the
        # nightly window overlaps the on-demand one by design).
        return False, 0
