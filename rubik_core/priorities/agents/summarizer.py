"""Summarizer agent.

Reads all per-channel digests and produces a single cross-channel
narrative + a normalized signal list the decision-maker can rank over.

Conceptually this is the BookWriter ``StyleAnalyst``/``Planner`` step:
it does NOT make decisions, it organizes what the channel agents
found into a shape the downstream agent can act on.
"""

from __future__ import annotations

import logging

from rubik_core.priorities.agents.base import LLMJsonClient
from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelKind,
    ChannelSignal,
    ConsolidatedDigest,
)
from rubik_core.priorities.temporal import TemporalContext

logger = logging.getLogger(__name__)

# The canonical channel order: ChannelKind's definition order. It fixes the
# order of the channel reports in the summarizer prompt and the tie-break inside
# each round of the interleave below — never which channel gets more slots.
_CHANNEL_RANK = {kind: i for i, kind in enumerate(ChannelKind)}


def in_channel_order(digests: list[ChannelDigest]) -> list[ChannelDigest]:
    """``digests`` in canonical channel order, whatever order they finished in."""
    return sorted(
        digests, key=lambda d: _CHANNEL_RANK.get(d.channel, len(_CHANNEL_RANK))
    )


def interleave_signals(digests: list[ChannelDigest]) -> list[ChannelSignal]:
    """Every channel's signals, dealt round-robin: each channel's 1st, then each 2nd, …

    The order decides who survives the downstream caps (the decision-maker
    reads the first 60 signals). It used to be completion order: the
    orchestrator appends digests as ``as_completed`` yields them and this list
    concatenated them, so on a busy day whichever channel's API answered first
    took the slots, and the same inbox could produce a different briefing on a
    different morning. Round-robin is deterministic and fair: a channel with
    30 deals cannot crowd out one with 3 To-Dos, and each channel keeps its
    own ranking, because agents list their signals best-first (see
    ``BaseChannelAgent.parse_signals``).
    """
    lanes = [list(d.signals) for d in in_channel_order(digests)]
    out: list[ChannelSignal] = []
    for rank in range(max((len(lane) for lane in lanes), default=0)):
        for lane in lanes:
            if rank < len(lane):
                out.append(lane[rank])
    return out


class SummarizerAgent:
    def __init__(self, config: PrioritiesConfig):
        self.config = config
        self.client = LLMJsonClient(
            model_key=config.models.summarizer,
            temperature=config.temperatures.summarizer,
            feature="ai_priorities",
        )

    def consolidate(
        self,
        digests: list[ChannelDigest],
        *,
        temporal: TemporalContext | None = None,
    ) -> ConsolidatedDigest:
        # Always preserve the full signal list, even if the LLM dies — the
        # decision-maker can still rank on raw signals. Canonical order first,
        # so neither the signal list nor this prompt depends on which channel
        # finished first.
        digests = in_channel_order(digests)
        all_signals = interleave_signals(digests)
        per_channel_summaries: dict[str, str] = {}
        for d in digests:
            per_channel_summaries[d.channel.value] = (
                d.error or d.narrative or "(no narrative)"
            )

        prompt = self._build_prompt(digests, temporal=temporal)
        result = self.client.invoke_json(
            prompt,
            fallback={"narrative": "", "cross_channel_themes": []},
        )

        narrative = result.get("narrative", "") if isinstance(result, dict) else ""
        themes = (
            result.get("cross_channel_themes", []) if isinstance(result, dict) else []
        )
        if not isinstance(themes, list):
            themes = []

        return ConsolidatedDigest(
            narrative=narrative or "(no consolidated narrative)",
            cross_channel_themes=[str(t) for t in themes][:8],
            per_channel_summaries=per_channel_summaries,
            all_signals=all_signals,
        )

    def _build_prompt(
        self,
        digests: list[ChannelDigest],
        *,
        temporal: TemporalContext | None = None,
    ) -> str:
        per_channel_blocks = []
        for d in digests:
            highlights = "\n    ".join(d.highlights) if d.highlights else "(none)"
            err = f"\n  ERROR: {d.error}" if d.error else ""
            per_channel_blocks.append(
                f"### {d.channel.value.upper()}{err}\n"
                f"  narrative: {d.narrative or '(empty)'}\n"
                f"  highlights:\n    {highlights}"
            )
        block = "\n\n".join(per_channel_blocks)
        temporal_block = f"{temporal.block()}\n\n" if temporal else ""

        return (
            f"{temporal_block}"
            "You are the Summarizer agent. Several channel agents have each "
            "reported what they saw. Your job is to weave those into ONE "
            "cross-channel view of what is going on for the user right now.\n\n"
            "## RULES\n"
            "- Do NOT make decisions about what is most important — that's the "
            "next agent's job. Just describe what is happening.\n"
            "- Call out cross-channel patterns (e.g. 'a deal mentioned in email "
            "also has a meeting tomorrow').\n"
            "- Drop redundancy — if two channels reported the same thing, say "
            "it once.\n\n"
            "## OUTPUT (raw JSON only)\n"
            "{\n"
            '  "narrative": "3-6 sentences. The unified picture across channels.",\n'
            '  "cross_channel_themes": ["short bullet per theme that spans more than one channel"]\n'
            "}\n\n"
            f"## CHANNEL REPORTS\n{block}\n"
        )
