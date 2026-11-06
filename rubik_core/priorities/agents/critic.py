"""Critic agent.

Reviews the decision-maker's priority list against the consolidated
digest. Scores 0-10 and flags anything missing, weak, or wrong.

Mirrors the BookWriter ``ChapterCritic``: low temperature, structured
JSON, one shot. The orchestrator decides whether to trigger a revision.
"""

from __future__ import annotations

import logging

from rubik_core.priorities.agents.base import LLMJsonClient
from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.models import (
    FORWARD_PRIORITY_KINDS,
    ConsolidatedDigest,
    CritiqueReport,
    PriorityCandidate,
)
from rubik_core.priorities.temporal import TemporalContext, is_future

logger = logging.getLogger(__name__)


class CriticAgent:
    def __init__(self, config: PrioritiesConfig):
        self.config = config
        self.threshold = config.revision_threshold
        self.client = LLMJsonClient(
            model_key=config.models.critic,
            temperature=config.temperatures.critic,
            feature="ai_priorities",
        )

    # UC-24 blocker penalty per priority that fails to cite ≥2 signal_ids
    # (or fails to carry a single signal with a concrete future deadline).
    # Cheap deterministic backstop on top of the LLM's own scoring.
    BLOCKER_PENALTY = 1.5
    # …but capped in TOTAL. Uncapped, this backstop stopped modulating the
    # LLM's judgment and became the score: at 1.5 × up to `max_priorities`
    # items it could subtract 7.5 from a 0-10 scale, and over one measured week
    # it drove **14 runs to exactly 0.0** (the clamp firing) with a
    # mean score of 4.61 — so 93% of runs "needed revision" and the revision
    # pass became the default path rather than an exception. A backstop that
    # can zero any score isn't measuring quality, it's counting citations.
    # 3.0 still bites hard (a wholly unsupported list lands ~4-5 from a typical
    # base, well under any sane gate) without swamping the holistic judgment.
    MAX_BLOCKER_PENALTY = 3.0

    def review(
        self,
        digest: ConsolidatedDigest,
        priorities: list[PriorityCandidate],
        *,
        temporal: TemporalContext | None = None,
    ) -> CritiqueReport:
        prompt = self._build_prompt(digest, priorities, temporal=temporal)
        # No silent healthy fallback: if the critic LLM fails we must NOT invent
        # a passing 7.0 (that both opens the quality gate and poisons the
        # persisted health metric). Let invoke_json raise, record the failure,
        # and fall through to the deterministic UC-24 blocker check — which is
        # the only judgment we can still trust.
        critic_failed = False
        try:
            result = self.client.invoke_json(prompt)
        except Exception as exc:  # noqa: BLE001 — pipeline must stay alive
            from rubik_core.ai_providers.registry import IncompatibleModelRoute
            from rubik_core.ai_providers.runtime import ProviderRequestError

            if isinstance(exc, (IncompatibleModelRoute, ProviderRequestError)):
                raise
            logger.warning(
                "critic LLM failed (%s); UC-24 blocker check still applies", exc
            )
            critic_failed = True
            result = {}

        if not isinstance(result, dict):
            result = {}

        # Base score: the LLM's holistic judgment when available. On critic
        # failure there is none, so start at the threshold — the UC-24 blocker
        # check below is then the ONLY thing that can move it, and critic_failed
        # marks the score as non-authoritative for the health metric.
        default_base = self.threshold
        try:
            score = float(result.get("overall_score", default_base))
        except (TypeError, ValueError):
            score = default_base

        # UC-24 deterministic blocker check — applied AFTER the LLM has
        # judged. A priority that cites <2 signal_ids and doesn't carry
        # a concrete future date in attrs is penalized regardless of
        # what the LLM said. This is what enforces "the system always
        # correlates": the critic cannot be talked out of it.
        unsupported = _unsupported_priorities(digest, priorities, temporal=temporal)
        if unsupported:
            score -= min(
                self.BLOCKER_PENALTY * len(unsupported), self.MAX_BLOCKER_PENALTY
            )
            existing_weak = result.get("weak_rationales") or []
            if not isinstance(existing_weak, list):
                existing_weak = []
            result["weak_rationales"] = list(existing_weak) + [
                f"{title} — fails BLOCKER (must cite ≥2 signal_ids OR "
                f"a single signal with a concrete future date)"
                for title in unsupported
            ]

        score = max(0.0, min(10.0, score))

        missing = result.get("missing_signals", [])
        weak = result.get("weak_rationales", [])
        suggestions = result.get("suggestions", [])
        if not isinstance(missing, list):
            missing = []
        if not isinstance(weak, list):
            weak = []
        if not isinstance(suggestions, list):
            suggestions = []

        return CritiqueReport(
            overall_score=score,
            needs_revision=score < self.threshold,
            missing_signals=[str(m) for m in missing][:6],
            weak_rationales=[str(w) for w in weak][:6],
            suggestions=[str(s) for s in suggestions][:6],
            raw_feedback=(
                "(critic LLM unavailable — only the deterministic blocker check ran)"
                if critic_failed
                else str(result.get("raw_feedback", "")).strip()
            ),
            critic_failed=critic_failed,
        )

    def _build_prompt(
        self,
        digest: ConsolidatedDigest,
        priorities: list[PriorityCandidate],
        *,
        temporal: TemporalContext | None = None,
    ) -> str:
        priority_lines = []
        for i, p in enumerate(priorities, 1):
            priority_lines.append(
                f"{i}. {p.title} [urgency={p.urgency}]\n"
                f"   why_it_matters: {p.why_it_matters}\n"
                f"   suggested_action: {p.suggested_action}\n"
                f"   signal_ids: {p.signal_ids}"
            )
        priority_block = "\n".join(priority_lines) or "(no priorities)"

        signal_lines = [
            f"- id={s.id} | channel={s.channel.value} | {s.title}"
            for s in _signals_for_review(digest, priorities)
        ]
        signal_block = "\n".join(signal_lines) or "(no signals)"

        temporal_block = f"{temporal.block()}\n\n" if temporal else ""

        # The citation-support blocker is NOT the model's to judge. This prompt
        # used to tell it to check whether a single cited signal "carries a
        # concrete FUTURE date or an amount in its attrs" — but the signal
        # lines below are `id | channel | title` only, so it was scoring data
        # it never received: guessing, and penalizing single-signal cards
        # that do carry a date. `_unsupported_priorities` below checks the
        # real attrs deterministically and adds each failure to
        # weak_rationales and to the score itself.
        return (
            f"{temporal_block}"
            "You are the Critic agent. Review the priorities below against "
            "the consolidated digest and score the list 0-10.\n\n"
            "## CITATION SUPPORT IS CHECKED FOR YOU\n"
            "Whether each priority is backed by enough evidence — two or more "
            "cited signals, or one signal carrying a concrete upcoming date or "
            "an amount — is checked deterministically after you answer, "
            "against signal fields this prompt does not show you. Do not "
            "score it and do not guess at it: every failure is added to "
            "weak_rationales and deducted from the score for you.\n\n"
            "## PAST DATES\n"
            "Flag in weak_rationales any priority justified by a PAST date (a "
            "deadline or meeting that has already passed) — those must be "
            "re-framed around an upcoming moment or dropped.\n\n"
            "## SCORING RUBRIC\n"
            "- 9-10: every rationale is specific and grounded in its cited signals; nothing obvious missed; ranking is right\n"
            "- 7-8: solid but at least one rationale is generic OR one important signal was missed\n"
            "- 5-6: multiple weak rationales, redundant priorities, or wrong ranking\n"
            "- 0-4: misses the obvious / cites the wrong signals\n\n"
            "## WHAT TO FLAG\n"
            "- missing_signals: items from the digest that *should* have been a priority but were not chosen\n"
            "- weak_rationales: priorities whose 'why_it_matters' is vague, or that rest on a past date\n"
            "- suggestions: concrete changes to the next revision\n\n"
            "## OUTPUT (raw JSON only)\n"
            "{\n"
            '  "overall_score": 0-10,\n'
            '  "missing_signals": ["signal id or short description"],\n'
            '  "weak_rationales": ["priority title — why it is weak"],\n'
            '  "suggestions": ["concrete change for next attempt"],\n'
            '  "raw_feedback": "1-2 sentences of narrative feedback for the decision-maker"\n'
            "}\n\n"
            f"## CONSOLIDATED DIGEST\n{digest.narrative}\n\n"
            f"## AVAILABLE SIGNALS\n{signal_block}\n\n"
            f"## PRIORITIES UNDER REVIEW\n{priority_block}\n"
        )


# ── what the critic is shown ─────────────────────────────────────────────────

# How many signals the critic's prompt lists (it used to be a bare `[:40]`).
CRITIC_SIGNAL_CAP = 40


def _signals_for_review(
    digest: ConsolidatedDigest, priorities: list[PriorityCandidate]
) -> list:
    """The signals the critic's prompt lists, in the digest's order.

    Every signal a priority cites is always listed; the rest of the cap goes to
    the others, first-come in ``all_signals`` order (the summarizer's
    deterministic round-robin). A plain ``all_signals[:40]`` hid any cited
    signal past position 40 — the decision-maker reads 60 — from the stage
    asked whether the list "cites the wrong signals".
    """
    cited = {sid for p in priorities for sid in (p.signal_ids or [])}
    n_cited = sum(1 for s in digest.all_signals if s.id in cited)
    budget = max(0, CRITIC_SIGNAL_CAP - n_cited)
    shown: list = []
    for s in digest.all_signals:
        if s.id in cited:
            shown.append(s)
        elif budget > 0:
            shown.append(s)
            budget -= 1
    return shown


# ── UC-24 blocker check ──────────────────────────────────────────────────────


def _unsupported_priorities(
    digest: ConsolidatedDigest,
    priorities: list[PriorityCandidate],
    *,
    temporal: TemporalContext | None = None,
) -> list[str]:
    """Return the list of priority titles that fail the blocker.

    A priority passes if EITHER:
      - it cites ≥2 valid signal_ids (cross-channel correlation), OR
      - it cites exactly 1 valid signal_id AND that signal carries a
        concrete FUTURE date (``start_iso`` / ``close_date`` /
        ``mentioned_dates``) or a non-empty ``mentioned_amounts`` in
        ``attrs``. A lapsed date no longer counts as a concrete anchor.
    """
    by_id = {s.id: s for s in digest.all_signals}
    fails: list[str] = []
    for p in priorities:
        valid_ids = [sid for sid in (p.signal_ids or []) if sid in by_id]
        if len(valid_ids) >= 2:
            continue
        if len(valid_ids) == 1:
            sig = by_id[valid_ids[0]]
            # Only FORWARD kinds must anchor on a future date. A backward debt
            # exists precisely because something already happened, so a
            # past-dated single signal is a legitimate anchor for it — require
            # the future date only for forward kinds.
            kind_is_forward = p.priority_kind in FORWARD_PRIORITY_KINDS
            anchor_clock = temporal if kind_is_forward else None
            if _signal_carries_concrete_anchor(sig, temporal=anchor_clock):
                continue
        fails.append(p.title or "(unnamed)")
    return fails


def _signal_carries_concrete_anchor(
    sig, *, temporal: TemporalContext | None = None
) -> bool:
    """Cheap check for a FUTURE date or an amount in the signal.

    Date-shaped keys (``start_iso``, ``close_date``, ``mentioned_dates``)
    only count when they resolve to today-or-later — a past meeting/deadline
    is not a valid anchor (this is what stopped the "prep for yesterday's
    L10" item from satisfying the blocker). ``mentioned_amounts`` is a
    non-temporal anchor and always counts. When no ``temporal`` is supplied
    (legacy callers / backward debts), fall back to the old non-empty check.
    """
    attrs = sig.attrs or {}

    amounts = attrs.get("mentioned_amounts")
    if isinstance(amounts, str) and amounts.strip():
        return True
    if isinstance(amounts, (int, float)) and amounts:
        return True
    if isinstance(amounts, list) and any(x for x in amounts):
        return True

    date_keys = ("start_iso", "close_date", "mentioned_dates")
    for key in date_keys:
        v = attrs.get(key)
        if v in (None, "", [], {}):
            continue
        if temporal is None:
            # Back-compat: no clock supplied → any non-empty value counts.
            if isinstance(v, str) and v.strip():
                return True
            if isinstance(v, list) and any(x for x in v):
                return True
            continue
        candidates = v if isinstance(v, list) else [v]
        if any(is_future(c, temporal) for c in candidates):
            return True
    return False
