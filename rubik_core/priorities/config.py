"""Per-role model + temperature routing for the priorities pipeline.

Mirrors the BookWriter ``ModelRouting``: each role declares its model key
once, then :class:`PrioritiesConfig` is consulted at agent construction
time. This makes it cheap to swap channel agents to a faster model and
keep the critic on a more careful one — without touching agent code.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelRouting:
    # Channel agents do light extraction → fast/cheap is fine.
    gmail_agent: str = "gemini-flash"
    calendar_agent: str = "gemini-flash"
    drive_agent: str = "gemini-flash"
    meet_agent: str = "gemini-flash"
    hubspot_agent: str = "gemini-flash"
    eos_agent: str = "gemini-flash"
    # NB: the field name is derived from ChannelKind.<X>.value at agent
    # construction (`getattr(config.models, f"{channel.value}_agent")`) — a
    # channel with no entry here dies with AttributeError on every run.
    slack_agent: str = "gemini-flash"
    # Unused at runtime (the inherited channel is deterministic and overrides
    # run()), but the base-class constructor resolves a model per channel.
    inherited_agent: str = "gemini-flash-lite"
    # Same story for the promises (My To-Dos) channel — deterministic reader.
    promises_agent: str = "gemini-flash-lite"

    # Cross-channel reasoning → use the default (better) model.
    summarizer: str = "gemini-pro"
    decision_maker: str = "gemini-pro"

    # Critic is the quality gate — same tier as decision-maker.
    critic: str = "gemini-pro"


@dataclass
class TemperatureRouting:
    # Channels: lower temp, deterministic extraction.
    gmail_agent: float = 0.3
    calendar_agent: float = 0.3
    drive_agent: float = 0.3
    meet_agent: float = 0.3
    hubspot_agent: float = 0.3
    eos_agent: float = 0.3
    slack_agent: float = 0.3
    inherited_agent: float = 0.0  # deterministic channel — never calls the LLM
    promises_agent: float = 0.0  # deterministic channel — never calls the LLM

    # Summarizer: moderate, allows phrasing flexibility.
    summarizer: float = 0.45

    # Decision-maker: a touch higher to combine signals creatively.
    decision_maker: float = 0.55

    # Critic: coldest, we want reproducible scoring.
    critic: float = 0.25


@dataclass
class PrioritiesConfig:
    """Top-level config bag for one pipeline run."""

    models: ModelRouting = field(default_factory=ModelRouting)
    temperatures: TemperatureRouting = field(default_factory=TemperatureRouting)

    # Critique gate. Was 7.0, inherited from BookWriter. Lowered to 6.0 after
    # measuring the live distribution over a week: mean
    # score 4.61, with only 7% of runs passing the then-live 8.0 gate and 20%
    # passing 7.0 — "revise" was the default path on 93% of runs, not an
    # exception. Only the FALLBACK: the live value is admin-tuned in
    # `workspace_settings` (priorities_agent_config → policy.revision_threshold,
    # see settings_store), so changing this line alone moves nothing in a
    # deployment that has saved its config.
    revision_threshold: float = 6.0
    # How many revise → re-score rounds a run may spend. A real bound: it
    # used to be read as a boolean, so 3 meant exactly 1.
    max_revisions: int = 1

    # Channel agent fetch budgets.
    gmail_max_threads: int = 12
    calendar_lookahead_days: int = 4
    drive_max_files: int = 10
    meet_max_results: int = 5
    slack_max_messages: int = 20
    slack_max_channels: int = 10

    # Wall-clock budget for a single channel agent. The orchestrator
    # gathers all channels in parallel and lets slow ones be empty
    # rather than blocking the whole bundle.
    #
    # UC-40.2 bumped this from 30 → 90. Per-channel work is Workspace
    # API calls (~5s) PLUS a Vertex LLM extraction call (~10-60s
    # depending on input size + cold-start). 30s was too tight; users
    # whose channels did legitimate work timed out before the LLM
    # responded. 90s comfortably covers a real channel run while still
    # bounding latency.
    per_channel_timeout_seconds: int = 90

    @classmethod
    def default(cls) -> PrioritiesConfig:
        return cls()
