"""Multi-agent pipeline that builds the AI Priorities bundle for a user.

Pipeline shape (mirroring the BookWriter pattern):

    [ChannelAgent x N] (parallel)
            │
            ▼
        Summarizer
            │
            ▼
       DecisionMaker  ←─── (revise once if critic asks)
            │
            ▼
          Critic
            │
            ▼
       PrioritiesBundle  ──►  Supervisor only sees this final bundle

Each role uses its own model + temperature via :class:`PrioritiesConfig`.
"""

from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.models import (
    GENERATION_EMPTY_WITH_SIGNALS,
    GENERATION_FILTERED_EMPTY,
    GENERATION_NO_SIGNALS,
    GENERATION_OK,
    ChannelDigest,
    ChannelKind,
    ChannelSignal,
    ConsolidatedDigest,
    CritiqueReport,
    EntityKind,
    PrioritiesBundle,
    PrioritiesRequest,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.orchestrator import (
    PrioritiesOrchestrator,
    bundle_from_metadata,
    bundle_to_metadata,
)
from rubik_core.priorities.persistence import (
    DAILY_BRIEFING_CATEGORY,
    NEXT_WEEK_CATEGORY,
    bundle_to_markdown,
    persist_bundle,
)

__all__ = [
    "ChannelKind",
    "ChannelSignal",
    "ChannelDigest",
    "ConsolidatedDigest",
    "EntityKind",
    "PriorityKind",
    "PriorityCandidate",
    "CritiqueReport",
    "PrioritiesBundle",
    "PrioritiesRequest",
    "PrioritiesConfig",
    "GENERATION_OK",
    "GENERATION_NO_SIGNALS",
    "GENERATION_EMPTY_WITH_SIGNALS",
    "GENERATION_FILTERED_EMPTY",
    "PrioritiesOrchestrator",
    "bundle_to_metadata",
    "bundle_from_metadata",
    "persist_bundle",
    "bundle_to_markdown",
    "DAILY_BRIEFING_CATEGORY",
    "NEXT_WEEK_CATEGORY",
    "AGENT_ROLES",
    "CHANNEL_AGENT_ROLES",
    "PIPELINE_AGENT_ROLES",
    "PROVIDER_MODEL_CATALOG",
    "SETTINGS_KEY",
    "SUPPORTED_PROVIDERS",
    "load_priorities_config",
    "load_settings_blob",
    "load_settings_blob_strict",
    "redact_secrets",
    "save_settings_blob",
    "SettingsReadError",
    "update_settings_blob",
]

# ── Admin-managed settings ───────────────────────────────────────────────────
from rubik_core.priorities.settings_store import (
    AGENT_ROLES,
    CHANNEL_AGENT_ROLES,
    PIPELINE_AGENT_ROLES,
    PROVIDER_MODEL_CATALOG,
    SETTINGS_KEY,
    SUPPORTED_PROVIDERS,
    SettingsReadError,
    load_priorities_config,
    load_settings_blob,
    load_settings_blob_strict,
    redact_secrets,
    save_settings_blob,
    update_settings_blob,
)
