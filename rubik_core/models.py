# rubik_core/models.py
import logging

from google import genai
from google.genai import types

from rubik_core.config import settings
from rubik_core.gemini_tuning import generation_config_for, thinking_config_for

logger = logging.getLogger(__name__)

_GENAI_CLIENT = None


def _estimate_tokens(contents) -> int:
    """Rough input-token estimate (~4 chars/token) for calls that don't report
    ``usage_metadata`` — chiefly embeddings, whose response carries a billable
    character count, not tokens. Only used as a fallback so embedding spend is
    approximated rather than lost. Handles str, list[str], and nested parts.
    """
    try:
        if contents is None:
            return 0
        if isinstance(contents, str):
            return max(1, len(contents) // 4)
        if isinstance(contents, (list, tuple)):
            return sum(_estimate_tokens(c) for c in contents)
        # Fall back to the stringified length for Part/Content-like objects.
        text = getattr(contents, "text", None)
        if isinstance(text, str):
            return max(1, len(text) // 4)
        return max(1, len(str(contents)) // 4)
    except Exception:  # noqa: BLE001
        return 0


def _embed_prompt_tokens(response, contents) -> int:
    """Best token estimate for an embedding call. The genai
    ``EmbedContentResponse`` has no ``usage_metadata`` — it reports
    ``metadata.billable_character_count`` — so prefer that (~4 chars/token) and
    fall back to estimating from the input length when it's absent.
    """
    try:
        meta = getattr(response, "metadata", None)
        bcc = getattr(meta, "billable_character_count", None)
        if bcc:
            return max(1, int(bcc) // 4)
    except Exception:  # noqa: BLE001
        pass
    return _estimate_tokens(contents)


class _InstrumentedModels:
    """Transparent wrapper around ``genai.Client.models`` that records token
    usage + dollar cost for every ``generate_content`` / ``embed_content`` call
    into the AI-Cost ledger (:mod:`rubik_core.cost.usage`).

    This is the single choke point: every direct-client feature (HubSpot intent
    planning, web search, daily briefing, priorities batch, EOS, Meeting/Deep
    Prep, X-Ray, embeddings, …) funnels through here, so cost
    attribution needs no per-call-site change — a feature only declares itself
    via ``rubik_core.cost.usage.feature_context``. Recording is best-effort and
    never alters the call's result or raises. Any attribute we don't override
    (streaming, count_tokens, etc.) delegates straight to the real object.
    """

    def __init__(self, real_models):
        self._real = real_models

    def __getattr__(self, name):
        # Delegate everything we don't explicitly wrap (count_tokens,
        # generate_content_stream, compute_tokens, …) to the real Models.
        return getattr(self._real, name)

    def generate_content(self, *args, **kwargs):
        response = self._real.generate_content(*args, **kwargs)
        try:
            from rubik_core.cost.usage import extract_usage, record_usage

            model = kwargs.get("model") or (args[0] if args else "")
            u = extract_usage(response)
            record_usage(
                operation="generate",
                model=model,
                prompt_tokens=u["prompt"],
                completion_tokens=u["completion"],
                cached_tokens=u["cached"],
                total_tokens=u["total"],
                thinking_tokens=u["thinking"],
            )
        except Exception:  # noqa: BLE001 — never let accounting break a call
            logger.debug("usage recording (generate) failed", exc_info=True)
        return response

    def embed_content(self, *args, **kwargs):
        response = self._real.embed_content(*args, **kwargs)
        try:
            from rubik_core.cost.usage import extract_usage, record_usage

            model = kwargs.get("model") or (args[0] if args else "")
            contents = kwargs.get("contents")
            if contents is None and len(args) > 1:
                contents = args[1]
            u = extract_usage(response)
            prompt = u["prompt"] or _embed_prompt_tokens(response, contents)
            record_usage(
                operation="embed",
                model=model,
                prompt_tokens=prompt,
                completion_tokens=0,
                total_tokens=prompt,
            )
        except Exception:  # noqa: BLE001
            logger.debug("usage recording (embed) failed", exc_info=True)
        return response


class _InstrumentedAsyncModels:
    """Async twin of :class:`_InstrumentedModels` for ``client.aio.models`` —
    the path Meeting Prep, Deep Prep, EOS, Slack summaries and the Gmail/email
    features actually use (``.aio.models.generate_content``). Without this, all
    async LLM spend would be missed.
    """

    def __init__(self, real_async_models):
        self._real = real_async_models

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def generate_content(self, *args, **kwargs):
        response = await self._real.generate_content(*args, **kwargs)
        try:
            from rubik_core.cost.usage import extract_usage, record_usage

            model = kwargs.get("model") or (args[0] if args else "")
            u = extract_usage(response)
            record_usage(
                operation="generate",
                model=model,
                prompt_tokens=u["prompt"],
                completion_tokens=u["completion"],
                cached_tokens=u["cached"],
                total_tokens=u["total"],
                thinking_tokens=u["thinking"],
            )
        except Exception:  # noqa: BLE001
            logger.debug("usage recording (async generate) failed", exc_info=True)
        return response

    async def embed_content(self, *args, **kwargs):
        response = await self._real.embed_content(*args, **kwargs)
        try:
            from rubik_core.cost.usage import extract_usage, record_usage

            model = kwargs.get("model") or (args[0] if args else "")
            contents = kwargs.get("contents")
            if contents is None and len(args) > 1:
                contents = args[1]
            u = extract_usage(response)
            prompt = u["prompt"] or _embed_prompt_tokens(response, contents)
            record_usage(
                operation="embed",
                model=model,
                prompt_tokens=prompt,
                completion_tokens=0,
                total_tokens=prompt,
            )
        except Exception:  # noqa: BLE001
            logger.debug("usage recording (async embed) failed", exc_info=True)
        return response


class _InstrumentedAio:
    """Proxy over ``client.aio`` that swaps in the instrumented async models."""

    def __init__(self, real_aio):
        self._real = real_aio
        self._models = _InstrumentedAsyncModels(real_aio.models)

    @property
    def models(self) -> _InstrumentedAsyncModels:
        return self._models

    def __getattr__(self, name):
        return getattr(self._real, name)


class _InstrumentedClient:
    """Thin proxy over ``genai.Client`` that swaps in :class:`_InstrumentedModels`
    for ``.models`` and :class:`_InstrumentedAio` for ``.aio``, delegating every
    other attribute (``.files``, ``.batches``, …) to the real client unchanged.
    """

    def __init__(self, real_client: genai.Client):
        self._real = real_client
        self._models = _InstrumentedModels(real_client.models)
        self._aio = _InstrumentedAio(real_client.aio)

    @property
    def models(self) -> _InstrumentedModels:
        return self._models

    @property
    def aio(self) -> _InstrumentedAio:
        return self._aio

    def __getattr__(self, name):
        return getattr(self._real, name)


def get_genai_client():
    """Get a cached, cost-instrumented Google GenAI SDK client.

    Returns an :class:`_InstrumentedClient` proxy: identical surface to
    ``genai.Client`` but every ``models.generate_content`` /
    ``models.embed_content`` call is logged to the AI-Cost ledger. Defaults to
    the location in VERTEX_LLM_LOCATION ("global"), which is required by every
    Gemini 3 preview model. Regional endpoints return a 404 not-found for those
    model IDs.
    """
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        # http_options gives every direct generate_content call (HubSpot intent
        # planning, web search, briefing, priorities batch, embeddings) SDK-native
        # backoff on transient 429/503/5xx plus a hang backstop. The timeout sits
        # above the 90s chat wall-clock, so it never changes chat behavior — it
        # only kills a truly stuck request in the batch paths (see config.py).
        real = genai.Client(
            vertexai=True,
            project=settings.GCP_PROJECT_ID,
            location=settings.VERTEX_LLM_LOCATION,
            http_options=types.HttpOptions(
                timeout=settings.GENAI_CLIENT_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(
                    attempts=settings.GENAI_CLIENT_RETRY_ATTEMPTS
                ),
            ),
        )
        _GENAI_CLIENT = _InstrumentedClient(real)
    return _GENAI_CLIENT


def safe_json(raw: str):
    """Parse LLM output as JSON, tolerating fences / leading prose.

    Strips ```json fences and falls back to the outermost {...}/[...] block.
    Raises ``ValueError`` if nothing parses.

    Lives here rather than in a feature package because three surfaces already
    need it (Deep Research, attendee intelligence, meeting prep) and a private
    copy per feature is how they drift.
    """
    import json
    import re

    if raw is None:
        raise ValueError("empty LLM response")
    text = raw.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("could not parse JSON from LLM output")


def llm_grounded(
    system: str,
    prompt: str,
    *,
    model_id: str = "",
    temperature: float = 0.1,
    thinking_level: str | None = None,
    timeout_s: float | None = None,
) -> tuple[str, list[str]]:
    """A Google-Search-grounded Gemini call. Returns ``(text, citation_urls)``.

    THE grounded pattern for the repo: attach the GoogleSearch tool and pull
    citation URLs out of ``grounding_metadata.grounding_chunks[].web.uri``.
    Citations are best-effort (an empty list is normal when the model answered
    without retrieving), so callers must not treat them as a success signal.

    ``thinking_level`` (Gemini 3.x: "low" / "medium" / "high"; "minimal" only
    below 3.7) is sent only when given. Left out, the model uses its own
    default, which on 3.8 Flash made one attendee lookup take 37-90+ s.

    ``timeout_s`` overrides the client's ``GENAI_CLIENT_TIMEOUT_MS`` (120 s) for
    this call only. A grounded call that runs past the client timeout is cut off
    and retried from scratch, so one that needs 180 s can never finish; a caller
    with its own wall-clock budget passes that budget here instead.
    """
    client = get_genai_client()
    res = client.models.generate_content(
        model=model_id or settings.VERTEX_LLM_MODEL_FLASH,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=temperature,
            thinking_config=(
                types.ThinkingConfig(thinking_level=thinking_level)
                if thinking_level
                else None
            ),
            http_options=(
                types.HttpOptions(timeout=int(timeout_s * 1000)) if timeout_s else None
            ),
        ),
    )
    urls: list[str] = []
    try:
        cands = res.candidates or []
        if cands and cands[0].grounding_metadata:
            for chunk in cands[0].grounding_metadata.grounding_chunks or []:
                web = getattr(chunk, "web", None)
                if web and getattr(web, "uri", None):
                    urls.append(web.uri)
    except Exception:  # noqa: BLE001 - citations are best-effort
        logger.debug("no grounding metadata on web response", exc_info=True)
    return (res.text or ""), urls


def check_configured_models() -> dict:
    """Probe each configured Gemini tier (LITE/FLASH/PRO) with a tiny request
    and report whether it still serves.

    Surfaces silent Google model-id changes loudly instead of as a user-facing
    outage: a configured ``*-preview`` id can be discontinued, a ``-latest``
    alias can be repointed, or a tier can land on a starved quota pool and start
    returning 429 RESOURCE_EXHAUSTED (the failure that masqueraded as a chat
    "connectivity issue" pre-UAT). Run on demand (the ``/api/health/models``
    diagnostic) — NOT at startup: three live calls would add cold-start latency
    and burn quota on every boot.

    Each ping carries the tier's REAL thinking knob (``gemini_tuning``), not a
    bare prompt. "The model serves" and "the model serves the call shape we
    actually send" are different claims: the thinking knob differs by family and
    is env-tunable, and google-genai does NOT reject a bad ``thinking_level``
    client-side — it warns, then Vertex 400s every real request while a bare
    ping still reports "ok". The ``thinking`` field in each result shows what was
    exercised, so a green check means the live config works end to end.
    """
    client = get_genai_client()
    tiers = {
        "lite": settings.VERTEX_LLM_MODEL_LITE,
        "flash": settings.VERTEX_LLM_MODEL_FLASH,
        "pro": settings.VERTEX_LLM_MODEL_PRO,
    }
    results: dict = {}
    all_ok = True
    for tier, model_id in tiers.items():
        thinking = thinking_config_for(model_id)
        # What this ping proves, in the payload — "ok" is otherwise ambiguous
        # about whether the tuned call shape or just the model id was checked.
        if thinking is None:
            exercised = "model default"
        elif thinking.thinking_level is not None:
            exercised = f"level={thinking.thinking_level.value.lower()}"
        else:
            exercised = f"budget={thinking.thinking_budget}"
        try:
            response = client.models.generate_content(
                model=model_id,
                contents="ping",
                config=generation_config_for(
                    model_id, types.GenerateContentConfig(max_output_tokens=1)
                ),
            )
            # What Vertex says served the request. Reported RAW — null when the
            # response carries no model_version, never defaulted to model_id.
            # The whole point is spotting a floating "-latest" alias that Google
            # has repointed to a new family (which would make the thinking knob
            # in gemini_tuning wrong, since that matches on the alias STRING and
            # can't see through it), and defaulting the field to the id we asked
            # for would make "no answer" indistinguishable from "no drift".
            #
            # Observed 2026-08-12 on the global endpoint: Vertex echoes the
            # requested id, including for "gemini-flash-latest" — so this does
            # NOT currently resolve aliases. Kept because it costs nothing and
            # goes truthy the moment Google starts returning a concrete version.
            served = getattr(response, "model_version", None)
            results[tier] = {
                "model": model_id,
                "served": served,
                "resolves_alias": bool(served) and served != model_id,
                "status": "ok",
                "thinking": exercised,
            }
        except Exception as e:  # noqa: BLE001 — a diagnostic must never raise
            all_ok = False
            msg = str(e)
            if "404" in msg or "NOT_FOUND" in msg:
                kind = "not_found"  # id deprecated / rotated by Google
            elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                kind = "quota_exhausted"  # starved pool — likely a preview id
            elif "400" in msg or "INVALID_ARGUMENT" in msg:
                # The model id is fine, the call shape isn't — nearly always the
                # thinking knob (a level this family doesn't offer, e.g.
                # "minimal" on a Pro tier). Named separately because the fix is
                # CHAT_LLM_THINKING_LEVEL, not the model id.
                kind = "bad_request"
            else:
                kind = "error"
            logger.warning(
                "Model health check FAILED — %s tier (model=%s, thinking=%s): %s — %s",
                tier,
                model_id,
                exercised,
                kind,
                msg[:200],
            )
            results[tier] = {
                "model": model_id,
                "status": kind,
                "thinking": exercised,
                "error": msg[:200],
            }
    return {
        "ok": all_ok,
        "location": settings.VERTEX_LLM_LOCATION,
        "models": results,
    }


class GenAIEmbeddings:
    """Wrapper for GenAI embeddings to maintain compatibility with embed_query."""

    def __init__(self, model_id: str):
        self.model_id = model_id
        self.client = get_genai_client()

    def embed_query(self, text: str) -> list[float]:
        response = self.client.models.embed_content(model=self.model_id, contents=text)
        return response.embeddings[0].values

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed a list of texts. The GenAI SDK's embed_content accepts a
        list and returns one embedding per element."""
        if not texts:
            return []
        response = self.client.models.embed_content(
            model=self.model_id,
            contents=texts,
        )
        return [e.values for e in response.embeddings]


def get_embeddings(model_key: str = "quality"):
    from rubik_core.ai_providers.embedding_index import active_embedding_index
    from rubik_core.ai_providers.embeddings import WorkspaceEmbeddings
    from rubik_core.ai_providers.registry import IncompatibleModelRoute
    from rubik_core.config import AppMode

    spec = active_embedding_index()
    if settings.APP_MODE == AppMode.GCP and spec.provider != "vertex_managed":
        raise IncompatibleModelRoute(
            "Client embeddings require a separately provisioned vector index; Vertex RAG remains managed"
        )
    return WorkspaceEmbeddings(spec)


# Legacy get_llm removed in favor of direct genai client usage in routers
