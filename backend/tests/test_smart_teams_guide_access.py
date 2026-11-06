"""ST-6 — a guide cannot escape ``can_view_guide`` through the Assets surface.

WHY THIS FILE EXISTS
--------------------
``POST /api/smart-teams/guides/{document_id}/save-to-assets`` used to register a
DAM ``AssetMetadata`` row for an **unpublished draft**, stamped the way every
Cubie-generated doc is stamped (``department=<the caller's>``,
``is_company_wide=False``, ``allowed_emails=[]``). ``GET
/api/assets/{doc_id}/download`` then served those exact bytes to the four
readers the guide router refuses. Measured, in one run, for a ready draft its
subject had not approved:

    reader                                     /guides/{subject}  /docx  assets download
    same-department colleague (ACL rule 6)      404                403    200, 38342 bytes
    admin outside the support set (rule 1)     404                403    200, 38342 bytes
    org-wide exec (rule 1b)                    404                403    200, 38342 bytes
    scoped exec over that department (rule 1c) 404                403    200, 38342 bytes

Every one of those denials is the feature working. The 200 next to it is the
same document, and it is why this file drives ``backend.main.app`` rather than a
private app carrying only the guides router: **the leak is invisible from inside
one router.** Both halves were individually correct. The bug was that they were
mounted in the same app.

The fix was to stop registering the Asset, not to stamp it better, and that is
worth stating because "stamp it better" is the obvious next idea:
:func:`test_no_asset_stamp_can_deny_an_admin_or_an_org_wide_exec` pins the
reason it cannot work. ``has_document_access`` rules 1 and 1b return True for
``is_admin`` and ``sees_all_departments`` before any stamp on the row is read,
and both of those people are denied an unapproved draft on purpose
(``authz.is_support_admin``: seniority must not buy somebody else's raw
material). Requiring ``published`` first would close the draft leak and leave a
company-visible copy that ``unpublish_guide`` cannot retract — and unpublish
must revoke. ST-5 reached this same fork for uploaded official reports and
declined it for the same reason (``rubik_core/smart_teams/uploads.py``,
"Storage, and why this is not an Asset").

Also pinned here: the download route now applies the ``assets:read`` role
permission its sibling metadata read already applied. In the run above the
metadata read 403'd for a caller without it while the *bytes* came back 200 —
the weaker gate on the stronger disclosure.

Real HTTP throughout, against a throwaway SQLite file. The Smart Teams gate is
never overridden — only ``get_current_user`` — so ``require_smart_teams_access``
runs for real, and so does every ACL rule.

Guides are ADVISORY. Nothing here is a measure of competence or fitness for a
role.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import zipfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.role import ResourcePermissions, Role
from backend.tests._rosters import narrow_smart_teams_support
from rubik_core.config import settings
from rubik_core.identifiers import ASSET_TAG_GENERATED

client = TestClient(app)

GUIDES = "/api/smart-teams/guides"
ASSETS = "/api/assets"

OWNER = "guide.owner@acme.example"
COLLEAGUE = "guide.colleague@acme.example"
ADMIN_OUTSIDE = "marketing.admin@acme.example"
ORG_WIDE_EXEC = "chief@acme.example"
SCOPED_EXEC = "sales.vp@acme.example"
SYSTEMS_ADMIN = "systems.admin@acme.example"

#: The owner's department, and therefore the department the old code stamped on
#: the asset. Sales is arbitrary; what matters is that the colleague shares it.
DEPT = "Sales"

#: The reader shapes ``authz.can_view_guide`` exists to refuse, and the ACL rule
#: each of them would have come in through. Parametrised rather than written out
#: four times so a fifth shape is one line.
DENIED_READERS = [
    pytest.param(COLLEAGUE, id="same-department-colleague-rule-6"),
    pytest.param(ADMIN_OUTSIDE, id="admin-outside-the-support-set-rule-1"),
    pytest.param(ORG_WIDE_EXEC, id="org-wide-exec-rule-1b"),
    pytest.param(SCOPED_EXEC, id="scoped-exec-over-that-department-rule-1c"),
]


@pytest.fixture(autouse=True)
def _support_is_the_systems_admins(monkeypatch):
    """Support is handed to the Systems team's admins in Feature Access, so
    ``ADMIN_OUTSIDE`` is an admin that row leaves out (support ships with
    every admin on it)."""
    narrow_smart_teams_support(monkeypatch, "Systems")


def _profile(email: str):
    """The identity ``get_current_user`` would return for one of the shapes."""
    from rubik_core.auth.registry import UserProfile

    people = {
        OWNER: dict(department=DEPT, roles=["user"]),
        COLLEAGUE: dict(department=DEPT, roles=["user"]),
        # Admin, but outside the support set (see ``_support_is_the_systems_admins``):
        # ``is_support_admin`` needs both.
        ADMIN_OUTSIDE: dict(department="Marketing", roles=["admin"]),
        # is_exec + the managed_departments wildcard == sees_all_departments.
        ORG_WIDE_EXEC: dict(
            department="Executive",
            roles=["user"],
            is_exec=True,
            managed_departments=["*"],
        ),
        # An exec who owns the subject's department on the Accountability Chart.
        SCOPED_EXEC: dict(
            department="Marketing",
            roles=["user"],
            is_exec=True,
            managed_departments=[DEPT],
        ),
        SYSTEMS_ADMIN: dict(department="Systems", roles=["admin"]),
    }[email]
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].replace(".", " ").title(),
        created_at="2026-08-20T00:00:00",
        updated_at="2026-08-20T00:00:00",
        **people,
    )


def _login(email: str) -> None:
    app.dependency_overrides[get_current_user] = lambda: _profile(email)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """A throwaway SQLite file for the DAM, the roles and the ST tables.

    Same shape as ``test_assets_rbac.py``: the assets router and the ACL read
    through ``UserRegistry`` / ``DAMRegistry``, which resolve their own sessions
    rather than the injected ``get_db``, so pointing ``settings.DATABASE_URL`` at
    a temp file and reloading the three modules is what actually isolates them.
    ``importlib.reload`` re-executes into the SAME module dict, so functions the
    routers already imported by name pick up the new engine.

    ``CLOUDSQL_CONNECTION_NAME`` is blanked because ``get_engine`` routes on it
    BEFORE it reads ``DATABASE_URL`` — the root ``conftest.py`` blanks it
    session-wide, and this restates it locally so the isolation does not depend
    on collection order.
    """
    db_url = f"sqlite:///{tmp_path / 'st6_guide_access.db'}"
    saved = (
        settings.DATABASE_URL,
        settings.CLOUDSQL_CONNECTION_NAME,
        settings.DB_USER,
        settings.LOCAL_DATA_DIR,
    )
    settings.DATABASE_URL = db_url
    settings.CLOUDSQL_CONNECTION_NAME = None
    settings.DB_USER = None
    settings.LOCAL_DATA_DIR = str(tmp_path)

    from rubik_core.database import db as dbmod

    dbmod._engine = None
    importlib.reload(dbmod)
    from rubik_core.database import repository

    importlib.reload(repository)
    from rubik_core.auth import registry

    importlib.reload(registry)

    from rubik_core.database.models import Base
    from rubik_core.database.smart_teams import SmartTeamsDocument  # noqa: F401

    assert "sqlite" in str(dbmod.get_engine().url), dbmod.get_engine().url
    Base.metadata.create_all(bind=dbmod.get_engine())

    _seed_roles_and_people()
    try:
        yield dbmod
    finally:
        app.dependency_overrides.clear()
        (
            settings.DATABASE_URL,
            settings.CLOUDSQL_CONNECTION_NAME,
            settings.DB_USER,
            settings.LOCAL_DATA_DIR,
        ) = saved
        dbmod._engine = None
        importlib.reload(dbmod)
        importlib.reload(repository)
        importlib.reload(registry)


def _seed_roles_and_people() -> None:
    """Everyone gets ``assets:read``, deliberately.

    The point of this file is which ROWS a reader may have, so the role
    permission must not be what refuses them — a 403 for the wrong reason would
    make the leak look closed. The one test that is about the permission grants
    a role without it explicitly.
    """
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()
    reg.add_role(
        Role(
            name="user",
            permissions={
                "assets": ResourcePermissions(
                    create=False, read=True, update=False, delete=False
                )
            },
        )
    )
    reg.add_role(
        Role(
            name="no_assets",
            permissions={
                "assets": ResourcePermissions(
                    create=False, read=False, update=False, delete=False
                )
            },
        )
    )
    for email in (
        OWNER,
        COLLEAGUE,
        ADMIN_OUTSIDE,
        ORG_WIDE_EXEC,
        SCOPED_EXEC,
        SYSTEMS_ADMIN,
    ):
        profile = _profile(email)
        reg.register_user(
            email=email,
            name=profile.name,
            id=profile.id,
            roles=list(profile.roles),
        )


def _docx_contents(raw: bytes) -> dict[str, bytes]:
    """A .docx's parts, with the ZIP metadata left out.

    A .docx is a ZIP and every entry carries a modification timestamp with
    2-second granularity, so two exports of the same document are byte-identical
    only when both land inside the same window. Comparing raw bytes across two
    separate ``export_docx`` calls is therefore a clock race, not an assertion:
    it produced three of the four main-branch CI failures this cycle, and fires
    more often under ``pytest -n 2``, where two workers share the runner's two
    cores and each call takes longer in wall-clock terms.

    Measured on this renderer: across a 2s gap all 17 entries' timestamps differ
    while every entry's CONTENT is identical. Comparing contents is the same
    assertion without the clock — and strictly stronger than hashing the text,
    since it still covers styles, numbering and relationships.

    Only for comparing two INDEPENDENT renders. Where bytes are stored and
    served back verbatim (the asset-download assertions below), raw byte
    equality is the real property and is left alone.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return {name: z.read(name) for name in sorted(z.namelist())}


def _ready_draft(dbmod) -> tuple[str, bytes]:
    """A ready, UNPUBLISHED personal guide for ``OWNER``. Returns (id, docx).

    Every framework is ``not_taken``, so this is the shortfall document — the
    body says there was not enough to write from. Deliberate: the question here
    is who can obtain the bytes, and the least revealing body that still exports
    is the honest fixture for that.
    """
    from rubik_core.database.smart_teams import SmartTeamsProfile
    from rubik_core.smart_teams import contracts, guide

    session = dbmod.SessionLocal()
    try:
        row = SmartTeamsProfile(
            id="prof-guide-owner-1",
            user_email=OWNER,
            version=1,
            status="current",
            is_current=True,
            scored_at=datetime.utcnow(),
            **{n: contracts.not_taken_envelope(n) for n in contracts.FRAMEWORKS},
        )
        session.add(row)
        session.commit()
        document = guide.ensure_guide_row(session, row).document
        guide.generate_into_row(session, document, row, display_name="Guide Owner")
        session.commit()
        assert document.status == "ready"
        assert not document.published
        data, _ = guide.export_docx(document, display_name="Guide Owner")
        return document.id, data
    finally:
        session.close()


def _register_asset(*, doc_id: str, owner: str, data: bytes, department, wide=False):
    """Register the Asset row the removed endpoint used to write.

    Only :func:`test_no_asset_stamp_can_deny_an_admin_or_an_org_wide_exec` uses
    this, and only to prove why the endpoint is gone rather than restamped.
    """
    from backend.services.generated_docs import store_doc_bytes
    from rubik_core.dam.registry import AssetMetadata, DAMRegistry

    now = datetime.now(timezone.utc)
    DAMRegistry().register_asset(
        AssetMetadata(
            doc_id=doc_id,
            owner=owner,
            status="approved",
            topic="Cubie Docs",
            department=department,
            original_filename="how-to-work-with-guide-owner.docx",
            source_path=store_doc_bytes(doc_id, "guide.docx", data),
            is_company_wide=wide,
            allowed_emails=[],
            content_hash=hashlib.sha256(data).hexdigest(),
            tags=[ASSET_TAG_GENERATED, "smart-teams-guide"],
            processing=False,
            created_at=now,
            updated_at=now,
        )
    )
    return doc_id


# ── 1. The route is gone, and nothing else creates the row ───────────────────


def test_there_is_no_save_to_assets_route(isolated_db):
    """Both halves: the path is not on the app, and a POST to it is not served.

    The openapi assertion is the one that survives a refactor — a re-added
    endpoint under a different function name still shows up as this path.
    """
    document_id, _ = _ready_draft(isolated_db)
    paths = app.openapi()["paths"]
    assert not [p for p in paths if p.endswith("/save-to-assets") and "guides" in p], (
        "a Smart Teams guide must not be registered as a DAM Asset — see the "
        "block where this endpoint used to be in routers/smart_teams_guides.py"
    )
    _login(OWNER)
    assert client.post(f"{GUIDES}/{document_id}/save-to-assets").status_code == 404


def test_no_guide_endpoint_reaches_the_asset_registry(isolated_db):
    """Read, generate, export, approve, unpublish — none of them may register.

    ``save_generated_doc`` is the only door from this app into
    ``AssetMetadata``, so making it explode is a complete check that the guide
    router does not walk through it. Cheaper and less brittle than grepping the
    module for an import.
    """
    document_id, data = _ready_draft(isolated_db)
    _login(OWNER)

    def _boom(*args, **kwargs):
        raise AssertionError("a guide must never be registered as an Asset")

    with patch("backend.services.generated_docs.save_generated_doc", _boom):
        assert client.get(f"{GUIDES}/me").status_code == 200
        assert client.get(f"{GUIDES}/{document_id}/docx").status_code == 200
        assert client.post(f"{GUIDES}/{document_id}/save-to-assets").status_code == 404
        assert client.post(f"{GUIDES}/{document_id}/approve").status_code == 200
        assert client.post(f"{GUIDES}/{document_id}/unpublish").status_code == 200

    from rubik_core.dam.registry import DAMRegistry

    assert (
        DAMRegistry().get_asset_by_content_hash(hashlib.sha256(data).hexdigest())
        is None
    ), "the guide's exact bytes are in the DAM"


# ── 2. The four readers, on every path ───────────────────────────────────────


@pytest.mark.parametrize("reader", DENIED_READERS)
def test_an_unpublished_draft_is_denied_on_every_path(isolated_db, reader):
    """The whole finding, as one assertion per surface.

    The subject does the thing that used to open the hole — finishes a draft,
    does not approve it, and asks for it to be saved to their Assets page. That
    request is what has to be refused; the reader assertions below are what it
    was refused *for*.

    The assets leg is not vacuous: an unrelated asset of the owner's is
    registered first, so the loop below really does exercise
    ``GET /api/assets/{doc_id}/download`` and really does hand this reader
    everything the DAM holds. What it must never hand them is the guide.
    """
    document_id, data = _ready_draft(isolated_db)
    unrelated = _register_asset(
        doc_id="gendoc-unrelated-1",
        owner=OWNER,
        data=b"an ordinary Cubie doc, nothing to do with a guide",
        department=DEPT,
    )

    _login(OWNER)
    saved = client.post(f"{GUIDES}/{document_id}/save-to-assets")
    assert saved.status_code == 404, (
        f"the subject just made an Asset copy of an unapproved draft: {saved.text}"
    )

    _login(reader)
    assert client.get(f"{GUIDES}/{OWNER}").status_code == 404
    assert client.get(f"{GUIDES}/{document_id}/docx").status_code == 403
    assert client.get(f"{GUIDES}/support/{OWNER}").status_code == 403

    # Everything this reader can reach on the assets surface, byte for byte.
    listed = client.get(f"{ASSETS}/")
    assert listed.status_code == 200
    served: list[bytes] = []
    for doc_id in listed.json()["assets"]:
        got = client.get(f"{ASSETS}/{doc_id}/download")
        if got.status_code == 200:
            served.append(got.content)
    assert served, "the download probe never ran — the loop proved nothing"
    assert data not in served
    assert all(
        hashlib.sha256(b).hexdigest() != hashlib.sha256(data).hexdigest()
        for b in served
    )
    # Stated separately so a future zero-asset listing cannot make the loop
    # above pass by finding nothing: this reader really can download from this
    # route. What they cannot download is a guide, because no such row exists.
    assert client.get(f"{ASSETS}/{unrelated}/download").status_code == 200


@pytest.mark.parametrize("reader", DENIED_READERS)
def test_a_published_guide_is_readable_and_still_is_not_an_asset(isolated_db, reader):
    """Publication opens the guide, and opens NOTHING on the assets surface.

    A published guide is company-visible, so all four of these readers get it —
    through the endpoints that carry ``can_view_guide``, which is the point: the
    Asset copy was never what made a guide shareable, and its absence costs
    nothing.
    """
    document_id, data = _ready_draft(isolated_db)
    _login(OWNER)
    assert client.post(f"{GUIDES}/{document_id}/approve").status_code == 200
    # Publication does not re-open Save-to-Assets. It is not that a published
    # guide is too private for an Asset — it is that the Asset would outlive the
    # consent, and the unpublish below is what that costs.
    assert client.post(f"{GUIDES}/{document_id}/save-to-assets").status_code == 404

    _login(reader)
    assert client.get(f"{GUIDES}/{OWNER}").status_code == 200
    assert client.get(f"{GUIDES}/{document_id}/docx").status_code == 200

    from rubik_core.dam.registry import DAMRegistry

    assert (
        DAMRegistry().get_asset_by_content_hash(hashlib.sha256(data).hexdigest())
        is None
    )

    # Retracting closes it again — which an Asset copy would have survived.
    _login(OWNER)
    assert client.post(f"{GUIDES}/{document_id}/unpublish").status_code == 200
    _login(reader)
    assert client.get(f"{GUIDES}/{OWNER}").status_code == 404
    assert client.get(f"{GUIDES}/{document_id}/docx").status_code == 403


# ── 3. The two readers who are supposed to have it ───────────────────────────


def test_the_subject_can_read_and_export_their_own_draft(isolated_db):
    document_id, data = _ready_draft(isolated_db)
    _login(OWNER)
    own = client.get(f"{GUIDES}/me")
    assert own.status_code == 200
    assert own.json()["state"] == "ready"
    assert own.json()["guide"]["published"] is False
    assert own.json()["guide"]["content_md"]
    export = client.get(f"{GUIDES}/{document_id}/docx")
    assert export.status_code == 200
    # Same document, re-rendered: compare contents, not the ZIP's clock.
    assert _docx_contents(export.content) == _docx_contents(data)
    assert client.get(f"{GUIDES}/{OWNER}").status_code == 200


def test_a_systems_admin_can_still_reach_an_unpublished_draft(isolated_db):
    """``is_support_admin`` — admin AND Systems — keeps the support read.

    Closing the Assets hole must not close this: a Systems admin can already see
    the raw answers the draft was generated from, so hiding the draft from them
    would protect nothing and make "why is my guide stuck" unanswerable.
    """
    document_id, _ = _ready_draft(isolated_db)
    _login(SYSTEMS_ADMIN)
    assert client.get(f"{GUIDES}/{OWNER}").status_code == 200
    assert client.get(f"{GUIDES}/{document_id}/docx").status_code == 200
    support = client.get(f"{GUIDES}/support/{OWNER}")
    assert support.status_code == 200
    assert [row["id"] for row in support.json()["guides"]] == [document_id]


# ── 4. Why the row is gone rather than restamped ─────────────────────────────


def test_no_asset_stamp_can_deny_an_admin_or_an_org_wide_exec(isolated_db):
    """The justification for removing the endpoint instead of fixing its stamp.

    This test asserts a leak, on purpose, against an Asset row it registers
    itself — no guide endpoint can produce one any more. It is here because
    "keep Save-to-Assets and stamp the row properly" is the obvious next idea
    and it cannot work:

    * with the department stamped and ``is_company_wide=False``, ACL rule 6
      hands the file to a same-department colleague — that stamp is the GRANT.
    * with the department stripped, the colleague and the scoped exec are
      refused, but rule 1 (``is_admin``) and rule 1b (``sees_all_departments``)
      return True before any stamp is read.

    Both of those last two are denied an unapproved draft by
    ``authz.can_view_guide``/``is_support_admin`` by design. So there is no
    configuration of an ``AssetMetadata`` row that agrees with this feature's
    rule, and the only safe number of rows is zero. If this test ever starts
    failing because ``has_document_access`` grew a real private stamp, that is
    the day Save-to-Assets could be reconsidered.
    """
    _, data = _ready_draft(isolated_db)
    stamped = _register_asset(
        doc_id="gendoc-guide-stamped",
        owner=OWNER,
        data=data,
        department=DEPT,
    )

    _login(COLLEAGUE)
    leaked = client.get(f"{ASSETS}/{stamped}/download")
    assert leaked.status_code == 200
    assert leaked.content == data, (
        "a department-stamped, non-company-wide asset is readable by the "
        "subject's whole department (has_document_access rule 6)"
    )

    from rubik_core.dam.registry import DAMRegistry

    DAMRegistry().update_asset(
        stamped, {"department": None, "is_company_wide": False, "allowed_emails": []}
    )

    _login(COLLEAGUE)
    assert client.get(f"{ASSETS}/{stamped}/download").status_code == 403
    _login(SCOPED_EXEC)
    assert client.get(f"{ASSETS}/{stamped}/download").status_code == 403
    # And the two nobody can stamp out.
    _login(ADMIN_OUTSIDE)
    assert client.get(f"{ASSETS}/{stamped}/download").status_code == 200
    _login(ORG_WIDE_EXEC)
    assert client.get(f"{ASSETS}/{stamped}/download").status_code == 200


# ── 5. The shared assets route: bytes are gated like metadata ────────────────


def test_the_download_path_requires_the_same_permission_as_the_metadata_read(
    isolated_db,
):
    """``assets:read``, on the bytes as well as on the description of them.

    Before this fix ``download_asset`` checked only ``has_document_access``,
    while ``get_asset`` next to it also required ``has_permission(user,
    "assets", "read")`` — so a caller without the role permission was refused
    the metadata and served the file. Owner-owned asset, so
    ``has_document_access`` passes on rule 2 and the permission is the only
    thing under test.
    """
    from rubik_core.auth.registry import UserProfile, UserRegistry

    doc_id = _register_asset(
        doc_id="gendoc-permission-probe",
        owner=OWNER,
        data=b"bytes only a permitted reader may have",
        department=DEPT,
    )
    UserRegistry().register_user(
        email="norole@acme.example",
        name="No Role",
        id="norole",
        roles=["no_assets"],
    )
    unpermitted = UserProfile(
        id="norole",
        email="norole@acme.example",
        name="No Role",
        department=DEPT,
        roles=["no_assets"],
        created_at="2026-08-20T00:00:00",
        updated_at="2026-08-20T00:00:00",
    )
    app.dependency_overrides[get_current_user] = lambda: unpermitted

    metadata = client.get(f"{ASSETS}/{doc_id}")
    download = client.get(f"{ASSETS}/{doc_id}/download")
    assert metadata.status_code == 403
    assert download.status_code == 403, (
        "the bytes must not be more freely available than their metadata"
    )

    # The permitted reader is unaffected: this is a missing check, not a new rule.
    _login(OWNER)
    assert client.get(f"{ASSETS}/{doc_id}").status_code == 200
    assert client.get(f"{ASSETS}/{doc_id}/download").status_code == 200
