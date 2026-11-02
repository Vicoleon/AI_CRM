"""Shared agent primitives.

We do NOT use a tool-calling ReAct loop inside channel agents — the
data fetch is deterministic Python, and the LLM is asked exactly once
for a structured JSON verdict. This mirrors the BookWriter agents
(planner/writer/critic): each one does a single shaped LLM call.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from google.genai import types as genai_types

from rubik_core.config import settings
from rubik_core.gemini_tuning import generation_config_for, split_model_choice
from rubik_core.models import get_genai_client
from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.models import ChannelDigest, ChannelKind
from rubik_core.priorities.temporal import (
    TemporalContext,
    parse_to_absolute,
    tense_of,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from rubik_core.ai_providers.registry import ModelRoute


# Map the role-friendly model keys declared in PrioritiesConfig.models to the
# concrete model IDs the GenAI SDK expects. Anything not in this map passes
# through unchanged so callers can pin an exact model id when they need to.
_MODEL_KEY_MAP = {
    "gemini-flash": "VERTEX_LLM_MODEL_FLASH",
    "gemini-flash-lite": "VERTEX_LLM_MODEL_LITE",
    "gemini-2.0-flash": "VERTEX_LLM_MODEL_FLASH",
    "gemini-pro": "VERTEX_LLM_MODEL_PRO",
}


def _resolve_model_id(model_key: str) -> str:
    setting_name = _MODEL_KEY_MAP.get(model_key)
    if setting_name and getattr(settings, setting_name, None):
        return getattr(settings, setting_name)
    return model_key


# ── JSON-safety helpers (mirrors bookwritter/llm.py:safe_json) ────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```", re.MULTILINE)


def safe_json(raw: str) -> Any:
    """Best-effort JSON extraction from LLM output.

    Handles markdown fences, leading prose, and a trailing comma or two.
    Raises ``ValueError`` if nothing parseable is found.
    """
    if not raw:
        raise ValueError("empty LLM output")
    cleaned = _JSON_FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find the outermost {...} or [...] block.
        for opener, closer in (("{", "}"), ("[", "]")):
            start = cleaned.find(opener)
            end = cleaned.rfind(closer)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    continue
    # Model output can repeat private mailbox contents. Neither an exception
    # nor a log message may include even a short response excerpt.
    raise ValueError("could not parse JSON from LLM output")


class LLMJsonClient:
    """Thin wrapper that asks a per-role LLM for one JSON object.

    Each agent constructs one of these with its role's model key and
    temperature. The wrapper handles the SDK call and JSON parsing.
    Uses the Google GenAI client returned by ``get_genai_client`` — same
    pattern as the rest of the codebase post-ADK migration.
    """

    def __init__(
        self, *, model_key: str, temperature: float, feature: str | None = None
    ):
        # An admin choice may pin a thinking level ("gemini-4-argon@high"): the
        # id goes to Vertex, the level into generation_config().
        self.model_id, self.thinking_level = split_model_choice(
            _resolve_model_id(model_key)
        )
        self.temperature = temperature
        # This shared client also serves Smart Teams, promises, offboarding,
        # and ingestion. Only callers that name a workload may use its
        # workspace-owned model route.
        self.feature = feature
        self._client = None  # lazy — keeps tests cheap
        # Set only when this run carries a personally connected Gmail mailbox.
        # Called after route resolution and before each model request/retry.
        self.route_guard: Callable[[ModelRoute], None] | None = None

    @property
    def client(self):
        if self._client is None:
            self._client = get_genai_client()
        return self._client

    def generation_config(
        self, model_id: str | None = None
    ) -> genai_types.GenerateContentConfig:
        """The ``config=`` every call on this client sends.

        This used to be absent: ``generate_content`` got only a model and a
        prompt, so the per-role temperatures in ``PrioritiesConfig`` (and the
        admin-tuned ones in ``settings_store``) were stored here and never
        sent, and every stage ran at Gemini's default temperature.

        The shape is the repo's JSON-call convention (``call_capture``,
        ``proposals``): the prompt states the schema, ``response_mime_type``
        asks for JSON, and :func:`safe_json` still parses whatever comes back.
        No ``response_schema`` — nothing in this repo uses one. The thinking
        knob comes from :func:`generation_config_for`, which leaves 2.5 Pro
        alone (its thinking cannot be disabled) and speaks ``thinking_level``
        to the 3.x Flash tiers, unless the admin's choice pinned a level, which
        then wins (generation_config_for never overrides an explicit one).
        """
        selected_model = model_id or self.model_id
        thinking_level = (
            self.thinking_level if selected_model == self.model_id else None
        )
        return generation_config_for(
            selected_model,
            genai_types.GenerateContentConfig(
                temperature=self.temperature,
                response_mime_type="application/json",
                thinking_config=(
                    genai_types.ThinkingConfig(thinking_level=thinking_level)
                    if thinking_level
                    else None
                ),
            ),
        )

    def invoke_json(
        self, prompt: str, *, fallback: Any | None = None, attempts: int = 1
    ) -> Any:
        """Run the LLM and parse the response as JSON.

        ``attempts`` is the TOTAL number of tries — transient Vertex 429s and
        truncated-JSON parses (the usual empty-briefing causes) usually
        clear on a second call. After the final try: returns ``fallback``, or
        re-raises if ``fallback`` is ``None`` — so a single bad agent never
        breaks the whole bundle unless the caller opted into strictness.
        """
        total = max(1, attempts)
        last_exc: Exception | None = None
        route = None
        for attempt in range(1, total + 1):
            try:
                from rubik_core.ai_providers.registry import (
                    IncompatibleModelRoute,
                    resolve_model_route,
                )

                if self.feature is not None:
                    current_route = resolve_model_route(
                        self.feature, "json", default_model=self.model_id
                    )
                    if route is None:
                        route = current_route
                    elif route != current_route:
                        raise IncompatibleModelRoute(
                            "Workspace AI route changed during this request"
                        )
                    if self.route_guard is not None:
                        self.route_guard(current_route)
                if route is not None and route.provider != "vertex_managed":
                    from rubik_core.ai_providers.runtime import generate_text_on_route

                    response = generate_text_on_route(route, prompt, json_mode=True)
                    return safe_json(response.text)
                # Built inside the try so a config error takes the same
                # fallback path as a failed call instead of escaping it.
                managed_model = route.model if route is not None else self.model_id
                response = self.client.models.generate_content(
                    model=managed_model,
                    contents=prompt,
                    config=self.generation_config(managed_model),
                )
                raw = response.text if hasattr(response, "text") else str(response)
                return safe_json(raw)
            except IncompatibleModelRoute:
                raise
            except Exception as exc:  # noqa: BLE001 — pipeline must stay alive
                if route is not None and route.provider != "vertex_managed":
                    from rubik_core.ai_providers.runtime import ProviderRequestError

                    # Parser exceptions can contain a snippet of the model's
                    # response, including customer content. Do not log or
                    # propagate that snippet for a client-owned provider.
                    last_exc = (
                        exc
                        if isinstance(exc, ProviderRequestError)
                        else ProviderRequestError(
                            "Selected AI provider returned invalid JSON"
                        )
                    )
                    logger.warning(
                        "LLMJsonClient selected provider failed (attempt %d/%d): %s",
                        attempt,
                        total,
                        type(exc).__name__,
                    )
                else:
                    last_exc = (
                        ValueError("Managed AI provider returned invalid JSON")
                        if isinstance(exc, ValueError)
                        else RuntimeError("Managed AI provider request failed")
                    )
                    logger.warning(
                        "LLMJsonClient(%s) failed (attempt %d/%d): %s",
                        self.model_id,
                        attempt,
                        total,
                        type(exc).__name__,
                    )
                if attempt < total:
                    time.sleep(1.5 * attempt)
        if route is not None and route.provider != "vertex_managed":
            raise last_exc  # type: ignore[misc]  # a selected BYOK route never hides failures
        if fallback is None:
            raise last_exc  # type: ignore[misc]  # loop ran ≥1 time
        return fallback


# ── Channel agent ABC ────────────────────────────────────────────────────────


def _is_unset(value: Any) -> bool:
    """True for an attr the parser left empty for the model to fill, or never set."""
    return value is None or value == "" or value == [] or value == {}


class BaseChannelAgent(ABC):
    """Contract for a single-channel agent.

    Subclasses must declare :attr:`channel` and implement :meth:`fetch_signals`
    (deterministic data pull) and :meth:`build_prompt` (LLM instructions).
    The base class handles the LLM call and ``ChannelDigest`` assembly so
    every channel agent has identical resilience behavior.
    """

    channel: ChannelKind

    #: The per-signal attrs the extractor LLM may write, and nothing else:
    #: exactly the fields this agent's prompt asks for in its OUTPUT section (a
    #: test holds the two together). Empty — the default — means the channel
    #: takes no model-authored attrs at all, because every attr is computed.
    model_attr_keys: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        user_email: str,
        config: PrioritiesConfig,
        temporal: TemporalContext | None = None,
    ):
        self.user_email = user_email
        self.config = config
        # Pinned "now" for this run. The orchestrator sets it so every
        # extractor prompt is anchored to the user's local date and any
        # date the model extracts can be normalized + tense-stamped.
        self.temporal = temporal
        self.client = LLMJsonClient(
            model_key=getattr(config.models, f"{self.channel.value}_agent"),
            temperature=getattr(config.temperatures, f"{self.channel.value}_agent"),
            feature="ai_priorities",
        )

    @abstractmethod
    def fetch_signals(self) -> list:
        """Return the raw items pulled from this channel."""

    @abstractmethod
    def build_prompt(self, signals: list) -> str:
        """Render the LLM prompt given the fetched signals."""

    @abstractmethod
    def parse_signals(self, raw_items: list) -> list:
        """Convert raw API items into :class:`ChannelSignal` objects, best-first.

        Order is part of the contract: return them in the order this channel
        would keep them if it could keep only a few. The summarizer deals
        channels round-robin in this order ahead of the decision-maker's
        signal cap, so on a busy day whatever is listed last is cut first.
        """

    def run(self) -> ChannelDigest:
        """Single-channel pipeline: fetch → normalise → enrich.

        UC-22 extractor contract: the LLM call returns a JSON object with:

        - ``summary``: required, one factual sentence describing what the
          channel saw. No judgment vocabulary.
        - ``signal_attrs`` (or ``thread_attrs`` etc. — see
          :attr:`signal_attrs_key`): optional dict keyed by signal ``id``
          mapping to a partial ``attrs`` dict the base class merges into
          the corresponding signal — only the keys in
          :attr:`model_attr_keys`, and never over a computed value (see
          :meth:`_merge_model_attrs`). This is how the LLM enriches the
          structured signals without inventing new ones.

        For backwards compat the old ``narrative`` + ``highlights`` shape
        is still tolerated (and stored), but new channel agents should
        populate ``summary`` + ``signal_attrs`` only.
        """
        try:
            raw_items = self.fetch_signals()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s fetch failed for %s: %s",
                self.channel,
                self.user_email,
                type(exc).__name__,
            )
            return ChannelDigest(
                channel=self.channel, error=f"fetch_failed: {type(exc).__name__}"
            )

        signals = self.parse_signals(raw_items)
        if not signals:
            return ChannelDigest(
                channel=self.channel,
                narrative=f"No notable activity in {self.channel.value} today.",
                summary=f"No notable activity in {self.channel.value} today.",
                signals=[],
            )

        prompt = self.build_prompt(signals)
        # Anchor the extractor to the user's local "now" so any relative date
        # it resolves ("next Tuesday") is resolved against the right day.
        if self.temporal is not None:
            prompt = f"{self.temporal.block()}\n\n{prompt}"
        result = self.client.invoke_json(
            prompt,
            fallback={"summary": "", "signal_attrs": {}},
        )
        if not isinstance(result, dict):
            result = {}

        # New contract — preferred.
        summary = (result.get("summary") or result.get("narrative") or "").strip()

        # Merge any LLM-extracted attrs into the matching signal. Channel
        # agents can override ``signal_attrs_key`` (e.g. Gmail uses
        # ``thread_attrs``) to keep their prompts readable.
        enrichment_key = getattr(self, "signal_attrs_key", "signal_attrs")
        enrichment = result.get(enrichment_key) or result.get("signal_attrs") or {}
        if isinstance(enrichment, dict):
            self._merge_model_attrs(signals, enrichment)

        # Deterministic backstop: normalize any extracted ``mentioned_dates``
        # to absolute dates + tense so the decision-maker and the past-event
        # post-filter can compare them to "now" without trusting the model.
        if self.temporal is not None:
            for sig in signals:
                self._normalize_mentioned_dates(sig)

        # Legacy fields, retained so older summarizer code that reads
        # ``highlights`` keeps working until UC-24 ships its rewrite.
        legacy_highlights_raw = result.get("highlights") or []
        legacy_highlights = (
            [str(h) for h in legacy_highlights_raw][:8]
            if isinstance(legacy_highlights_raw, list)
            else []
        )

        return ChannelDigest(
            channel=self.channel,
            narrative=summary,  # narrative kept = summary for back-compat
            summary=summary,
            highlights=legacy_highlights,
            signals=signals,
        )

    def _merge_model_attrs(self, signals: list, enrichment: dict) -> None:
        """Merge the extractor's per-signal patch where, and only where, it may write.

        Two rules. The comment that used to sit on this merge promised the
        first, and the code enforced neither:

        1. Only a key in :attr:`model_attr_keys`. A model that returns
           ``direction`` (Gmail computes it) is not enriching the signal, it
           is rewriting a fact, and ``orchestrator._reader_closed_the_loop``
           hard-drops follow-up cards on ``direction`` — so an email whose
           text steered the model into returning it could hide a real
           priority.
        2. Never over a value code computed. A placeholder the parser left
           empty for the model (``[]``, ``""``, ``None``, or no key at all)
           may be filled; anything else stays, unless :meth:`_model_may_write`
           says the prompt explicitly asks the model to re-read that field.

        Rejections are counted without logging model-supplied key text, which
        may contain private source content after prompt injection.
        """
        by_id = {s.id: s for s in signals}
        for sig_id, patch in enrichment.items():
            sig = by_id.get(str(sig_id))
            if sig is None or not isinstance(patch, dict):
                continue
            outside = 0
            computed = 0
            for key, value in patch.items():
                if not isinstance(key, str) or key not in self.model_attr_keys:
                    outside += 1
                elif sig.attrs.get(key) == value:
                    continue  # an echo of what is already there changes nothing
                elif self._model_may_write(sig, key, value):
                    sig.attrs[key] = value
                else:
                    computed += 1
            if outside or computed:
                logger.warning(
                    "[priorities] %s extractor patch for %s rejected on signal "
                    "%s — outside-schema keys: %d; computed-value overwrites: %d",
                    self.channel.value,
                    self.user_email,
                    sig.id,
                    outside,
                    computed,
                )

    def _model_may_write(self, sig, key: str, value: Any) -> bool:
        """Whether the extractor may set ``sig.attrs[key] = value``.

        Asked only for keys in :attr:`model_attr_keys`. The rule is "fill a
        placeholder, never overwrite a computed value". An agent overrides
        this for a field its prompt explicitly asks the model to re-read.
        """
        return _is_unset(sig.attrs.get(key))

    def _normalize_mentioned_dates(self, sig) -> None:
        """Coerce ``attrs['mentioned_dates']`` into ``{raw,resolved,tense}``.

        The extractor LLM is asked to emit resolved dates, but the model can
        regress to bare strings ("Friday EOD"). This makes the field
        deterministic regardless: every entry gains an absolute ``resolved``
        (ISO date or None) and a ``tense`` (past/today/future/unknown), so
        downstream temporal logic never has to re-parse free-form text.
        """
        if self.temporal is None:
            return
        raw_dates = sig.attrs.get("mentioned_dates")
        if not isinstance(raw_dates, list) or not raw_dates:
            return
        normalized: list[dict] = []
        for entry in raw_dates:
            # Preserve any caller-supplied metadata (e.g. a semantic label);
            # only (re)compute raw/resolved/tense so the field is deterministic
            # without dropping fields a future extractor may attach.
            out = dict(entry) if isinstance(entry, dict) else {}
            if isinstance(entry, dict):
                raw = entry.get("raw") or entry.get("resolved") or ""
            else:
                raw = str(entry)
            resolved = parse_to_absolute(entry, self.temporal)
            out["raw"] = raw
            out["resolved"] = resolved.isoformat() if resolved else None
            out["tense"] = tense_of(entry, self.temporal)
            normalized.append(out)
        sig.attrs["mentioned_dates"] = normalized
