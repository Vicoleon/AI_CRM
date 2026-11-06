"""Persistence for council verdicts (core — DB only, no delivery)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from rubik_core.database.models import IngestionCouncilVerdict
from rubik_core.ingestion_council.models import CouncilVerdict
from rubik_core.ingestion_council.report import render_html


def cached_verdict_visible(verdict_json: object) -> bool:
    """Legacy reports and reports with revoked CRM sources stay unavailable."""
    from rubik_core.priorities.cached_visibility import verified_bundle_deals

    if not isinstance(verdict_json, dict):
        return False
    return (
        verified_bundle_deals(
            {
                "schema_version": 2,
                "crm_provenance": verdict_json.get("crm_provenance"),
                "priorities": [],
            }
        )
        is not None
    )


def persist_verdict(
    db: Session, verdict: CouncilVerdict, report_html: str | None = None
) -> IngestionCouncilVerdict:
    """Store a verdict + its rendered HTML report; returns the persisted row.

    ``report_html`` is rendered on demand if not supplied, so callers that don't
    need the HTML separately can just pass the verdict.
    """
    row = IngestionCouncilVerdict(
        id=str(uuid.uuid4()),
        scope=verdict.scope,
        overall_health=verdict.overall_health.value,
        headline=verdict.headline,
        verdict_json=verdict.to_dict(),
        report_html=report_html if report_html is not None else render_html(verdict),
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    return row


def latest_verdicts(
    db: Session, limit: int = 20, scope: str | None = None
) -> list[IngestionCouncilVerdict]:
    q = db.query(IngestionCouncilVerdict)
    if scope:
        q = q.filter(IngestionCouncilVerdict.scope == scope)
    return q.order_by(IngestionCouncilVerdict.created_at.desc()).limit(limit).all()


def get_verdict(db: Session, verdict_id: str) -> IngestionCouncilVerdict | None:
    return (
        db.query(IngestionCouncilVerdict)
        .filter(IngestionCouncilVerdict.id == verdict_id)
        .first()
    )
