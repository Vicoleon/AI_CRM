"""Smart Teams access control — the company-wide gate and the per-subject rules.

Four separable things are pinned here, because each of them fails silently:

1. **The gate is company-wide, and NOT the copilot.**
   ``require_smart_teams_access`` lets in any employee on the Workspace domain.
   If someone moves the Smart Teams routers under the copilot-gated ``core``
   parent, every team an admin has narrowed out of the copilot can no longer
   take the interview they're nudged weekly to finish.

2. **The company boundary is asserted at the gate, not inherited.** The
   mock-auth branch of ``_authenticate_user`` returns before the Google
   domain check, so ``X-Mock-User`` is a free
   identity — and a local run resolves to the deployed dev Cloud SQL, which
   holds real people's rows. Off-domain identities are therefore tested against
   the gate itself and against every per-subject predicate: ``dev@example.com``
   and ``admin@example.com`` (stock example addresses mock auth signs in),
   ``vendor@outside.example`` (a company's HubSpot portal can host a client
   pipeline) and a plain outsider.

3. **Raw answers are the person's + the support admins'. Nobody else's.**
   Support admins hold the admin role AND the ``smart_teams_support`` feature
   (Admin → Feature Access; every admin by default). The case most likely to
   regress is the exec who is not an admin: ``is_exec`` opens doors elsewhere
   in the app, so it is the flag someone reaches for when a leader "should see
   more". Raw psychometric answers about their reports is the one thing it must
   not buy.

4. **The frontend reads the backend's verdicts.** The nav reads
   ``features.smart_teams`` and the raw-data panel reads ``smart_teams_support``
   from ``/api/users/me``; the client mirrors no rule of its own.

Publication is read against the REAL column, not a guessed one: the tests below
build ``SmartTeamsDocument`` rows in SQLite and hand ``doc.published`` — the
``Boolean`` the schema actually chose — to ``can_view_guide``. An earlier
revision of these predicates compared a string against ``"published"``, which
denied a published guide to every coworker in the company (the entire product
promise) and was cemented by a test asserting ``not is_guide_published(True)``.
Read the flag the model has; never re-derive publication from ``status``, which
is ``pending | ready | failed`` generation state and would publish every guide
the moment it rendered.

Profiles are ADVISORY throughout — collaboration and team staffing only, never
performance review, promotion, or compensation.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.auth import (
    get_current_user,
    require_smart_teams_access,
    user_can_access_full_app,
    user_can_access_smart_teams,
)
from backend.main import app
from backend.tests._rosters import narrow_smart_teams_support, pin_rosters
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy
from rubik_core.smart_teams.authz import (
    can_curate_skill_taxonomy,
    can_view_guide,
    can_view_multi_subject_guide,
    can_view_raw_profile,
    is_employee,
    is_guide_published,
    is_subject,
    is_support_admin,
)

_AUTH_CONTEXT = (
    Path(__file__).resolve().parents[2] / "frontend/src/context/AuthContext.tsx"
)


def _profile(email, *, department="general", roles=None, is_exec=False, managed=None):
    now = "2026-08-19T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at=now,
        updated_at=now,
    )


# The six viewers every rule below is judged against. OWNER is the subject of
# the profile under test; everyone else is looking at somebody else's.
OWNER = _profile("owner@acme.example", department="Compliance")
OTHER_EMPLOYEE = _profile("hr@acme.example", department="HR")
COPILOT_EMPLOYEE = _profile("ae@acme.example", department="Sales")
SUPPORT_ADMIN = _profile(
    "itadmin@acme.example", department="IT", roles=["admin", "user"]
)
EXEC_NOT_ADMIN = _profile(
    "ceo@acme.example", department="Operations", is_exec=True, managed=["*"]
)
IMPERSONATED = _profile("impersonated_user@compliance.local", department="Compliance")

SUBJECT = OWNER.email

EMPLOYEE_VIEWERS = (
    OWNER,
    OTHER_EMPLOYEE,
    COPILOT_EMPLOYEE,
    SUPPORT_ADMIN,
    EXEC_NOT_ADMIN,
)

# Every off-domain identity that has actually reached one of these code paths,
# or trivially can. None of them may read anything, published or not.
#
# * ``dev@example.com`` — a stock example address; mock auth signs in any
#   address it is handed, before the Google domain check.
# * ``admin@example.com`` — a stock example address that can hold the
#   ``admin`` role in a dev database (see the root ``conftest.py``).
# * ``vendor@outside.example`` — a HubSpot portal can host a CLIENT pipeline, and
#   ``meeting_participants`` carries external attendees from synced transcripts,
#   so client-side addresses reach the ADK tool paths (ST-7, ST-9) that call
#   these predicates with no request at all.
# * ``attacker@gmail.com`` — the plain ``X-Mock-User`` case.
OFF_DOMAIN_EMAILS = (
    "dev@example.com",
    "admin@example.com",
    "vendor@outside.example",
    "attacker@gmail.com",
)


def _st():
    """Resolved at call time — other suites ``importlib.reload`` the db modules."""
    from rubik_core.database import smart_teams

    return smart_teams


@pytest.fixture
def st_session(tmp_path):
    """A SQLite session holding the real Smart Teams tables.

    The publication rule is only worth testing against the column the model
    actually declared, so these rows come from ``SmartTeamsDocument`` itself
    rather than from a hand-written stand-in that could agree with a wrong
    predicate.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database.models import Base

    _st()  # ensure the Smart Teams tables are registered on Base.metadata
    engine = create_engine(f"sqlite:///{tmp_path}/st_access.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _guide(session, subject_emails, *, published):
    """Insert one ``st_documents`` row and return it read back from the DB."""
    st = _st()
    kind = "personal_guide" if len(subject_emails) == 1 else "pair_guide"
    versions = {email: 1 for email in subject_emails}
    subject_key, version_key = st.document_cache_keys(kind, subject_emails, versions)
    doc = st.SmartTeamsDocument(
        id=uuid.uuid4().hex,
        kind=kind,
        subject_emails=list(subject_emails),
        subject_key=subject_key,
        profile_versions=versions,
        profile_version_key=version_key,
        title="How to work with them",
        content_md="# Working style\n",
        status="ready",
        published=published,
        # Publication is consent, and ck_st_document_published_needs_approval
        # enforces that in the database — a published row with no approval is
        # not a state the product can reach, so the fixture must not fake one.
        approved_at=datetime(2026, 8, 19, 9, 0) if published else None,
        approved_by=subject_emails[0] if published else None,
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc


# ── 1. the gate: every employee on the domain, and only real ones ────────────


def test_every_signed_in_employee_may_use_smart_teams(monkeypatch):
    """Company-wide, whatever their department — that is the whole point.

    ``department="HR"`` is the load-bearing case: with the copilot narrowed to
    four teams (installed below as an admin would), HR is outside it, and a
    profile the copilot gate rejects must still pass this one.
    """
    pin_rosters(
        monkeypatch,
        {
            "copilot": FeaturePolicy(
                departments=frozenset({"sales", "marketing", "systems", "support"}),
                exec_allowed=True,
                admin_allowed=True,
            )
        },
    )

    for user in EMPLOYEE_VIEWERS:
        assert user_can_access_smart_teams(user), user.email
        assert require_smart_teams_access(user=user) is user

    # The point of the whole arrangement: independent of the copilot.
    assert not user_can_access_full_app(OTHER_EMPLOYEE)
    assert user_can_access_smart_teams(OTHER_EMPLOYEE)


def test_the_impersonation_pseudo_user_is_refused():
    """An admin impersonating a department is nobody, and must stay out.

    Smart Teams rows are keyed by email, so letting this identity in writes an
    interview, a profile and a guide for a person who does not exist — then
    ranks them in Team Builder and counts them on the completion dashboard.
    """
    assert not user_can_access_smart_teams(IMPERSONATED)
    with pytest.raises(HTTPException) as raised:
        require_smart_teams_access(user=IMPERSONATED)
    assert raised.value.status_code == 403
    assert "impersonation" in raised.value.detail.lower()

    assert not is_employee("impersonated_user@compliance.local")
    assert not is_employee("someone@sales.local")
    assert not is_employee("not-an-email")
    assert not is_employee(None)
    assert is_employee("Owner@ACME.example")


def test_an_off_domain_mock_user_is_refused_at_the_gate():
    """The gate asserts the Workspace domain itself, because auth may not have.

    ``ENABLE_MOCK_AUTH`` + a non-GCP ``APP_MODE`` makes ``X-Mock-User`` a free
    identity: that branch of ``_authenticate_user`` returns BEFORE the Google
    domain check and before the no-department lockout, so an arbitrary address
    arrives here as a fully-formed ``UserProfile`` — and a mock ``sysadmin@`` in
    Systems arrives as an admin nobody had to authenticate as. The branch cannot
    run on the deployed service (``backend/main.py`` refuses to boot with
    ``ENABLE_MOCK_AUTH`` under ``APP_MODE=gcp``), but a LOCAL run resolves to the
    deployed dev Cloud SQL, and dev holds real users — so what it would reach is
    real people's interview answers, scores and guides.
    """
    for email in OFF_DOMAIN_EMAILS:
        outsider = _profile(email)
        assert not user_can_access_smart_teams(outsider), email
        with pytest.raises(HTTPException) as raised:
            require_smart_teams_access(user=outsider)
        assert raised.value.status_code == 403, email
        # Different copy from the impersonation refusal: there is nothing for an
        # off-domain caller to switch back to.
        assert "impersonation" not in raised.value.detail.lower(), email
        assert "workspace" in raised.value.detail.lower(), email

    # Even holding the admin role. ``admin@example.com`` really does carry it in
    # the dev database, so this is the live shape, not a hypothetical one.
    mock_admin = _profile(
        "admin@example.com", department="Systems", roles=["admin", "user"]
    )
    assert not user_can_access_smart_teams(mock_admin)
    assert not is_support_admin(mock_admin)


@pytest.mark.parametrize(
    "company, expected",
    [
        ("Acme Corp", "Smart Teams is limited to Acme Corp's Workspace accounts."),
        ("", "Smart Teams is limited to your company's Workspace accounts."),
    ],
)
def test_the_off_domain_refusal_names_the_company(monkeypatch, company, expected):
    """Spec §4.1: the refusal names the workspace's company from its profile,
    and says "your company" when none is saved."""
    from rubik_core.company import profile

    monkeypatch.setattr(
        profile,
        "get_company_profile",
        lambda db=None: profile.CompanyProfile(name=company),
    )
    with pytest.raises(HTTPException) as raised:
        require_smart_teams_access(user=_profile("someone@outside.example"))
    assert raised.value.status_code == 403
    assert raised.value.detail == expected


def test_the_gate_takes_its_identity_only_from_the_authenticated_user():
    """Structural proof that no request-supplied subject can reach this gate.

    Smart Teams holds the private raw answers, and with local mock auth an
    ``X-Mock-User`` header is a free identity — so a gate that read a header, a
    query param or a body field would be a full impersonation primitive over
    the most sensitive data in the app. The dependency therefore takes exactly
    one parameter, and it is ``Depends(get_current_user)``.
    """
    params = inspect.signature(require_smart_teams_access).parameters
    assert list(params) == ["user"], params
    assert params["user"].default.dependency is get_current_user


# ── 2. raw interview answers and scores: the person + support admins ─────────


def test_the_owner_sees_their_own_raw_data():
    assert can_view_raw_profile(OWNER, SUBJECT)
    assert is_subject(OWNER, SUBJECT)
    # users.email is a case-sensitive PK stored verbatim from the Google token,
    # so both sides fold — otherwise a person whose token spells them
    # differently loses access to their own answers.
    assert can_view_raw_profile(OWNER, "  OWNER@ACME.example ")
    assert can_view_raw_profile(_profile("OWNER@ACME.example"), SUBJECT)


def test_another_ordinary_employee_is_denied_raw_data():
    assert not can_view_raw_profile(OTHER_EMPLOYEE, SUBJECT)
    assert not is_subject(OTHER_EMPLOYEE, SUBJECT)


def test_a_copilot_user_is_denied_raw_data():
    """Being in the copilot is unrelated to seeing someone's answers."""
    assert user_can_access_full_app(COPILOT_EMPLOYEE)
    assert not can_view_raw_profile(COPILOT_EMPLOYEE, SUBJECT)


def test_an_admin_sees_raw_data_for_support():
    """The ``smart_teams_support`` default is every admin: a fresh workspace has
    no support team, and its admins are the people who field the questions."""
    assert is_support_admin(SUPPORT_ADMIN)
    assert can_view_raw_profile(SUPPORT_ADMIN, SUBJECT)
    assert can_curate_skill_taxonomy(SUPPORT_ADMIN)


def test_an_admin_with_no_department_is_a_support_admin():
    """Departments are optional; support is judged on the admin role."""
    for department in ("", "general"):
        admin = _profile(
            "admin2@acme.example", department=department, roles=["admin", "user"]
        )
        assert is_support_admin(admin), department
        assert can_view_raw_profile(admin, SUBJECT), department
        assert can_curate_skill_taxonomy(admin), department


def test_a_non_admin_is_never_a_support_admin(monkeypatch):
    """Not by default, and not by any Feature Access row: a row that opens the
    feature to everyone, to the Exec team or to the person's own department
    still needs the admin role on top."""
    pin_rosters(
        monkeypatch,
        {
            "smart_teams_support": FeaturePolicy(
                departments=frozenset({"compliance", "operations"}),
                exec_allowed=True,
                admin_allowed=True,
                everyone=True,
            )
        },
    )
    for viewer in (OWNER, OTHER_EMPLOYEE, COPILOT_EMPLOYEE, EXEC_NOT_ADMIN):
        assert not is_support_admin(viewer), viewer.email
        assert not can_curate_skill_taxonomy(viewer), viewer.email
    assert not can_view_raw_profile(EXEC_NOT_ADMIN, SUBJECT)


def test_a_stored_policy_naming_a_department_admits_only_its_admins(monkeypatch):
    """Admins unticked, IT named: the IT admins support Smart Teams, nobody else."""
    narrow_smart_teams_support(monkeypatch, "IT")
    it_member = _profile("it@acme.example", department="IT")
    hr_admin = _profile("hradmin@acme.example", department="HR", roles=["admin"])
    it_lead_admin = _profile(
        "itlead@acme.example", department="Operations", roles=["admin"], managed=["IT"]
    )

    assert is_support_admin(SUPPORT_ADMIN)
    assert is_support_admin(it_lead_admin)  # manages IT: the department counts
    assert not is_support_admin(hr_admin)
    assert not is_support_admin(it_member)  # in IT, but not an admin
    assert can_curate_skill_taxonomy(SUPPORT_ADMIN)
    assert not can_curate_skill_taxonomy(hr_admin)
    assert not can_view_raw_profile(hr_admin, SUBJECT)


def test_an_exec_who_is_not_an_admin_is_denied_raw_data():
    """THE regression to fear.

    ``is_exec`` opens doors elsewhere in the app, and an
    org-wide exec (``managed_departments == ["*"]``) reads across every
    department elsewhere in the app. None of that may reach someone's interview
    answers: a leader who wants to know how to work with a person reads their
    published guide, the same artifact everyone else reads.
    """
    assert user_can_access_smart_teams(EXEC_NOT_ADMIN)  # can use the feature
    assert not is_support_admin(EXEC_NOT_ADMIN)
    assert not can_view_raw_profile(EXEC_NOT_ADMIN, SUBJECT)

    # Nor does managing the subject's team.
    assert not can_view_raw_profile(
        _profile("boss@acme.example", department="HR", managed=["Compliance"]),
        SUBJECT,
    )


def test_an_unknown_subject_is_denied_to_everyone_including_support():
    """ "Whose raw data?" — "we don't know" is a refusal, never a wildcard.

    A caller that lost its subject on the way (a missing path param, an empty
    row, a lookup that returned nothing) must not become a dump of somebody's
    private answers. Support access is permission to help a NAMED colleague.
    """
    for subject in (None, "", "   ", "not-an-email", "vendor@outside.example"):
        assert not can_view_raw_profile(SUPPORT_ADMIN, subject), subject
        assert not can_view_raw_profile(OWNER, subject), subject
        assert not can_view_guide(SUPPORT_ADMIN, subject, True), subject
        assert not can_view_guide(OTHER_EMPLOYEE, subject, True), subject


def test_the_impersonation_pseudo_user_reads_nobody():
    """Belt and braces: the per-subject helpers re-check it themselves.

    ADK tool code (the ST-7 comparison tool, the ST-9 matcher) reads profiles
    without passing through a FastAPI dependency, so the router gate is not
    reachable from there.
    """
    assert not can_view_raw_profile(IMPERSONATED, SUBJECT)
    assert not can_view_guide(IMPERSONATED, SUBJECT, True)
    # Even asking about "its own" profile, which no interview should ever create.
    assert not can_view_raw_profile(IMPERSONATED, IMPERSONATED.email)


# ── 3. guides: published is company-wide, a draft is the author's ────────────


def test_publication_reads_the_boolean_the_schema_actually_has():
    """``SmartTeamsDocument.published`` is ``Column(Boolean, ...)``, so the rule
    is a boolean rule.

    The regression this pins: comparing the flag against the string
    ``"published"`` returns False for BOTH ``True`` and ``False``, which denies
    every coworker every published guide — the whole product promise — while
    looking like a working consent check. ``1``/``0`` count too, because a
    ``text()`` SELECT or a raw DBAPI tuple bypasses SQLAlchemy's ``Boolean``
    conversion. Everything else fails closed: this is the consent switch, and a
    value we do not recognize must not be guessed at.
    """
    assert is_guide_published(True)
    assert is_guide_published(1)
    assert not is_guide_published(False)
    assert not is_guide_published(0)
    assert not is_guide_published(None)
    # NOT the ``status`` column (pending|ready|failed) and not a status string of
    # any kind — reading generation state as consent publishes every guide the
    # moment it renders.
    for status in ("published", "ready", "draft", "", "PUBLISHED"):
        assert not is_guide_published(status), status


def test_a_published_guide_is_readable_by_every_employee():
    for viewer in EMPLOYEE_VIEWERS:
        assert can_view_guide(viewer, SUBJECT, True), viewer.email


def test_an_unpublished_guide_is_the_authors_alone():
    """Consent is the product here: a draft (or an unpublished retraction) is
    private, and an exec's seniority does not open it."""
    for flag in (False, None, 0):
        assert can_view_guide(OWNER, SUBJECT, flag), flag
        assert not can_view_guide(OTHER_EMPLOYEE, SUBJECT, flag), flag
        assert not can_view_guide(COPILOT_EMPLOYEE, SUBJECT, flag), flag
        assert not can_view_guide(EXEC_NOT_ADMIN, SUBJECT, flag), flag
        # A support admin already sees the raw answers the draft was generated from, so
        # hiding the draft from them protects nothing and blocks support.
        assert can_view_guide(SUPPORT_ADMIN, SUBJECT, flag), flag


def test_a_real_document_row_decides_a_third_party_read(st_session):
    """End to end against the model, not against an assumed column shape.

    Two ``st_documents`` rows, identical but for ``published``, are read back
    from SQLite and their flag handed to ``can_view_guide``. A coworker gets the
    published one and is refused the draft. Also asserts the raw-cursor path,
    since a ``text()`` SELECT hands back ``1``/``0`` rather than ``True``/
    ``False`` and the predicate has to survive both.
    """
    published_doc = _guide(st_session, [SUBJECT], published=True)
    draft_doc = _guide(st_session, [OTHER_EMPLOYEE.email], published=False)

    assert published_doc.published is True
    assert draft_doc.published is False

    # The whole product promise: a colleague can read a published guide.
    assert can_view_guide(OTHER_EMPLOYEE, SUBJECT, published_doc.published)
    assert can_view_guide(EXEC_NOT_ADMIN, SUBJECT, published_doc.published)
    # ...and cannot read one nobody published.
    assert not can_view_guide(OWNER, OTHER_EMPLOYEE.email, draft_doc.published)
    # ...though its own subject still can.
    assert can_view_guide(OTHER_EMPLOYEE, OTHER_EMPLOYEE.email, draft_doc.published)

    raw = dict(
        st_session.execute(sa.text("SELECT id, published FROM st_documents")).fetchall()
    )
    assert can_view_guide(OTHER_EMPLOYEE, SUBJECT, raw[published_doc.id])
    assert not can_view_guide(OWNER, OTHER_EMPLOYEE.email, raw[draft_doc.id])


def test_a_shared_guide_needs_every_subject_to_have_published(st_session):
    """N subjects behind ONE flag is not N consents.

    ``st_documents.subject_emails`` is an ordered list, so the row's own
    ``published`` flag records only that somebody pressed a button — whoever
    asked for the pair guide would otherwise be consenting on their colleague's
    behalf. The rule is per-person, against each subject's OWN personal guide.
    """
    pair = _guide(st_session, [SUBJECT, OTHER_EMPLOYEE.email], published=True)
    subjects = pair.subject_emails
    assert len(subjects) == 2

    both_published = {SUBJECT: True, OTHER_EMPLOYEE.email: True}
    one_missing = {SUBJECT: True, OTHER_EMPLOYEE.email: False}

    assert can_view_multi_subject_guide(COPILOT_EMPLOYEE, subjects, both_published)
    # The mixed case: HR never published, so the pair guide must not become the
    # back door to their profile just because the other subject did publish.
    assert not can_view_multi_subject_guide(COPILOT_EMPLOYEE, subjects, one_missing)
    assert not can_view_multi_subject_guide(COPILOT_EMPLOYEE, subjects, {SUBJECT: True})

    # A subject reads a guide about themselves without needing their own
    # consent — consent is what they give others.
    assert can_view_multi_subject_guide(OTHER_EMPLOYEE, subjects, one_missing)
    # ...but not while the OTHER person is unpublished.
    assert not can_view_multi_subject_guide(
        OTHER_EMPLOYEE, subjects, {SUBJECT: False, OTHER_EMPLOYEE.email: True}
    )

    # A support admin reads it for support, exactly as with a single guide.
    assert can_view_multi_subject_guide(SUPPORT_ADMIN, subjects, one_missing)

    # The row's own flag is deliberately not the rule: it is True above and the
    # denials still hold. Unresolvable subjects deny everyone.
    assert pair.published is True
    assert not can_view_multi_subject_guide(COPILOT_EMPLOYEE, [], both_published)
    assert not can_view_multi_subject_guide(
        COPILOT_EMPLOYEE, [SUBJECT, "vendor@outside.example"], both_published
    )
    assert not can_view_multi_subject_guide(
        _profile("attacker@gmail.com"), subjects, both_published
    )


# ── 4. the company boundary, on the paths that never met a request ───────────


def test_off_domain_addresses_read_nothing_even_when_published():
    """These predicates are the ONLY identity check on the paths that bypass
    FastAPI: ADK tool code (ST-7 comparison, ST-9 matcher) and the cubie-worker
    guide ingest / purge, none of which see a request or a dependency.

    Each address below has actually reached a Smart Teams-adjacent code path or
    exists as a live row, so "an employee-only feature" has to be asserted here
    rather than assumed of the caller.
    """
    for email in OFF_DOMAIN_EMAILS:
        assert not is_employee(email), email
        # As a viewer: a published guide is company-visible, not world-visible.
        assert not can_view_guide(email, SUBJECT, True), email
        assert not can_view_guide(_profile(email), SUBJECT, True), email
        assert not can_view_raw_profile(email, SUBJECT), email
        # As a subject: an external attendee from ``meeting_participants`` is
        # not somebody we hold a profile for, so nobody reads one.
        assert not can_view_guide(OTHER_EMPLOYEE, email, True), email
        assert not can_view_raw_profile(OTHER_EMPLOYEE, email), email
        # Not even about themselves.
        assert not can_view_guide(email, email, True), email
        assert not is_subject(email, email), email


def test_a_bare_email_string_works_as_a_viewer():
    """ADK tools and worker paths hold an email, not a ``UserProfile``.

    A profile-only reader would deny a person their OWN data whenever the caller
    passed the string it actually has — a silent, correct-looking 403 on the
    subject's own guide. A string carries no roles, so it can only ever be the
    subject: support access is a property of a resolved account.
    """
    assert can_view_raw_profile(SUBJECT, SUBJECT)
    assert can_view_guide(SUBJECT, SUBJECT, False)
    assert can_view_guide("  OWNER@ACME.example  ", SUBJECT, False)
    assert is_subject(SUBJECT, SUBJECT)

    # A string viewer is still only an employee, never a support admin.
    assert not is_support_admin(SUPPORT_ADMIN.email)
    assert not can_view_raw_profile(SUPPORT_ADMIN.email, SUBJECT)
    assert not can_view_guide(SUPPORT_ADMIN.email, SUBJECT, False)
    # But it reads a published guide like anyone else on the domain.
    assert can_view_guide(OTHER_EMPLOYEE.email, SUBJECT, True)


# ── 5. the frontend mirror (the CI agreement check) ──────────────────────────


@pytest.fixture
def users_me_db(tmp_path, monkeypatch):
    """A fresh SQLite database behind ``/api/users/me``.

    The endpoint reads roles and feature rows through the process-wide engine,
    so without this the test depended on whichever suite last pointed that
    engine somewhere — and failed whenever that database lacked ``roles``.
    """
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database.models import Base
    from rubik_core.features import clear_cache

    _st()  # register the Smart Teams tables on Base.metadata
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'users_me.db'}"
    )
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    clear_cache()
    yield
    clear_cache()
    dbmod._engine = None


def test_the_frontend_mirror_agrees_with_the_backend(users_me_db):
    """The client reads the SERVER's verdicts and mirrors no rule of its own.

    ``canAccessSmartTeams`` is ``features.smart_teams`` from ``/api/users/me``,
    the same verdict ``user_can_access_smart_teams`` returns (domain check
    included), and ``canViewSmartTeamsRawData`` is ``smart_teams_support`` —
    ``is_support_admin`` evaluated on the server. The per-subject rule stays in
    code there; it is not a roster an admin may widen, and not a list the
    client keeps a copy of.
    """
    ctx = _AUTH_CONTEXT.read_text(encoding="utf-8")

    assert "smartTeams: 'smart_teams'" in ctx
    assert "canAccessSmartTeams" in ctx
    assert "userCanViewSmartTeamsRawData" in ctx
    assert "canViewSmartTeamsRawData" in ctx
    assert "smart_teams_support" in ctx
    # No roster: the client must not have grown a Smart Teams department list
    # or re-grown the copilot one.
    assert "SMART_TEAMS_RAW_DEPARTMENTS" not in ctx
    assert "SMART_TEAMS_DEPARTMENTS" not in ctx
    assert "SMART_TEAMS_ROLLOUT_DEPARTMENTS" not in ctx
    assert "const FULL_APP_PILOT_DEPARTMENTS" not in ctx

    # The server verdicts the client reads agree with the predicates.
    hr_admin = _profile("hr@acme.example", department="HR", roles=["admin"])
    for viewer in (hr_admin, SUPPORT_ADMIN, OTHER_EMPLOYEE):
        app.dependency_overrides[get_current_user] = lambda v=viewer: v
        r = TestClient(app).get("/api/users/me")
        app.dependency_overrides.clear()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["features"]["smart_teams"] is user_can_access_smart_teams(viewer)
        assert body["smart_teams_support"] is is_support_admin(viewer), viewer.email
    assert is_support_admin(hr_admin) and not is_support_admin(OTHER_EMPLOYEE)
