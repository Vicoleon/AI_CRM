"""The Smart Teams schema invariants — the ones the database has to hold.

These tests exist because every invariant below has a failure mode that is
silent rather than loud, and three of them are things application code cannot be
trusted to enforce on its own:

* A second row claiming to be version 3 of somebody's profile makes every
  published guide unauditable, because a guide cites the version it was
  generated from.
* Two rows marked current for the same person means the profile a reader gets
  depends on row order. ``is_current`` uses the True-or-NULL trick to make that
  a database error, and the trick inverts into "one superseded row per person"
  if a writer ever stores ``False`` — so the coercion is tested too.
* An unconfirmed AI-inferred skill treated as fact means telling a project lead
  somebody has expertise nobody ever claimed. The Team Builder's filter is the
  only thing standing between a suggestion and a staffing decision.
* A pair guide served from cache after one of its subjects retook the interview
  is describing a person who has since told us something different.

The email-casing tests are here for a specific scar: ``users.email`` is stored
verbatim from the Google ID token, and a row written with different casing
silently matches nothing. Folding in the ORM is what makes
``uq_st_profile_user_version`` actually mean "once per person" rather than "once
per spelling of that person".

That the migration builds these tables exactly as the models declare them is
pinned for the whole schema by ``test_migrations_baseline.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

SHAY = "shay@acme.example"
EMERY = "ebrooks@acme.example"


def _st():
    """Resolved at call time — other suites importlib.reload the db modules."""
    from rubik_core.database import smart_teams

    return smart_teams


def _wire_sqlite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/st.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)
    return TestSession


def _profile(email: str, version: int, *, current: bool = True, **kw):
    st = _st()
    return st.SmartTeamsProfile(
        id=uuid.uuid4().hex,
        user_email=email,
        version=version,
        status="current" if current else "superseded",
        is_current=True if current else None,
        **kw,
    )


def _current_versions(session, emails):
    """What a guide generator would read to build a fresh cache key."""
    st = _st()
    out = {}
    for email in emails:
        row = (
            session.query(st.SmartTeamsProfile)
            .filter(
                func.lower(st.SmartTeamsProfile.user_email) == email.strip().lower(),
                st.SmartTeamsProfile.is_current.is_(True),
            )
            .one()
        )
        out[email.strip().lower()] = row.version
    return out


# ─────────────────────────── version monotonicity ───────────────────────────


def test_a_version_number_cannot_be_reused_for_the_same_person(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(_profile(SHAY, 1))
        s.commit()

    with TestSession() as s:
        s.add(_profile(SHAY, 1, current=False))
        with pytest.raises(IntegrityError):
            s.commit()


def test_the_same_version_number_is_fine_for_a_different_person(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(_profile(SHAY, 1))
        s.add(_profile(EMERY, 1))
        s.commit()
        assert s.query(_st().SmartTeamsProfile).count() == 2


def test_versions_advance_from_the_max_and_survive_a_retake(tmp_path, monkeypatch):
    """The append-only contract: a retake adds a version, it never edits one."""
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(_profile(SHAY, 1))
        s.commit()

    # Retake: retire the live row, insert max+1.
    with TestSession() as s:
        live = (
            s.query(st.SmartTeamsProfile)
            .filter(st.SmartTeamsProfile.is_current.is_(True))
            .one()
        )
        live.is_current = False  # coerced to NULL — see the coercion test
        live.status = "superseded"
        live.superseded_at = datetime(2026, 8, 19)
        nxt = (
            s.query(func.max(st.SmartTeamsProfile.version))
            .filter(func.lower(st.SmartTeamsProfile.user_email) == SHAY)
            .scalar()
            + 1
        )
        s.add(_profile(SHAY, nxt))
        s.commit()

    with TestSession() as s:
        rows = (
            s.query(st.SmartTeamsProfile).order_by(st.SmartTeamsProfile.version).all()
        )
        assert [r.version for r in rows] == [1, 2]
        # Version 1 is still there, unmutated apart from being retired.
        assert rows[0].status == "superseded"
        assert rows[1].is_current is True


def test_a_differently_cased_email_cannot_smuggle_in_a_duplicate_version(
    tmp_path, monkeypatch
):
    """The case-folding hazard: casing must not create a second identity.

    Without the ORM fold, ``Shay@acme.example`` and ``shay@acme.example``
    are different values, so the uniqueness constraint would happily accept two
    "version 1" rows and the person would end up with a split profile history.
    """
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(_profile(SHAY, 1))
        s.commit()

    with TestSession() as s:
        s.add(_profile("  Shay@ACME.example  ", 1, current=False))
        with pytest.raises(IntegrityError):
            s.commit()


def test_email_is_stored_folded_so_a_func_lower_read_matches(tmp_path, monkeypatch):
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(_profile(" EBrooks@ACME.example ", 1))
        s.commit()

    with TestSession() as s:
        row = s.query(st.SmartTeamsProfile).one()
        assert row.user_email == EMERY
        found = (
            s.query(st.SmartTeamsProfile)
            .filter(func.lower(st.SmartTeamsProfile.user_email) == EMERY)
            .one_or_none()
        )
        assert found is not None


# ──────────────────────── one current version per person ────────────────────


def test_a_person_cannot_have_two_current_profiles(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(_profile(SHAY, 1))
        s.commit()

    with TestSession() as s:
        s.add(_profile(SHAY, 2))  # also current
        with pytest.raises(IntegrityError):
            s.commit()


def test_many_superseded_versions_coexist(tmp_path, monkeypatch):
    """The other half of the True-or-NULL trick, and the reason for it.

    If retired rows carried ``False`` instead of ``NULL`` the same constraint
    would read "at most one superseded version per person" and the third
    retake would fail. The ``@validates`` coercion is what prevents that, so
    this asserts on the stored value as well as the insert succeeding.
    """
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(_profile(SHAY, 1, current=False))
        s.add(_profile(SHAY, 2, current=False))
        s.add(_profile(SHAY, 3, current=False))
        s.add(_profile(SHAY, 4))
        s.commit()

    with TestSession() as s:
        retired = (
            s.query(st.SmartTeamsProfile)
            .filter(st.SmartTeamsProfile.is_current.is_(None))
            .all()
        )
        assert len(retired) == 3
        assert all(r.is_current is None for r in retired)
        assert (
            s.query(st.SmartTeamsProfile)
            .filter(st.SmartTeamsProfile.is_current.is_(True))
            .count()
            == 1
        )


def test_setting_is_current_false_is_coerced_to_null(tmp_path, monkeypatch):
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        row = _profile(SHAY, 1)
        row.is_current = False
        s.add(row)
        s.commit()

    with TestSession() as s:
        assert s.query(st.SmartTeamsProfile).one().is_current is None


# ───────────────────────────── provenance defaults ──────────────────────────


def test_every_framework_defaults_to_the_interview(tmp_path, monkeypatch):
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(_profile(SHAY, 1))
        s.commit()

    with TestSession() as s:
        prov = s.query(st.SmartTeamsProfile).one().provenance
        assert set(prov) == set(st.PROFILE_FRAMEWORKS)
        assert set(prov.values()) == {"interview"}
        assert set(prov.values()) <= set(st.PROVENANCE_SOURCES)


def test_an_upload_merge_rewrites_only_its_own_framework(tmp_path, monkeypatch):
    """Uploading official DISC results must not relabel the other five layers."""
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        prov = st.default_provenance()
        prov["disc"] = "official_upload"
        s.add(
            _profile(
                EMERY,
                1,
                origin="official_upload",
                provenance=prov,
                disc={"d": 9, "i": 4, "s": 2, "c": 7},
            )
        )
        s.commit()

    with TestSession() as s:
        prov = s.query(st.SmartTeamsProfile).one().provenance
        assert prov["disc"] == "official_upload"
        assert prov["big_five"] == "interview"
        assert prov["enneagram"] == "interview"


# ──────────────────── inferred skills are not fact until confirmed ──────────


def _seed_skills(session):
    st = _st()
    for key, name in (("data_privacy", "Data Privacy"), ("qc_audit", "QC Audit")):
        session.add(st.SmartTeamsSkill(skill_key=key, name=name, cluster="Compliance"))


def test_an_unconfirmed_inferred_skill_is_excluded_from_confirmed_skills(
    tmp_path, monkeypatch
):
    """The exact predicate the Team Builder uses, and why it is a boolean.

    ``confirmed`` — not ``source != "inferred"`` — is the filter, because a
    person who has confirmed an inference must count, and an inference nobody
    has looked at must not.
    """
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        _seed_skills(s)
        # Declared: the act of declaring IS the confirmation.
        s.add(
            st.SmartTeamsPersonSkill(
                id=uuid.uuid4().hex,
                user_email=SHAY,
                skill_key="data_privacy",
                level="strong",
                source="declared",
                confirmed=True,
                confirmed_at=datetime(2026, 8, 19),
            )
        )
        # Inferred from a meeting, nobody has said yes yet.
        s.add(
            st.SmartTeamsPersonSkill(
                id=uuid.uuid4().hex,
                user_email=SHAY,
                skill_key="qc_audit",
                level="working",
                source="inferred",
                evidence_ref="meeting-4471",
            )
        )
        s.commit()

    with TestSession() as s:
        rows = (
            s.query(st.SmartTeamsPersonSkill)
            .filter(
                func.lower(st.SmartTeamsPersonSkill.user_email) == SHAY,
                st.SmartTeamsPersonSkill.confirmed.is_(True),
            )
            .all()
        )
        assert [r.skill_key for r in rows] == ["data_privacy"]

        # The suggestion is still there to confirm or deny — excluded, not lost.
        pending = (
            s.query(st.SmartTeamsPersonSkill)
            .filter(
                st.SmartTeamsPersonSkill.source == "inferred",
                st.SmartTeamsPersonSkill.confirmed.is_(False),
            )
            .all()
        )
        assert [r.skill_key for r in pending] == ["qc_audit"]
        assert pending[0].evidence_ref == "meeting-4471"


def test_confirming_an_inference_brings_it_into_the_confirmed_set(
    tmp_path, monkeypatch
):
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        _seed_skills(s)
        s.add(
            st.SmartTeamsPersonSkill(
                id=uuid.uuid4().hex,
                user_email=SHAY,
                skill_key="qc_audit",
                source="inferred",
            )
        )
        s.commit()

    with TestSession() as s:
        row = s.query(st.SmartTeamsPersonSkill).one()
        assert row.confirmed is False  # the column default, not None
        row.confirmed = True
        row.confirmed_at = datetime(2026, 8, 19)
        s.commit()

    with TestSession() as s:
        assert (
            s.query(st.SmartTeamsPersonSkill)
            .filter(st.SmartTeamsPersonSkill.confirmed.is_(True))
            .count()
            == 1
        )


def test_one_row_per_person_and_skill(tmp_path, monkeypatch):
    """An inference for an already-declared skill updates, never stacks."""
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        _seed_skills(s)
        s.add(
            st.SmartTeamsPersonSkill(
                id=uuid.uuid4().hex,
                user_email=SHAY,
                skill_key="data_privacy",
                source="declared",
                confirmed=True,
            )
        )
        s.commit()

    with TestSession() as s:
        s.add(
            st.SmartTeamsPersonSkill(
                id=uuid.uuid4().hex,
                user_email="Shay@ACME.example",
                skill_key="data_privacy",
                source="inferred",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


# ──────────────────────── guide cache + staleness detection ─────────────────


def _document(kind, subjects, versions, **kw):
    st = _st()
    subject_key, version_key = st.document_cache_keys(kind, subjects, versions)
    return st.SmartTeamsDocument(
        id=uuid.uuid4().hex,
        kind=kind,
        subject_emails=[e.strip().lower() for e in subjects],
        subject_key=subject_key,
        profile_versions=versions,
        profile_version_key=version_key,
        status="ready",
        **kw,
    )


def test_a_pair_guide_goes_stale_when_a_subject_retakes_the_interview(
    tmp_path, monkeypatch
):
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(_profile(SHAY, 1))
        s.add(_profile(EMERY, 1))
        s.commit()

    # Generate the pair guide against whatever is current right now.
    with TestSession() as s:
        versions = _current_versions(s, [SHAY, EMERY])
        s.add(
            _document(
                "pair_guide",
                [SHAY, EMERY],
                versions,
                title="How Shay and Emery work together",
                approved_at=datetime(2026, 8, 19),
                published=True,
            )
        )
        s.commit()

    def cache_lookup(session):
        fresh = _current_versions(session, [SHAY, EMERY])
        subject_key, version_key = st.document_cache_keys(
            "pair_guide", [SHAY, EMERY], fresh
        )
        return (
            session.query(st.SmartTeamsDocument)
            .filter(
                st.SmartTeamsDocument.kind == "pair_guide",
                st.SmartTeamsDocument.subject_key == subject_key,
                st.SmartTeamsDocument.profile_version_key == version_key,
            )
            .one_or_none()
        )

    with TestSession() as s:
        assert cache_lookup(s) is not None, "fresh guide should be a cache hit"

    # Shay retakes the interview: version 2 becomes current.
    with TestSession() as s:
        live = (
            s.query(st.SmartTeamsProfile)
            .filter(
                func.lower(st.SmartTeamsProfile.user_email) == SHAY,
                st.SmartTeamsProfile.is_current.is_(True),
            )
            .one()
        )
        live.is_current = False
        live.status = "superseded"
        s.add(_profile(SHAY, 2))
        s.commit()

    with TestSession() as s:
        # The stale guide is no longer served...
        assert cache_lookup(s) is None
        # ...but is still findable by subject, so the UI can offer to regenerate
        # rather than pretending no guide was ever written.
        prior = (
            s.query(st.SmartTeamsDocument)
            .filter(
                st.SmartTeamsDocument.kind == "pair_guide",
                st.SmartTeamsDocument.subject_key
                == st.subject_key_for("pair_guide", [SHAY, EMERY]),
            )
            .all()
        )
        assert len(prior) == 1
        assert prior[0].profile_versions == {SHAY: 1, EMERY: 1}


def test_the_same_guide_cannot_be_cached_twice(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    versions = {SHAY: 1, EMERY: 1}

    with TestSession() as s:
        s.add(_document("pair_guide", [SHAY, EMERY], versions))
        s.commit()

    with TestSession() as s:
        s.add(_document("pair_guide", [SHAY, EMERY], versions))
        with pytest.raises(IntegrityError):
            s.commit()


def test_pair_guides_are_order_sensitive_and_team_guides_are_not():
    """The cache key encodes what the document actually is.

    A pair guide reads "X provides / Y provides", so the flipped pair is a
    different document. A team is a set, so the same five people asked for in
    any order must hit the same cached report.
    """
    st = _st()
    versions = {SHAY: 1, EMERY: 2}

    ab = st.document_cache_keys("pair_guide", [SHAY, EMERY], versions)
    ba = st.document_cache_keys("pair_guide", [EMERY, SHAY], versions)
    assert ab != ba

    team_ab = st.document_cache_keys("team_guide", [SHAY, EMERY], versions)
    team_ba = st.document_cache_keys("team_guide", [EMERY, SHAY], versions)
    assert team_ab == team_ba

    # Casing and whitespace never fork the cache either.
    assert (
        st.document_cache_keys("pair_guide", ["  Shay@ACME.example ", EMERY], versions)
        == ab
    )


def test_an_unpublish_keeps_the_row_and_records_when(tmp_path, monkeypatch):
    """Unpublishing is a state, not a delete — see the privacy contract."""
    st = _st()
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    with TestSession() as s:
        s.add(
            _document(
                "personal_guide",
                [SHAY],
                {SHAY: 1},
                approved_at=datetime(2026, 8, 19),
                approved_by=SHAY,
                published=True,
                rag_ingested=True,
            )
        )
        s.commit()

    with TestSession() as s:
        doc = s.query(st.SmartTeamsDocument).one()
        doc.published = False
        doc.unpublished_at = datetime(2026, 8, 20)
        s.commit()

    with TestSession() as s:
        doc = s.query(st.SmartTeamsDocument).one()
        assert doc.published is False
        assert doc.unpublished_at is not None
        # The approval is still on record: completion state survives unpublish.
        assert doc.approved_at is not None
