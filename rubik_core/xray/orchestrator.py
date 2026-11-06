"""Deep Research orchestration.

Mirrors ``rubik_core/priorities/orchestrator.py`` mechanics exactly (it's
proven): parallel channel agents with a single per-fan-out wall-clock budget via
``concurrent.futures``, structured digests, then analyst → critic with one
feedback retry. Differences called out in the spec:

* Analyst persona (CS expert) replaces the decision-maker.
* Web agent is new (Google Search grounding).
* Per-agent failure = partial brief, not failure: a timed-out/crashed agent
  becomes a ``ChannelDigest(error=...)`` recorded in ``partial_errors`` and the
  run still completes (same UX as MeetingPrep's Intelligence Gaps).

Gotchas carried over from priorities: ``TimeoutError`` here is
``concurrent.futures.TimeoutError`` (NOT the builtin); Python threads can't be
killed, so ``cancel_futures=True`` only drops queued work — a straggler keeps
running in the background rather than blocking the batch; "all channels errored"
is an OUTAGE, surfaced, not a quiet "nothing found".
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rubik_core.ai_providers.registry import ModelRoute
from rubik_core.cost.usage import attribute_to
from rubik_core.tenancy.threads import ContextThreadPoolExecutor
from rubik_core.xray import churn as churn_mod
from rubik_core.xray import config
from rubik_core.xray.agents import CHANNEL_AGENTS
from rubik_core.xray.agents import analyst as analyst_mod
from rubik_core.xray.agents import critic as critic_mod
from rubik_core.xray.agents.base import (
    model_label,
    resolve_xray_grounding_route,
    resolve_xray_route,
)
from rubik_core.xray.schemas import (
    ChannelDigest,
    ChurnResult,
    CompanyBundle,
    TimelineDraft,
)

logger = logging.getLogger(__name__)

ProgressCb = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class XrayRoutes:
    channel: ModelRoute
    analyst: ModelRoute
    critic: ModelRoute
    web: ModelRoute | None = None


def resolve_routes() -> XrayRoutes:
    """Pin JSON and web research stages at the start of a Deep Research run."""
    return XrayRoutes(
        channel=resolve_xray_route(config.model_channel()),
        analyst=resolve_xray_route(config.model_analyst()),
        critic=resolve_xray_route(config.model_critic()),
        web=resolve_xray_grounding_route(config.model_web()),
    )


@dataclass
class DeepResult:
    sections: dict[str, Any]
    churn: ChurnResult
    model_id: str = ""
    digests: list[ChannelDigest] = field(default_factory=list)
    timeline_drafts: list[TimelineDraft] = field(default_factory=list)
    partial_errors: list[str] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)
    critique: dict[str, Any] = field(default_factory=dict)
    all_channels_errored: bool = False


@attribute_to("customer_xray")
def run_deep(
    bundle: CompanyBundle,
    whitespace: list[dict[str, Any]],
    base_timeline: list[TimelineDraft],
    *,
    requesting_user: str,
    prev_sections: dict[str, Any] | None = None,
    focus: str = "",
    personalization: str = "",
    wgll: str = "",
    channels: tuple[str, ...] = config.DEEP_CHANNELS,
    news_after: str = "",
    progress_cb: ProgressCb | None = None,
    routes: XrayRoutes | None = None,
) -> DeepResult:
    """Run the full Deep pipeline and return the merged brief."""
    routes = routes or resolve_routes()
    progress: dict[str, Any] = {ch: "queued" for ch in channels}
    progress["analyst"] = "queued"
    progress["critic"] = "queued"

    def emit() -> None:
        if progress_cb:
            try:
                progress_cb(dict(progress))
            except Exception:  # noqa: BLE001 - progress writes never break the run
                logger.debug("xray progress callback failed", exc_info=True)

    emit()

    agents = _build_agents(
        channels,
        bundle,
        requesting_user,
        news_after,
        route=routes.channel,
        web_route=routes.web,
    )
    digests = _run_channels(agents, progress, emit)

    partial_errors = list(bundle.errors)
    for d in digests:
        if d.error:
            partial_errors.append(f"{d.channel}: {d.error}")

    all_errored = bool(digests) and all(d.error for d in digests)

    # Deep churn includes the meetings-agent sentiment factor.
    sentiment = _sentiment_from(digests)
    churn = churn_mod.score(bundle, meeting_sentiment=sentiment)

    # Merge timeline: base (HubSpot) + each agent's drafts (web news, etc.).
    timeline_drafts = list(base_timeline)
    for d in digests:
        timeline_drafts.extend(d.timeline)

    # Analyst → critic → one retry.
    progress["analyst"] = "running"
    emit()
    try:
        sections = analyst_mod.synthesize(
            bundle,
            churn,
            whitespace,
            digests,
            timeline_drafts,
            prev_sections=prev_sections,
            focus=focus,
            personalization=personalization,
            wgll=wgll,
            route=routes.analyst,
        )
        progress["analyst"] = "done"
    except Exception:  # noqa: BLE001 - analyst failure = failed run upstream
        logger.warning("xray analyst AI analysis unavailable")
        progress["analyst"] = "error"
        emit()
        raise
    emit()

    progress["critic"] = "running"
    emit()
    critique = critic_mod.review(sections, route=routes.critic)
    if critique.get("critic_failed"):
        partial_errors.append("X-Ray critic AI analysis unavailable this run.")
    if critique.get("needs_revision") and config.DEEP_MAX_REVISIONS > 0:
        progress["analyst"] = "revising"
        emit()
        try:
            revised = analyst_mod.synthesize(
                bundle,
                churn,
                whitespace,
                digests,
                timeline_drafts,
                prev_sections=prev_sections,
                focus=focus,
                personalization=personalization,
                wgll=wgll,
                previous_attempt=sections,
                critique=critique,
                route=routes.analyst,
            )
            if revised and revised.get("executive_summary"):
                sections = revised  # a revision may improve, never erase
        except Exception:  # noqa: BLE001 - keep the first-pass on failure
            logger.warning("xray analyst revision AI analysis unavailable")
    progress["critic"] = "done"
    emit()

    # Deterministic guarantee: the churn rationale prose comes from the analyst
    # but the number stays the scorer's; stitch the rationale onto the result.
    churn.rationale_md = sections.get("churn_rationale_md") or ""

    if all_errored:
        partial_errors.insert(
            0, "All research channels failed — brief built from CRM data only."
        )

    return DeepResult(
        sections=sections,
        churn=churn,
        model_id=model_label(routes.analyst),
        digests=digests,
        timeline_drafts=timeline_drafts,
        partial_errors=partial_errors,
        progress=progress,
        critique=critique,
        all_channels_errored=all_errored,
    )


def _build_agents(
    channels: tuple[str, ...],
    bundle: CompanyBundle,
    requesting_user: str,
    news_after: str,
    *,
    route: ModelRoute | None = None,
    web_route: ModelRoute | None = None,
):
    agents: dict[str, Any] = {}
    for ch in channels:
        cls = CHANNEL_AGENTS.get(ch)
        if cls is None:
            logger.info("xray: no agent registered for channel %s, skipping", ch)
            continue
        if ch == "web":
            agents[ch] = cls(
                bundle=bundle,
                requesting_user=requesting_user,
                news_after=news_after,
                route=web_route,
            )
        else:
            agents[ch] = cls(
                bundle=bundle,
                requesting_user=requesting_user,
                route=route,
            )
    return agents


def _run_channels(agents: dict[str, Any], progress: dict[str, Any], emit) -> list:
    """Parallel fan-out with a single wall-clock budget (priorities mechanics)."""
    digests: list[ChannelDigest] = []
    if not agents:
        return digests

    for ch in agents:
        progress[ch] = "running"
    emit()

    budget = float(config.DEEP_PER_AGENT_BUDGET_S)
    pool = ContextThreadPoolExecutor(max_workers=max(1, len(agents)))
    try:
        future_to_ch = {pool.submit(a.run): ch for ch, a in agents.items()}
        try:
            for future in concurrent.futures.as_completed(future_to_ch, timeout=budget):
                ch = future_to_ch[future]
                try:
                    digest = future.result(timeout=1.0)
                except Exception:  # noqa: BLE001
                    logger.warning("xray channel %s crashed", ch)
                    digest = ChannelDigest(
                        channel=ch, error="agent_crashed: AI analysis unavailable"
                    )
                digests.append(digest)
                progress[ch] = digest.as_progress()
                emit()
        except concurrent.futures.TimeoutError:
            done = {future_to_ch[f] for f in future_to_ch if f.done()}
            for _f, ch in future_to_ch.items():
                if ch in done or any(d.channel == ch for d in digests):
                    continue
                digests.append(
                    ChannelDigest(
                        channel=ch,
                        error=f"channel_timeout: exceeded {budget:.0f}s budget",
                    )
                )
                progress[ch] = "error"
            emit()
            logger.warning(
                "xray channel fan-out timed out after %.0fs — %d done, %d timed out",
                budget,
                sum(1 for d in digests if not d.error),
                sum(1 for d in digests if d.error),
            )
    finally:
        # Don't wait for stragglers — a slow Vertex/Workspace call would block
        # the whole run. Running threads finish in the background.
        pool.shutdown(wait=False, cancel_futures=True)
    return digests


def _sentiment_from(digests: list[ChannelDigest]) -> float | None:
    for d in digests:
        if d.channel == "meetings" and not d.error:
            val = d.attrs.get("sentiment_trend")
            try:
                if val is not None:
                    return max(-1.0, min(1.0, float(val)))
            except (TypeError, ValueError):
                return None
    return None
