"""Stored identifiers, frozen as named constants.

Each value below is written into storage that outlives a deploy: ADK session
rows (app and agent names), the vector store (the collection name), and
database rows (a call's origin, an interview speaker, a loose end's detail
key and close reason, an asset tag, an audit marker, a Fathom sync outcome). Changing one
orphans every row that already holds the old value, so it is changed here, as
a value, on purpose, and never by a text search over the code.

Writers and readers import the constant from this module; none repeats the
literal. The frontend mirrors three of them (the interview speaker in
``frontend/src/api/smartTeams.ts``, the generated-asset tag in
``frontend/src/pages/Assets.tsx`` and the loose-end close reason in
``frontend/src/api/looseEnds.ts``); ``backend/tests/test_identifiers.py`` pins
that they agree.
"""

from __future__ import annotations

#: ADK ``app_name`` of the chat runner; keys every chat session row.
ADK_APP_NAME = "cubie"
#: Name of the single-agent ADK root agent (``adk_agent/agent.py``).
ADK_AGENT_NAME = "cubie_agent"
#: Name of the chat coordinator agent; the author of its session events.
ADK_COORDINATOR_NAME = "cubie_coordinator"
#: ADK ``app_name`` of the nightly pipeline runs.
NIGHTLY_APP_NAME = "cubie_nightly"
#: ADK ``app_name`` of the HubSpot-only chat an admin uses to view as a rep.
IMPERSONATION_APP_NAME = "cubie_impersonation"
#: Default local Chroma collection.
CHROMA_COLLECTION = "cubie"
#: ``call_sessions.origin`` of a call the app started itself (a Meet space it
#: created, with no calendar event behind it).
CALL_ORIGIN_ASSISTANT = "cubie"
#: Smart Teams interview speaker for the assistant's own turns.
SMART_TEAMS_ASSISTANT_SPEAKER = "cubie"
#: ``loop_items.detail`` key holding the emails already logged to the CRM
#: while the item stayed open.
CRM_LOGGED_DETAIL_KEY = "cubie_logged"
#: ``loop_items.close_reason`` of a loose end its owner closed from the app by
#: logging its emails to the CRM ("Log it in HubSpot"). Also in the
#: ``ck_loop_items_close_reason`` CHECK.
LOOP_CLOSE_LOGGED_FROM_ASSISTANT = "logged_from_assistant"
#: Asset tag on documents the app generated (they take no new versions).
ASSET_TAG_GENERATED = "cubie-generated"
#: ``details.via`` of an audit row written by the web app itself.
AUDIT_VIA_ASSISTANT = "cubie"
#: Fathom sync outcome for a recording whose host is outside the workspace.
FATHOM_SKIP_HOST_OUTSIDE = "skipped_host_outside_workspace"
