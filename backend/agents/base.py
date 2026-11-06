"""Base contracts for department-pluggable overnight agents.

UC-10 surfaces proactive recommendations on each user's homepage. Today the
only data source is HubSpot (Sales). An HR system, an accounting system and
so on can plug in later. This module defines the shape every plugin must
satisfy so the orchestrator (`backend.agents.overnight.OvernightAgent`) can
treat them uniformly.

Adding a new department:
  1. Create `backend/agents/<dept>_<source>.py` implementing
     `DepartmentAgentPlugin` (e.g. `hr_<system>.py`, `finance_<system>.py`).
  2. Append an instance to `default_plugins()` in `registry.py`.
  3. Done — the orchestrator picks it up; the briefing surface auto-renders
     any rows the plugin emits to `agent_recommendations`.

The recommendation row format is intentionally generic:
  - `type` is a free-form string the plugin owns (`stale_lead`, `pto_pending`,
    `invoice_overdue`, …). The frontend maps it to a friendly label.
  - `evidence` is a JSON dict — plugin-specific cited facts.
  - `lead_id` is misnamed for non-Sales contexts but is just "the entity this
    recommendation is about" (deal_id, employee_id, invoice_id, …). Renamed
    semantically to `entity_id` in the dataclass; the column stays as-is to
    avoid a destructive migration.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from rubik_core.database.models import AgentRecommendation, User

logger = logging.getLogger(__name__)


@dataclass
class Recommendation:
    """In-memory recommendation a plugin produces; persisted by the orchestrator.

    `entity_id` is what the recommendation is about — deal_id for Sales,
    employee_id for HR, invoice_id for Finance. Stored as the table's
    `lead_id` column for backward-compat with the schema written by UC-10.

    The trailing three fields are UC-17 additions:
      - `sources`: per-source provenance list serialized into the
        `agent_recommendations.sources` JSON column. The correlator's
        `CorrelationGroup.to_dict()` is the canonical producer.
      - `correlation_id`: stable group id from the correlator; lets the UI
        link multiple recs (and lets later runs dedupe by entity).
      - `next_action`: the judge's short imperative phrase.
    Legacy mechanical-rule plugins leave the three at their defaults.
    """

    user_email: str
    type: str
    entity_id: str | None
    entity_name: str | None
    text: str
    evidence: dict[str, Any]
    confidence: float
    source: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] | None = None
    correlation_id: str | None = None
    next_action: str | None = None


@runtime_checkable
class DepartmentAgentPlugin(Protocol):
    """A plugin produces recommendations for users in a single department.

    The orchestrator calls `run(db)` once per scheduled tick. The plugin is
    responsible for fetching its own data (BigQuery, REST API, etc.) and
    converting it to `Recommendation` instances. The orchestrator handles
    persistence, deduplication, and error isolation.
    """

    department: str  # "sales" | "hr" | "finance" | …
    name: str  # human-recognizable plugin id, e.g. "sales_hubspot"

    def run(self, db: Session) -> list[Recommendation]:
        """Return zero or more recommendations. May raise — the orchestrator
        catches and records the error against the run, then continues with
        other plugins (one bad plugin doesn't fail the whole run)."""
        ...


# ─── Shared helpers (department-agnostic) ─────────────────────────────────────


def normalize_user_email(email: str | None, db: Session) -> str | None:
    """Match a third-party-system email to a registered User row, case-insensitive.

    Returns None for unknown users so plugins can silently skip orphaned
    recommendations rather than producing dangling FK rows.
    """
    if not email:
        return None
    user = db.query(User).filter(User.email.ilike(email)).first()
    return user.email if user else None


def format_date(value: Any) -> str:
    """Coerce date/datetime/string to ISO `YYYY-MM-DD`."""
    if value is None:
        return "unknown"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def jsonable(value: Any) -> Any:
    """Recursively convert a value to one the JSON column accepts.

    BigQuery returns `Decimal` for NUMERIC columns and `date`/`datetime` for
    timestamps. Postgres' JSON encoder rejects both. Future plugins (an
    accounting or HR system, etc.) will hit the same issue, so the conversion
    lives here rather than being duplicated in every plugin.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return str(value)  # last-resort stringification


DUPLICATE_HORIZON_DAYS = 30


def is_duplicate(
    db: Session, rec: Recommendation, horizon_days: int = DUPLICATE_HORIZON_DAYS
) -> bool:
    """Skip if the exact (user, entity, type) was already emitted recently OR
    has been acted on at any time.

    Three reasons a rec is suppressed:
      1. Same (user, type, entity_id) already written within the horizon.
      2. The previous row was acted on (email drafted, meeting blocked, etc.) —
         the user has already engaged, no point re-emitting noise. This is
         the "hard signal" feedback loop documented in the UC-10 plan.
      3. Dismissed rows count too — dismissal is permanent silence.
    """
    horizon = datetime.utcnow() - timedelta(days=horizon_days)
    q = (
        db.query(AgentRecommendation)
        .filter(
            AgentRecommendation.user_email == rec.user_email,
            AgentRecommendation.type == rec.type,
            AgentRecommendation.lead_id == rec.entity_id,
        )
        .filter(
            # Recent emission OR ever-acted-on (no horizon on acted-on so a
            # rep who handled a lead 60 days ago doesn't get pinged again today).
            (AgentRecommendation.created_at >= horizon)
            | (AgentRecommendation.acted_on_at.isnot(None))
        )
    )
    return db.query(q.exists()).scalar()


def persist_recommendations(
    db: Session,
    recs: list[Recommendation],
    run_id: str,
    plugin_name: str,
) -> tuple[int, int]:
    """Insert non-duplicate recs. Returns (written, skipped_duplicates).

    Tags every row with `source_metadata.plugin = plugin_name` so we can later
    answer "which plugin produced this row" without joins.
    """
    now = datetime.utcnow()
    written = 0
    skipped = 0
    for rec in recs:
        if is_duplicate(db, rec):
            skipped += 1
            continue
        row = AgentRecommendation(
            id=str(uuid.uuid4()),
            user_email=rec.user_email,
            type=rec.type,
            lead_id=rec.entity_id,
            lead_name=rec.entity_name,
            recommendation_text=rec.text,
            evidence=jsonable(rec.evidence),
            confidence=float(rec.confidence) if rec.confidence is not None else 0.0,
            source_metadata=jsonable(
                {
                    **rec.source,
                    "run_id": run_id,
                    "plugin": plugin_name,
                    "generator": "uc10.v1",
                }
            ),
            sources=jsonable(rec.sources) if rec.sources is not None else None,
            correlation_id=rec.correlation_id,
            next_action=rec.next_action,
            created_at=now,
            dismissed_at=None,
        )
        db.add(row)
        written += 1
    db.commit()
    return written, skipped
