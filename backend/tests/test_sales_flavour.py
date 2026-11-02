"""The sales flavour of a brief or a chat follows the department SLUG.

CLAUDE.md: compare departments by RAG slug (``slugify_department``), never a raw
``==`` or ``.lower()``. Department rows are uncurated free text, so " Sales",
"Sales " and "Sales." are one team; a raw compare once gave such a person the
sales Meeting Prep but the regular Deep Prep, and no Sales Email Agent.

Pinned here: the one helper every site uses (``rubik_core.sales.flavour``), the
chat agents built from it, and that no site falls back to a raw compare.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rubik_core.sales.flavour import is_sales_flavoured

_REPO = Path(__file__).resolve().parents[2]

#: Every place that picks a sales flavour by department.
_SITES = (
    "backend/routers/home.py",
    "rubik_core/adk_agent/agent.py",
    "rubik_core/agent_runtime/coordinator.py",
    "backend/routers/fathom.py",
)

SALES_SPELLINGS = ["Sales", "sales", " Sales", "Sales ", "Sales.", "SALES\t"]


@pytest.mark.parametrize("department", SALES_SPELLINGS)
def test_every_spelling_of_the_sales_department_gets_the_sales_flavour(department):
    assert is_sales_flavoured(department)


@pytest.mark.parametrize("department", [None, "", "Sales Ops", "Marketing", "presales"])
def test_other_departments_do_not(department):
    assert not is_sales_flavoured(department)


def test_a_sales_role_counts_like_the_department():
    assert is_sales_flavoured("Marketing", ["user", " Sales"])
    assert not is_sales_flavoured("Marketing", ["user", "admin"])


@pytest.mark.parametrize("department", [" Sales ", "Sales."])
def test_the_chat_coordinator_gives_any_sales_spelling_the_email_agent(department):
    from rubik_core.agent_runtime.coordinator import build_coordinator
    from rubik_core.company.profile import CompanyProfile

    agent = build_coordinator(
        user_email="rep@acme.example",
        user_department=department,
        company_profile=CompanyProfile(),
    )
    assert [sub.name for sub in agent.sub_agents] == ["sales_email_agent"]


def test_the_chat_coordinator_gives_no_one_else_the_email_agent():
    from rubik_core.agent_runtime.coordinator import build_coordinator
    from rubik_core.company.profile import CompanyProfile

    agent = build_coordinator(
        user_email="mk@acme.example",
        user_department="Marketing",
        company_profile=CompanyProfile(),
    )
    assert agent.sub_agents == []


def test_the_single_agent_gives_any_sales_spelling_the_email_agent():
    from rubik_core.adk_agent.agent import build_agent

    agent = build_agent(user_email="rep@acme.example", user_department="Sales.")
    assert [sub.name for sub in agent.sub_agents] == ["sales_email_agent"]


@pytest.mark.parametrize("path", _SITES)
def test_no_site_compares_a_department_raw(path):
    source = (_REPO / path).read_text(encoding="utf-8")
    raw = re.findall(
        r"""(?:department|dept|\br)\b[^\n]*\.lower\(\)\s*==\s*["']""", source
    )
    assert raw == [], f"{path} compares a department with .lower(): {raw}"
