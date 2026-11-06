"""Tests for the VTO Excel uploader and per-line confidentiality.

Three layers:
1. Pure parser/build unit tests on ``rubik_core.ingestion.eos.vto_xlsx``
   (no app, no DB).
2. Endpoint tests for ``GET /api/eos/vto/template`` and
   ``POST /api/eos/vto/upload`` — Exec/admin gating (writes), and that an
   upload publishes a new active version while archiving the prior one.
3. Endpoint tests for the read side — versions/sections are open to any
   authenticated user, but non-exec/admin callers only ever see the active
   version and only its non-confidential bullets.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.ingestion.eos.vto_xlsx import (
    TEMPLATE_SECTIONS,
    XLSX_MEDIA_TYPE,
    Bullet,
    ParsedVto,
    VtoParseError,
    build_vto_template_workbook,
    bullets_to_content,
    parse_vto_workbook,
)

client = TestClient(app)


# -- helpers ----------------------------------------------------------------


def _make_workbook(
    rows: list[tuple],
    *,
    sheet_name: str = "VTO",
    header: tuple | None = ("Category", "Section", "Content", "Confidential"),
) -> bytes:
    """Build a minimal .xlsx with the given rows for parser/upload tests."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    if header is not None:
        ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _profile(email: str, roles: list[str] | None = None) -> UserProfile:
    now = "2026-06-22T00:00:00"
    stored = UserRegistry().get_user(email)
    return UserProfile(
        id=stored.id if stored else email.split("@")[0],
        email=email,
        name=stored.name if stored else email.split("@")[0].title(),
        roles=roles or (stored.roles if stored else ["user"]),
        department=stored.department if stored else "general",
        created_at=now,
        updated_at=now,
        is_exec=bool(stored.is_exec) if stored else False,
    )


def _as_user(email: str, roles: list[str] | None = None):
    def _factory():
        return _profile(email, roles)

    app.dependency_overrides[get_current_user] = _factory


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_eos_vto_upload.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    reg = UserRegistry()
    for email, roles in [
        ("admin@example.com", ["admin"]),
        ("exec@example.com", ["user"]),
        ("regular@example.com", ["user"]),
    ]:
        if not reg.get_user(email):
            reg.register_user(
                email=email, name=email.split("@")[0], id=email, roles=roles
            )
    yield
    app.dependency_overrides.clear()


def _seed_org():
    """Returns the org's UUID (the eos tables use sa.Uuid PKs/FKs, so DB-layer
    inserts and filters must use UUID objects, not strings)."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Organization

    db = SessionLocal()
    try:
        org = Organization(name="Acme", created_by="admin@example.com")
        db.add(org)
        db.commit()
        db.refresh(org)
        return org.id
    finally:
        db.close()


def _seed_active_version(org_id, name: str = "V1"):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import VTOVersion

    db = SessionLocal()
    try:
        v = VTOVersion(
            organization_id=org_id,
            version_name=name,
            is_active=True,
            status="published",
            created_by="admin@example.com",
        )
        db.add(v)
        db.commit()
        db.refresh(v)
        return v.id
    finally:
        db.close()


def _seed_draft_version(org_id, name: str = "Draft"):
    """A version that is neither active nor archived — non-exec callers
    must never be able to reach it or its sections."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import VTOVersion

    db = SessionLocal()
    try:
        v = VTOVersion(
            organization_id=org_id,
            version_name=name,
            is_active=False,
            status="draft",
            created_by="admin@example.com",
        )
        db.add(v)
        db.commit()
        db.refresh(v)
        return v.id
    finally:
        db.close()


def _seed_section(version_id, section_type: str, bullets: list[Bullet]):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import VTOSection

    db = SessionLocal()
    try:
        s = VTOSection(
            vto_version_id=version_id,
            section_type=section_type,
            content=bullets_to_content(bullets),
            created_by="admin@example.com",
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id
    finally:
        db.close()


# -- parser / build unit tests ----------------------------------------------


def test_build_template_has_confidential_column_and_six_sections():
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(build_vto_template_workbook()))
    assert "VTO" in wb.sheetnames
    ws = wb["VTO"]
    assert [ws.cell(row=1, column=c).value for c in (1, 2, 3, 4)] == [
        "Category",
        "Section",
        "Content",
        "Confidential",
    ]
    labels_in_sheet = {
        ws.cell(row=r, column=2).value
        for r in range(2, ws.max_row + 1)
        if ws.cell(row=r, column=2).value
    }
    assert labels_in_sheet == {s.label for s in TEMPLATE_SECTIONS}
    # Each section gets more than one scaffold row so the one-row-per-bullet
    # layout is obvious.
    core_values_rows = [
        r
        for r in range(2, ws.max_row + 1)
        if ws.cell(row=r, column=2).value == "Core Values"
    ]
    assert len(core_values_rows) > 1


def test_template_labels_match_vto_labels():
    """The uploader labels must match the labels Cubie's ingestion uses, so
    the template can't drift from the rest of the system."""
    from rubik_core.ingestion.eos.extractor import _VTO_LABELS

    for sec in TEMPLATE_SECTIONS:
        assert _VTO_LABELS[sec.section_type] == sec.label


def test_parse_valid_two_sections():
    data = _make_workbook(
        [
            ("Vision", "Core Values", "Integrity", "No"),
            ("Vision", "Core Focus", "Help teams ship faster", "No"),
        ]
    )
    parsed = parse_vto_workbook(data)
    assert isinstance(parsed, ParsedVto)
    assert parsed.sections == {
        "values": [Bullet("Integrity", False)],
        "focus": [Bullet("Help teams ship faster", False)],
    }
    assert parsed.ignored_labels == []


def test_parse_multiline_splits_bullets_sharing_row_confidential():
    data = _make_workbook(
        [
            (
                "Vision",
                "Core Values",
                "Integrity\nTransparency\n  Accountability  ",
                "Yes",
            )
        ]
    )
    parsed = parse_vto_workbook(data)
    assert parsed.sections["values"] == [
        Bullet("Integrity", True),
        Bullet("Transparency", True),
        Bullet("Accountability", True),
    ]


def test_parse_duplicate_section_rows_merge_preserving_per_row_confidential():
    data = _make_workbook(
        [
            ("Vision", "Core Values", "Integrity", "No"),
            ("Vision", "Core Values", "Transparency", "Yes"),
        ]
    )
    parsed = parse_vto_workbook(data)
    assert parsed.sections["values"] == [
        Bullet("Integrity", False),
        Bullet("Transparency", True),
    ]


def test_parse_unknown_label_ignored():
    data = _make_workbook(
        [
            ("Vision", "Core Values", "Integrity", "No"),
            ("Other", "Made Up Section", "should be ignored", "No"),
        ]
    )
    parsed = parse_vto_workbook(data)
    assert parsed.sections == {"values": [Bullet("Integrity", False)]}
    assert parsed.ignored_labels == ["Made Up Section"]


def test_parse_blank_raises():
    data = _make_workbook(
        [("Vision", "Core Values", "", "No"), ("Vision", "Core Focus", None, "No")]
    )
    with pytest.raises(VtoParseError):
        parse_vto_workbook(data)


def test_parse_garbage_raises():
    with pytest.raises(VtoParseError):
        parse_vto_workbook(b"this is not a spreadsheet")


def test_parse_case_insensitive_label_sheet_and_confidential_header():
    data = _make_workbook(
        [("vision", "core values", "Integrity", "yes")],
        sheet_name="vto",
        header=("category", "section", "content", "confidential"),
    )
    parsed = parse_vto_workbook(data)
    assert parsed.sections == {"values": [Bullet("Integrity", True)]}


def test_parse_blank_confidential_defaults_to_open():
    data = _make_workbook([("Vision", "Core Values", "Integrity", "")])
    parsed = parse_vto_workbook(data)
    assert parsed.sections["values"] == [Bullet("Integrity", False)]

    data_none_cell = _make_workbook([("Vision", "Core Values", "Integrity", None)])
    parsed_none = parse_vto_workbook(data_none_cell)
    assert parsed_none.sections["values"] == [Bullet("Integrity", False)]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("No", False),
        ("no", False),
        ("N", False),
        ("open", False),
        ("false", False),
        ("0", False),
        ("Yes", True),
        ("yes", True),
        ("Y", True),
        ("confidential", True),
        ("true", True),
        ("1", True),
    ],
)
def test_parse_confidential_recognized_vocabulary(raw, expected):
    data = _make_workbook([("Vision", "Core Values", "Integrity", raw)])
    parsed = parse_vto_workbook(data)
    assert parsed.sections["values"] == [Bullet("Integrity", expected)]


def test_parse_confidential_unrecognized_value_raises():
    data = _make_workbook([("Vision", "Core Values", "Integrity", "Confidental")])
    with pytest.raises(VtoParseError, match="Confidential"):
        parse_vto_workbook(data)


def test_parse_missing_confidential_column_raises_specific_error():
    """An old (pre-this-feature) 3-column upload must fail with a message
    naming the Confidential column specifically, not a generic
    Section/Content error — the exec needs to know exactly what's stale."""
    data = _make_workbook(
        [("Vision", "Core Values", "Integrity")],
        header=("Category", "Section", "Content"),
    )
    with pytest.raises(VtoParseError, match="Confidential"):
        parse_vto_workbook(data)


# -- endpoint: template -----------------------------------------------------


def test_template_download_requires_exec():
    _as_user("regular@example.com")
    r = client.get("/api/eos/vto/template")
    assert r.status_code == 403


def test_exec_can_download_template():
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")
    r = client.get("/api/eos/vto/template")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
    # The bytes are a real, parseable workbook.
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(r.content))
    assert "VTO" in wb.sheetnames


# -- endpoint: read gating (per-line confidentiality) ------------------------


def test_regular_user_can_list_versions_sees_only_active():
    org_id = _seed_org()
    active_id = _seed_active_version(org_id, name="Active")
    _seed_draft_version(org_id, name="Draft")
    _as_user("regular@example.com")
    r = client.get("/api/eos/vto/versions")
    assert r.status_code == 200
    assert {v["id"] for v in r.json()} == {str(active_id)}


def test_admin_can_list_draft_and_active_versions():
    org_id = _seed_org()
    active_id = _seed_active_version(org_id, name="Active")
    draft_id = _seed_draft_version(org_id, name="Draft")
    _as_user("admin@example.com", roles=["admin"])
    r = client.get("/api/eos/vto/versions")
    assert {v["id"] for v in r.json()} == {str(active_id), str(draft_id)}


def test_regular_user_can_get_active_version():
    org_id = _seed_org()
    active_id = _seed_active_version(org_id)
    _as_user("regular@example.com")
    r = client.get(f"/api/eos/vto/versions/{active_id}")
    assert r.status_code == 200


def test_regular_user_gets_404_on_draft_version():
    org_id = _seed_org()
    draft_id = _seed_draft_version(org_id)
    _as_user("regular@example.com")
    r = client.get(f"/api/eos/vto/versions/{draft_id}")
    assert r.status_code == 404


def test_regular_user_gets_404_on_draft_versions_sections():
    """Guards against a non-exec caller enumerating a not-yet-published
    version's id and reading its (even open) bullets early."""
    org_id = _seed_org()
    draft_id = _seed_draft_version(org_id)
    _seed_section(draft_id, "values", [Bullet("Not published yet", False)])
    _as_user("regular@example.com")
    r = client.get(f"/api/eos/vto/versions/{draft_id}/sections")
    assert r.status_code == 404


def test_regular_user_sees_only_open_bullets_in_sections():
    org_id = _seed_org()
    active_id = _seed_active_version(org_id)
    _seed_section(
        active_id,
        "values",
        [Bullet("Integrity", False), Bullet("$10M secret plan", True)],
    )
    _as_user("regular@example.com")
    r = client.get(f"/api/eos/vto/versions/{active_id}/sections")
    assert r.status_code == 200
    values = next(s for s in r.json() if s["section_type"] == "values")
    assert values["content"] == [{"text": "Integrity", "confidential": False}]


def test_exec_sees_open_and_confidential_bullets():
    org_id = _seed_org()
    active_id = _seed_active_version(org_id)
    _seed_section(
        active_id,
        "values",
        [Bullet("Integrity", False), Bullet("$10M secret plan", True)],
    )
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")
    r = client.get(f"/api/eos/vto/versions/{active_id}/sections")
    assert r.status_code == 200
    values = next(s for s in r.json() if s["section_type"] == "values")
    assert values["content"] == [
        {"text": "Integrity", "confidential": False},
        {"text": "$10M secret plan", "confidential": True},
    ]


def test_legacy_plain_string_content_hidden_from_regular_user_but_visible_to_exec():
    """Bullets published before per-line confidentiality existed are plain
    strings, not `{text, confidential}` dicts. They must normalize to
    confidential=True so they don't leak the moment this ships, and stay
    visible to Exec/admin (who could already see them) until republished."""
    org_id = _seed_org()
    active_id = _seed_active_version(org_id)
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import VTOSection

    db = SessionLocal()
    try:
        db.add(
            VTOSection(
                vto_version_id=active_id,
                section_type="values",
                content=["Legacy bullet from before this feature"],
                created_by="admin@example.com",
            )
        )
        db.commit()
    finally:
        db.close()

    _as_user("regular@example.com")
    r = client.get(f"/api/eos/vto/versions/{active_id}/sections")
    values = next(s for s in r.json() if s["section_type"] == "values")
    assert values["content"] == []

    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")
    r = client.get(f"/api/eos/vto/versions/{active_id}/sections")
    values = next(s for s in r.json() if s["section_type"] == "values")
    assert values["content"] == [
        {"text": "Legacy bullet from before this feature", "confidential": True}
    ]


def test_exec_can_read_versions_and_sections():
    org_id = _seed_org()
    version_id = _seed_active_version(org_id)
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")
    assert client.get("/api/eos/vto/versions").status_code == 200
    assert client.get(f"/api/eos/vto/versions/{version_id}").status_code == 200
    assert client.get(f"/api/eos/vto/versions/{version_id}/sections").status_code == 200


def test_admin_can_read_versions():
    _seed_org()
    _as_user("admin@example.com", roles=["admin"])
    assert client.get("/api/eos/vto/versions").status_code == 200


# -- endpoint: upload (Exec/admin-only write) --------------------------------


def test_upload_requires_exec():
    _seed_org()
    _as_user("regular@example.com")
    data = _make_workbook([("Vision", "Core Values", "Integrity", "No")])
    r = client.post(
        "/api/eos/vto/upload",
        files={"file": ("vto.xlsx", data, XLSX_MEDIA_TYPE)},
    )
    assert r.status_code == 403


def test_upload_rejects_non_xlsx():
    _seed_org()
    _as_user("admin@example.com", roles=["admin"])
    r = client.post(
        "/api/eos/vto/upload",
        files={"file": ("vto.xlsx", b"not a spreadsheet", XLSX_MEDIA_TYPE)},
    )
    assert r.status_code == 400


def test_upload_without_org_returns_400():
    _as_user("admin@example.com", roles=["admin"])
    data = _make_workbook([("Vision", "Core Values", "Integrity", "No")])
    r = client.post(
        "/api/eos/vto/upload",
        files={"file": ("vto.xlsx", data, XLSX_MEDIA_TYPE)},
    )
    assert r.status_code == 400
    assert "organization" in r.json()["detail"].lower()


def test_exec_upload_creates_new_active_version_and_archives_prior():
    org_id = _seed_org()
    prior_id = _seed_active_version(org_id, name="V1")
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")

    data = _make_workbook(
        [
            ("Vision", "Core Values", "Integrity", "No"),
            ("Vision", "Core Values", "Transparency", "No"),
            ("Traction", "Quarterly Rocks", "Ship the VTO uploader", "No"),
            ("Traction", "Quarterly Rocks", "$10M pipeline target", "Yes"),
            ("Other", "Bogus", "ignored", "No"),
        ]
    )
    r = client.post(
        "/api/eos/vto/upload",
        files={"file": ("vto.xlsx", data, XLSX_MEDIA_TYPE)},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert sorted(body["sections_set"]) == ["quarterly_rocks", "values"]
    assert body["ignored_labels"] == ["Bogus"]
    assert body["is_active"] is True
    assert body["status"] == "published"

    # DB-level checks: prior archived, exactly one active, sections written
    # as plain JSON-serializable dicts (not `Bullet` dataclass instances —
    # storing those directly would raise at db.commit() time).
    import json

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import VTOSection, VTOVersion

    db = SessionLocal()
    try:
        prior = db.query(VTOVersion).filter(VTOVersion.id == prior_id).first()
        assert prior.is_active is False
        assert prior.status == "archived"

        active = (
            db.query(VTOVersion)
            .filter(VTOVersion.organization_id == org_id, VTOVersion.is_active == True)  # noqa: E712
            .all()
        )
        assert len(active) == 1
        new_version = active[0]
        assert new_version.id != prior_id

        sections = (
            db.query(VTOSection)
            .filter(VTOSection.vto_version_id == new_version.id)
            .all()
        )
        by_type = {
            (
                s.section_type.value
                if hasattr(s.section_type, "value")
                else s.section_type
            ): s.content
            for s in sections
        }
        json.dumps(by_type)  # would already have failed at upload time if not
        assert by_type["values"] == [
            {"text": "Integrity", "confidential": False},
            {"text": "Transparency", "confidential": False},
        ]
        assert by_type["quarterly_rocks"] == [
            {"text": "Ship the VTO uploader", "confidential": False},
            {"text": "$10M pipeline target", "confidential": True},
        ]
    finally:
        db.close()


def test_upload_then_active_version_visible_via_list():
    org_id = _seed_org()
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")
    data = _make_workbook([("Vision", "Core Values", "Integrity", "No")])
    up = client.post(
        "/api/eos/vto/upload",
        files={"file": ("vto.xlsx", data, XLSX_MEDIA_TYPE)},
    )
    assert up.status_code == 201, up.text

    versions = client.get("/api/eos/vto/versions").json()
    active = [v for v in versions if v["is_active"]]
    assert len(active) == 1
    sections = client.get(f"/api/eos/vto/versions/{active[0]['id']}/sections").json()
    assert any(s["section_type"] == "values" for s in sections)
