"""Regression tests for the chat coordinator's tool-discipline / honesty rules
and the scoped HubSpot tool description.

Pins the fix for the bug where the coordinator (a) asked "what should I check?"
instead of calling the source the user named, and (b) claimed it had "already
checked" HubSpot when it had only run a web search + RAG. See ClickUp 86aje6que.

These are intentionally content-assertions (like test_hubspot_retriever_prompt.py):
they guard the specific guarantees in the prompt/description so a later edit can't
silently drop them. They do not instantiate the Agent (no GCP/LLM needed).
"""

import inspect

from rubik_core.adk_agent.tools import AgentTools
from rubik_core.agent_runtime import coordinator


def test_coordinator_forbids_claiming_an_unused_tool():
    """Defect D2 — the instruction must forbid saying a source was checked when its
    tool was not actually called this turn, and forbid passing one source's result
    off as another's."""
    src = inspect.getsource(coordinator.build_coordinator)
    lowered = src.lower()
    assert "never say you checked" in lowered
    assert "current turn" in lowered
    assert "as if it came from another" in lowered


def test_coordinator_calls_a_named_source_immediately():
    """Defect D1 — the instruction must tell the model to call a source the user
    named right away rather than asking what to look for first."""
    src = inspect.getsource(coordinator.build_coordinator)
    assert "names a source explicitly" in src
    assert "do NOT ask them what to look for" in src


def test_hubspot_tool_description_is_scoped_and_marks_out_of_scope():
    """search_hubspot_data must advertise sales-pipeline scope and explicitly mark
    company-attribute / vendor lookups OUT OF SCOPE, so the coordinator routes such
    asks elsewhere instead of implying HubSpot holds the data."""
    doc = AgentTools.search_hubspot_data.__doc__ or ""
    # Advertises its scope (sales-pipeline + outreach/last-touch) and, critically,
    # still marks non-sales company attributes OUT OF SCOPE.
    assert "sales-pipeline" in doc.lower()
    assert "OUT OF SCOPE" in doc
    # The exact real-world miss that started this: a company's doc custodian.
    assert "document custodian" in doc
    # Must not resurrect the stale "BigQuery data warehouse" claim — the live path
    # is the HubSpot REST API.
    assert "BigQuery data warehouse" not in doc


def test_find_hubspot_contact_tool_is_registered_and_distinct():
    """A by-name/email contact lookup must be a real, registered tool, distinct
    from the analytics tool — otherwise the coordinator has no way to resolve a
    person's email (the "couldn't find a contact record for Arden Webb" gap)."""
    # Wired into the coordinator's tool list.
    src = inspect.getsource(coordinator.build_coordinator)
    assert "find_hubspot_contact" in src

    # Exists as a tool with a contact-lookup docstring keyed on name OR email,
    # and it explicitly names search_hubspot_data so the model routes correctly.
    doc = (AgentTools.find_hubspot_contact.__doc__ or "").lower()
    assert "contact" in doc
    assert "email" in doc and "name" in doc
    assert "search_hubspot_data" in doc
