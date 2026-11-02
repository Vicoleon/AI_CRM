"""Conservative, fixed provider endpoints and capability declarations.

The catalog is an application compatibility contract, not a claim that a
customer's account has model access. A connection test validates access before
an admin activates a route. Model IDs are deliberately allowlisted so client
input never becomes a URL or arbitrary API destination.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderModel:
    provider: str
    model: str
    capabilities: frozenset[str]


# These URLs are never editable from a browser or workspace setting.
PROVIDER_ENDPOINTS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "deepseek": "https://api.deepseek.com/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "meta": "https://api.meta.ai/v1/chat/completions",
}

_TEXT = frozenset({"text"})
_JSON = frozenset({"text", "json"})
_REMOTE_TOOLS = frozenset({"text", "tools", "stream", "tools_stream"})
_REMOTE_JSON_TOOLS = _REMOTE_TOOLS | {"json"}
_GENAI = frozenset(
    {
        "text",
        "json",
        "tools",
        "native_tools",
        "vision",
        "vision_json",
        "audio",
        "audio_json",
        "audio_tools",
        "stream",
        "tools_stream",
        "grounding",
    }
)
_EMBEDDING = frozenset({"embedding"})

# All other models must be added with a provider contract test and documented
# capability check. Unsupported features fail closed before model I/O.
MODEL_CATALOG: tuple[ProviderModel, ...] = (
    ProviderModel("anthropic", "claude-sonnet-4-6", _REMOTE_TOOLS),
    ProviderModel("deepseek", "deepseek-v4-pro", _REMOTE_JSON_TOOLS),
    ProviderModel("openai", "gpt-6-luna", _REMOTE_JSON_TOOLS),
    ProviderModel("openai", "text-embedding-3-small", _EMBEDDING),
    ProviderModel("meta", "muse-spark-1.3", _REMOTE_JSON_TOOLS),
    ProviderModel("gemini", "gemini-2.5-pro", _GENAI),
    ProviderModel("gemini", "gemini-embedding-2", _EMBEDDING),
    ProviderModel("vertex_client", "gemini-2.5-pro", _GENAI),
)

# Only workloads with an adapted call path appear in Admin → Workspace. Add a
# workload here in the same change that routes its actual execution path.
ROUTABLE_FEATURES: dict[str, str] = {
    "ai_priorities": "json",
    "l10_summary_email": "text",
    "insights_summary": "json",
    "crm_sync": "json",
    "crm_retrieval_plan": "json",
    "call_capture_extraction": "json",
    "proposal_generator": "json",
    "xray_json": "json",
    "xray_grounding": "grounding",
    "l10_recap": "text",
    "loop_closure": "json",
    "eos_rock_drafts": "json",
    "transcript_email_draft": "json",
    "slack_summaries": "text",
    "smart_teams_conversation": "json",
    "people_persona": "json",
    "people_web_search": "grounding",
    "open_loops": "json",
    "gmail_summaries": "json",
    "email_importance": "json",
    "signals_judge": "json",
    "dam_description": "text",
    "dam_version_summary": "text",
    "meeting_skills_extract": "json",
    "chat_file_text": "json",
    "chat_file_vision": "vision_json",
    "voice_transcription": "audio_json",
    "voice_command_audio": "audio_tools",
    "voice_command_text": "native_tools",
    "meeting_prep_internal": "json",
    "meeting_prep_external": "grounding",
    "meeting_prep_rewrite": "json",
    "meeting_prep_rewrite_grounded": "grounding",
    "deep_prep_council": "json",
    "chat_adk": "tools_stream",
    "chat_web_search": "grounding",
    "chat_impersonation": "tools",
    "sales_email_agent": "tools",
    "nightly_pipeline": "tools",
    "embedding_index": "embedding",
}


def model_capabilities(provider: str, model: str) -> frozenset[str]:
    if provider == "vertex_managed":
        from rubik_core.config import settings

        if model == "text-embedding-004":
            return _EMBEDDING
        if model in (settings.VERTEX_LLM_MODEL_FLASH, settings.VERTEX_LLM_MODEL_PRO):
            return _GENAI
        raise ValueError("Model is not in the verified provider catalog")
    for item in MODEL_CATALOG:
        if (item.provider, item.model) == (provider, model):
            return item.capabilities
    raise ValueError("Model is not in the verified provider catalog")


def public_catalog() -> dict[str, dict[str, list[str]]]:
    from rubik_core.config import settings

    result: dict[str, dict[str, list[str]]] = {
        "vertex_managed": {
            model: sorted(_GENAI)
            for model in (
                settings.VERTEX_LLM_MODEL_FLASH,
                settings.VERTEX_LLM_MODEL_PRO,
            )
        }
    }
    result["vertex_managed"]["text-embedding-004"] = sorted(_EMBEDDING)
    for item in MODEL_CATALOG:
        result.setdefault(item.provider, {})[item.model] = sorted(item.capabilities)
    return result
