"""Applying an approved suggestion to HubSpot.

The only path from a suggestion row to a CRM write, and it runs only when a
human clicked Approve on that specific item.

Two invariants:

* **Exactly-once per approval.** The status transition is a compare-and-swap, so
  a double-click, a second browser tab, and a retry after a timeout all resolve
  to one write. The CAS also carries the ownership check, which makes it the
  IDOR guard as well: it matches on ``user_email`` too, so another user's id
  simply doesn't match a row.
* **The AE approves current reality.** Between the scan and the click, the CRM
  may have moved. Before writing a deal stage we re-read the deal and refuse if
  it drifted, refreshing the stored "before" so the card re-renders truthfully
  and the AE decides again against what's actually there.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone

from rubik_core.brand import PRODUCT_NAME
from rubik_core.crm_sync.service import (
    STATUS_APPLIED,
    STATUS_APPLYING,
    STATUS_DISMISSED,
    STATUS_FAILED,
    STATUS_PENDING,
)
from rubik_core.crm_sync.validators import _validate_amount, _validate_closedate
from rubik_core.database.models import CrmSyncSuggestion
from rubik_core.retrieval.hubspot_write import (
    CONTACT_UPDATE_WRITABLE,
    CONTACT_WRITABLE,
    DEAL_CREATE_WRITABLE,
    DEAL_UPDATE_WRITABLE,
    HubSpotWriteClient,
    HubSpotWriteError,
    HubSpotWriteRefused,
    friendly_write_error,
)

logger = logging.getLogger(__name__)

# Statuses an approval may act on. `failed` is included so a transient HubSpot
# error is retryable from the UI without a re-scan.
_CLAIMABLE = (STATUS_PENDING, STATUS_FAILED)

_WHITELIST_BY_CHANGE = {
    "create_contact": CONTACT_WRITABLE,
    "update_contact": CONTACT_UPDATE_WRITABLE,
    "create_deal": DEAL_CREATE_WRITABLE,
    "update_deal": DEAL_UPDATE_WRITABLE,
}

_LABEL_BY_CHANGE = {
    "create_contact": "the contact",
    "update_contact": "the contact",
    "create_deal": "the deal",
    "update_deal": "the deal",
}


class SuggestionConflict(RuntimeError):
    """The suggestion is not in a state this action can act on (HTTP 409)."""


class SuggestionInvalid(ValueError):
    """The caller's edit was rejected (HTTP 422)."""


def _after_values(row: CrmSyncSuggestion) -> dict[str, str]:
    props = (row.payload or {}).get("properties") or {}
    return {k: v.get("after") for k, v in props.items() if isinstance(v, dict)}


def _claim(
    db, suggestion_id: str, actor_email: str, target_status: str
) -> CrmSyncSuggestion:
    """CAS the row into ``target_status``. Raises SuggestionConflict on a miss."""
    updated = (
        db.query(CrmSyncSuggestion)
        .filter(CrmSyncSuggestion.id == suggestion_id)
        .filter(CrmSyncSuggestion.user_email == actor_email)
        .filter(CrmSyncSuggestion.status.in_(_CLAIMABLE))
        .update(
            {
                "status": target_status,
                "applied_by": actor_email,
                "updated_at": datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    if not updated:
        raise SuggestionConflict(
            "This suggestion was already handled — refresh to see its current state."
        )
    db.commit()
    row = (
        db.query(CrmSyncSuggestion)
        .filter(CrmSyncSuggestion.id == suggestion_id)
        .first()
    )
    if row is None:  # pragma: no cover — the CAS just matched it
        raise SuggestionConflict("Suggestion disappeared mid-apply.")
    return row


def _apply_edits(row: CrmSyncSuggestion, edited_after: dict | None) -> dict[str, str]:
    """Merge the AE's edits over the proposed values, whitelist-checked.

    An edited value is still a value going to the CRM, so it passes the same
    property whitelist as anything the model proposed — the edit box is not a
    way around the gates.
    """
    values = _after_values(row)
    if not edited_after:
        return values
    whitelist = _WHITELIST_BY_CHANGE.get(row.change_type, frozenset())
    unknown = sorted(set(edited_after) - set(whitelist))
    if unknown:
        raise SuggestionInvalid(f"Cannot edit these fields: {', '.join(unknown)}")
    for key, value in edited_after.items():
        text = "" if value is None else str(value).strip()
        if not text:
            raise SuggestionInvalid(f"{key} cannot be blank.")
        # Hand-typed values get the same normalisation the AI-proposed ones
        # already passed, so "$45,000" reaches HubSpot as a number and an
        # implausible close date is caught here rather than by the API.
        try:
            if key == "amount":
                text = _validate_amount(text)
            elif key == "closedate":
                text = _validate_closedate(text, datetime.now(timezone.utc))
        except ValueError as exc:
            raise SuggestionInvalid(f"{key}: {exc}") from exc
        values[key] = text
    return values


def _check_drift(client: HubSpotWriteClient, row: CrmSyncSuggestion, db) -> None:
    """Refuse a stage move if the deal moved since the scan proposed it.

    Only ``dealstage`` blocks. Other fields are last-writer-wins on purpose: if
    an AE approves a corrected phone number, a concurrent edit elsewhere isn't
    a reason to refuse. A stage is different — approving "move to Proposal" is
    a judgement about where the deal is *now*.
    """
    if row.change_type != "update_deal":
        return
    stage = ((row.payload or {}).get("properties") or {}).get("dealstage")
    if not isinstance(stage, dict):
        return

    live = client.get_object("deals", row.hubspot_object_id, ["dealstage"])
    live_stage = (live.get("properties") or {}).get("dealstage")
    expected = stage.get("before")
    if str(live_stage or "") == str(expected or ""):
        return

    # Re-anchor the card on reality, then make the AE look again. Deep-copied
    # first: mutating the nested dict in place would leave the assignment below
    # comparing equal to itself, so SQLAlchemy would emit no UPDATE for this
    # JSON column and the card would keep rendering the stale "before".
    payload = deepcopy(row.payload or {})
    payload.setdefault("properties", {})["dealstage"] = {
        "before": str(live_stage or "") or None,
        "after": stage.get("after"),
    }
    row.payload = payload
    row.status = STATUS_PENDING
    row.updated_at = datetime.utcnow()
    db.commit()
    raise SuggestionConflict(
        f"This deal's stage changed in HubSpot since {PRODUCT_NAME} suggested the "
        "update. The suggestion has been refreshed — take another look before "
        "approving."
    )


def _write(client: HubSpotWriteClient, row: CrmSyncSuggestion, values: dict) -> dict:
    change = row.change_type
    payload = row.payload or {}

    if change == "create_contact":
        return client.create_contact(values, owner_email=row.user_email)
    if change == "update_contact":
        return client.update_contact(row.hubspot_object_id, values)
    if change == "create_deal":
        assoc = payload.get("associations") or {}
        return client.create_deal(
            values,
            owner_email=row.user_email,
            company_id=assoc.get("company_id"),
            contact_ids=list(assoc.get("contact_ids") or []),
        )
    if change == "update_deal":
        return client.update_deal(
            row.hubspot_object_id, values, acting_owner_email=row.user_email
        )
    raise SuggestionInvalid(f"Unsupported change type {change!r}")


def apply_suggestion(
    db,
    suggestion_id: str,
    actor_email: str,
    *,
    edited_after: dict | None = None,
    client: HubSpotWriteClient | None = None,
) -> CrmSyncSuggestion:
    """Approve and write one suggestion. Returns the updated row."""
    from backend.audit import record_audit

    actor_email = (actor_email or "").lower()
    row = _claim(db, suggestion_id, actor_email, STATUS_APPLYING)
    client = client or HubSpotWriteClient()
    label = _LABEL_BY_CHANGE.get(row.change_type, "the record")

    try:
        values = _apply_edits(row, edited_after)
        _check_drift(client, row, db)
        response = _write(client, row, values)
    except (SuggestionConflict, SuggestionInvalid):
        # Not a write failure — put the card back so the AE can act on it again.
        row.status = STATUS_PENDING
        row.updated_at = datetime.utcnow()
        db.commit()
        raise
    except (HubSpotWriteRefused, HubSpotWriteError) as exc:
        message = friendly_write_error(label, exc)
        row.status = STATUS_FAILED
        row.error = message
        row.updated_at = datetime.utcnow()
        db.commit()
        record_audit(
            db,
            actor_email=actor_email,
            action="CRM_SYNC_APPLY",
            status="failure",
            target=row.id,
            details={
                "change_type": row.change_type,
                "hubspot_object_type": row.hubspot_object_type,
                "hubspot_id": row.hubspot_object_id,
                "error": message,
            },
        )
        logger.warning("[crm-sync] apply failed for %s: %s", row.id, exc)
        return row

    before = {
        k: v.get("before")
        for k, v in ((row.payload or {}).get("properties") or {}).items()
        if isinstance(v, dict)
    }
    hubspot_id = str(response.get("id") or row.hubspot_object_id or "") or None
    row.status = STATUS_APPLIED
    row.error = None
    row.hubspot_object_id = hubspot_id
    row.applied_at = datetime.utcnow()
    row.applied_by = actor_email
    row.hubspot_response = {"id": hubspot_id, "mode": response.get("_mode")}
    row.updated_at = datetime.utcnow()
    db.commit()

    record_audit(
        db,
        actor_email=actor_email,
        action="CRM_SYNC_APPLY",
        target=row.id,
        details={
            "change_type": row.change_type,
            "hubspot_object_type": row.hubspot_object_type,
            "hubspot_id": hubspot_id,
            "mode": response.get("_mode"),
            "before": before,
            "after": values,
        },
    )
    logger.info(
        "[crm-sync] %s applied %s to %s (%s)",
        actor_email,
        row.change_type,
        hubspot_id,
        response.get("_mode"),
    )
    return row


def dismiss_suggestion(
    db, suggestion_id: str, actor_email: str, *, reason: str | None = None
) -> CrmSyncSuggestion:
    """Dismiss one suggestion. Suppressed from re-scans for the configured window."""
    from backend.audit import record_audit

    actor_email = (actor_email or "").lower()
    row = _claim(db, suggestion_id, actor_email, STATUS_DISMISSED)
    if reason:
        row.payload = {**(row.payload or {}), "dismiss_reason": reason[:500]}
        db.commit()

    record_audit(
        db,
        actor_email=actor_email,
        action="CRM_SYNC_DISMISS",
        target=row.id,
        details={
            "change_type": row.change_type,
            "hubspot_object_type": row.hubspot_object_type,
            "hubspot_id": row.hubspot_object_id,
            "reason": reason,
        },
    )
    return row
