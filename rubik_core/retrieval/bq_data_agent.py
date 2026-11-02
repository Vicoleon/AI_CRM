"""
BigQuery Data Agent (preview) wrapper — UC-7.

The old implementation called Google's Conversational Analytics API
(`geminidataanalytics.googleapis.com`) directly via HTTPS for HubSpot reads.

The process-global agent resource is disabled until a workspace-bound agent,
project, and dataset can be resolved. Callers should use the live HubSpot API.
"""

from __future__ import annotations

import logging
from typing import Any

import google.auth
import google.auth.transport.requests
import httpx

from rubik_core.adapters.bigquery import (
    WorkspaceBigQueryUnavailable,
    require_workspace_crm_bigquery,
)
from rubik_core.config import settings

logger = logging.getLogger(__name__)

API_HOST = "geminidataanalytics.googleapis.com"


class BQDataAgentError(RuntimeError):
    """Raised when the BQ Data Agent call fails or returns an unusable response."""


def _get_access_token() -> str:
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def query_hubspot_via_agent(question: str) -> dict[str, Any]:
    """Send a natural-language question to the configured BQ Data Agent.

    Returns:
        {"nl_answer": str, "raw_response": dict}

    Raises:
        BQDataAgentError on any failure — auth, network, empty response, etc.
    """
    try:
        require_workspace_crm_bigquery()
    except WorkspaceBigQueryUnavailable as e:
        raise BQDataAgentError(str(e)) from e

    agent_resource = settings.BQ_DATA_AGENT_RESOURCE
    if not agent_resource:
        raise BQDataAgentError("BQ_DATA_AGENT_RESOURCE not configured")

    project = settings.GCP_PROJECT_ID
    if not project:
        raise BQDataAgentError("GCP_PROJECT_ID not set")

    location = settings.BQ_DATA_AGENT_LOCATION
    parent = f"projects/{project}/locations/{location}"
    url = f"https://{API_HOST}/v1beta/{parent}:chat"

    try:
        token = _get_access_token()
    except Exception as e:
        raise BQDataAgentError(f"auth failed: {e}") from e

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "parent": parent,
        "messages": [{"userMessage": {"text": question}}],
        "dataAgentContext": {"dataAgent": agent_resource},
    }

    logger.info("Calling BQ Data Agent: agent=%s, q=%r", agent_resource, question[:80])
    try:
        with httpx.Client(timeout=settings.BQ_DATA_AGENT_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise BQDataAgentError(f"HTTP transport error: {e}") from e

    if resp.status_code >= 400:
        raise BQDataAgentError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as e:
        raise BQDataAgentError(f"non-JSON response: {e}") from e

    nl_answer = _extract_nl_answer(data)
    if not nl_answer:
        raise BQDataAgentError(
            f"agent returned no NL answer; raw keys={list(data.keys())}"
        )

    return {"nl_answer": nl_answer, "raw_response": data}


def _extract_nl_answer(data: Any) -> str:
    """Pull NL answer text out of the chat response across known shapes.

    The preview API has changed shape across versions; we try several known
    layouts so a schema tweak doesn't immediately break us.
    """
    parts: list[str] = []

    def walk_text(text_obj: Any) -> None:
        if isinstance(text_obj, str):
            parts.append(text_obj)
            return
        if isinstance(text_obj, dict):
            for p in text_obj.get("parts", []) or []:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict) and "text" in p:
                    parts.append(str(p["text"]))

    def walk_messages(msgs: Any) -> None:
        if not isinstance(msgs, list):
            return
        for m in msgs:
            if not isinstance(m, dict):
                continue
            sm = m.get("systemMessage") or m.get("system_message") or {}
            if isinstance(sm, dict):
                walk_text(sm.get("text"))
            for key in ("text", "content"):
                if key in m and isinstance(m[key], (str, dict)):
                    walk_text(m[key])

    if isinstance(data, dict):
        walk_messages(data.get("messages"))
        for resp in data.get("responses", []) or []:
            if isinstance(resp, dict):
                walk_messages(resp.get("messages"))
        if not parts and isinstance(data.get("text"), (str, dict)):
            walk_text(data["text"])

    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                walk_messages(entry.get("messages"))

    return "\n".join(p for p in parts if p).strip()
