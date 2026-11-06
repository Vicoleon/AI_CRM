"""Pydantic schemas for the EOS subdomain.

Layout mirrors the SQLAlchemy package at `rubik_core/database/eos/` — one
file per domain. The `AuditBase` schema mirrors the SQLAlchemy `AuditMixin`
(UUID id + timestamps + soft-delete flag).

Departments are NOT ported as a schema — they're owned by the app's
existing `departments` router and surfaced through that path. EOS
references them by `department_name: str` (FK to `departments.name`).
"""
