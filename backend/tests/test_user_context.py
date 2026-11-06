"""The personalisation blocks (``backend/services/user_context.py``).

``build_user_context_block`` (email voice) and
``build_chat_personalization_context`` (Chat) both say who the person is: "a
member of the Sales team at Acme Corp". The team comes from the person's
department and the company from the company profile. Pinned (Review Focus 1):
with either one empty the sentence still reads naturally, and it never says
"None", a blank name or a raw ``{company}``. The profile is read from a real
database here, not patched away.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.services.user_context import (
    build_chat_personalization_context,
    build_user_context_block,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.company.profile import CompanyProfile, save_company_profile
from rubik_core.database.models import Base, UserPreference

EMAIL = "rep@acme.example"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(UserPreference(user_email=EMAIL, tone="direct", about_me="Runs demos"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(department: str) -> UserProfile:
    return UserProfile(
        id="rep",
        email=EMAIL,
        name="Rep",
        department=department,
        roles=["user"],
        created_at="2026-10-28T00:00:00",
        updated_at="2026-10-28T00:00:00",
    )


def _assert_clean(block: str) -> None:
    for bad in ("None", "{company}", "{", " the  team", "at .", "at  "):
        assert bad not in block, (bad, block)


@pytest.mark.parametrize(
    "company, department, who",
    [
        ("", "", "a member of the team at the company"),
        ("", "Sales", "a member of the Sales team at the company"),
        ("Acme Corp", "", "a member of the team at Acme Corp"),
        ("Acme Corp", "Sales", "a member of the Sales team at Acme Corp"),
    ],
    ids=["neither", "department-only", "company-only", "both"],
)
def test_the_email_voice_block_says_who_the_person_is(db, company, department, who):
    if company:
        save_company_profile(db, CompanyProfile(name=company))

    block = build_user_context_block(_user(department), db)

    assert f"You are writing on behalf of {who}." in block
    _assert_clean(block)


@pytest.mark.parametrize(
    "company, department, who",
    [
        ("", "", "a member of the team at the company"),
        ("Acme Corp", "  Sales  ", "a member of the Sales team at Acme Corp"),
    ],
    ids=["no-department-no-profile", "both-with-padding"],
)
def test_the_chat_block_says_who_the_person_is(db, company, department, who):
    if company:
        save_company_profile(db, CompanyProfile(name=company))

    block = build_chat_personalization_context(_user(department), db)

    assert f"You are assisting {who}." in block
    _assert_clean(block)


def test_a_legal_name_stands_in_when_the_profile_has_no_short_name(db):
    save_company_profile(db, CompanyProfile(legal_name="Acme Corporation LLC"))

    block = build_user_context_block(_user("Sales"), db)

    assert "a member of the Sales team at Acme Corporation LLC" in block
