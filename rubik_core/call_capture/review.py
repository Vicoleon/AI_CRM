"""Shared predicates for wrapped calls that still need an AE's attention."""

from sqlalchemy import and_, or_, select

from rubik_core.database.call_capture import (
    CallExtraction,
    CallExtractionItem,
    CallSession,
)

CLAIMABLE_ITEM_STATUSES: tuple[str, ...] = ("proposed", "write_failed")


def reviewable_items_exist():
    """Correlated predicate for proposals and failed writes on a session.

    Only the latest run's proposals can be acted on. A failed HubSpot write
    stays retryable even when a newer extraction has no proposals.
    """
    latest = (
        select(CallExtraction.id)
        .where(CallExtraction.call_session_id == CallSession.id)
        .order_by(CallExtraction.created_at.desc(), CallExtraction.id.desc())
        .limit(1)
        .correlate(CallSession)
        .scalar_subquery()
    )
    return (
        select(CallExtractionItem.id)
        .where(
            CallExtractionItem.call_session_id == CallSession.id,
            or_(
                CallExtractionItem.status == "write_failed",
                and_(
                    CallExtractionItem.extraction_id == latest,
                    CallExtractionItem.status == "proposed",
                ),
            ),
        )
        .correlate(CallSession)
        .exists()
    )


def failed_analysis_marker_exists():
    """Find a wrapped extraction failure on SQLite and PostgreSQL.

    ``as_string`` compiles to JSON_EXTRACT on SQLite and ``->>`` on Postgres.
    The marker contains only a timestamp, never the provider's exception.
    """
    return CallSession.attrs["extraction_retry"]["failed_at"].as_string().is_not(None)
