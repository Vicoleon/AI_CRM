"""Shared base/mixin and enums for the EOS subdomain.

EOS tables intentionally diverge from the rest of Cubie:
- UUID primary keys (Cubie elsewhere uses String PKs keyed on email/name).
- AuditMixin (created_at/updated_at/created_by/is_deleted) on every row.

This is deliberate — EOS is a self-contained subdomain whose upstream source
(the standalone EOS app's models) was designed this way.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum as PyEnum

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, DateTime, String


class AuditMixin:
    """Audit columns inherited by every EOS table except `eos_events`."""

    id = Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    created_by = Column(String, nullable=True)
    # Author attribution for the *last* write. `created_at`/`created_by`
    # capture the insert; `updated_at` captures *when* a row last changed but
    # nothing recorded *who*. `updated_by` closes that gap so every in-meeting
    # mutation (issue solved, todo flipped, rock progress logged, scorecard
    # value entered, survey submitted) is traceable to a person. Nullable so
    # rows that have never been updated since the migration keep a NULL here.
    updated_by = Column(String, nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)


# --- Enums ---------------------------------------------------------------
# Stored as native PG ENUMs in Postgres; SQLAlchemy falls back to VARCHAR
# on SQLite for local dev. Values mirror the upstream EOS app exactly so a
# future data import (if ever needed) can round-trip without translation.


class VTOSectionType(str, PyEnum):
    VISION = "vision"
    MISSION = "mission"
    VALUES = "values"
    FOCUS = "focus"
    TEN_YEAR_TARGET = "10_year_target"
    THREE_YEAR_PICTURE = "3_year_picture"
    ONE_YEAR_PLAN = "1_year_plan"
    QUARTERLY_ROCKS = "quarterly_rocks"
    ISSUES_LIST = "issues_list"


class RockStatus(str, PyEnum):
    ON_TRACK = "on_track"
    OFF_TRACK = "off_track"
    DONE = "done"


class Visibility(str, PyEnum):
    COMPANY = "company"
    DEPARTMENT = "department"
    INDIVIDUAL = "individual"


class TodoStatus(str, PyEnum):
    PENDING = "pending"
    DONE = "done"


class IssuePriority(str, PyEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IssueStatus(str, PyEnum):
    OPEN = "open"
    SOLVED = "solved"
    ARCHIVED = "archived"


class L10InputKind(str, PyEnum):
    """Discriminator for pre-meeting L10 inputs.

    HEADLINE: people + company news the team should hear ("people
    headlines" + general headlines).
    IDEA: parking-lot / suggestion items.
    """

    HEADLINE = "headline"
    IDEA = "idea"
