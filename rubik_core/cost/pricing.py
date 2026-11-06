"""LLM rate card + dollar-cost calculator — the single source of truth for
every dollar figure shown in Admin → AI Cost.

Rates are **USD per 1,000,000 tokens**, split into input
(prompt), output (completion, and the model's "thinking" tokens, which Vertex
bills at the output rate) and — where the model bills it separately —
cached-input tokens. Embedding models bill on input tokens only.

⚠️  These are best-effort estimates transcribed from Google's public Vertex
pricing. They WILL drift as Google changes prices or as the ``-latest``
aliases repoint to a new family. Treat this table as the knob to turn:

* Correct a rate here and every *future* ``LLMUsageEvent`` uses it.
* Historical rows keep the ``cost_usd`` computed at write time, so past
  totals are not silently rewritten when a rate changes.
* To change rates without editing code, set the ``LLM_PRICING_JSON`` env var
  to a JSON object of ``{"model-id-or-prefix": {"input": x, "output": y,
  "cached_input": z}}`` — it is merged over the defaults below.

The Admin API surfaces this resolved rate card verbatim (see
``/api/admin/ai-cost/rate-card``) with the ``estimated`` disclaimer, so the
numbers on screen are always explainable and auditable.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

# ── Default rate card (USD per 1M tokens) ────────────────────────────────
# Keys are matched against the raw model id by longest-prefix, so an entry
# like "gemini-2.5-flash" also covers "gemini-2.5-flash-002". Order-independent
# — resolution always picks the LONGEST matching key (see _resolve_rate).
#
# Sources (Vertex AI generative-AI pricing, transcribed 2026-07; verify at
# https://cloud.google.com/vertex-ai/generative-ai/pricing before trusting to
# the cent). "cached_input" is the discounted rate for context-cache hits.
_DEFAULT_RATES: dict[str, dict[str, float]] = {
    # Client-owned provider estimates. Source pages (checked 2026-10-30):
    # https://platform.claude.com/docs/en/models/sonnet-4-6/overview
    # https://api-docs.deepseek.com/quick_start/pricing/ (peak V4 rate)
    # https://developers.openai.com/api/docs/models/gpt-6-luna
    # https://dev.meta.ai/docs/pricing-rate-limits
    # Client invoices may include tiers, discounts, taxes and other usage.
    "anthropic:claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cached_input": 0.30,
    },
    "deepseek:deepseek-v4-pro": {"input": 1.32, "output": 3.96, "cached_input": 0.044},
    "openai:gpt-6-luna": {"input": 0.10, "output": 0.50, "cached_input": 0.01},
    "meta:muse-spark-1.3": {"input": 1.25, "output": 4.25, "cached_input": 0.15},
    # ── Gemini 2.5 family ──
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cached_input": 0.31},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cached_input": 0.025},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.075},
    # ── Gemini 3 / 3.1 family ──
    "gemini-3.1-pro": {"input": 2.00, "output": 12.00, "cached_input": 0.50},
    "gemini-3-pro": {"input": 2.00, "output": 12.00, "cached_input": 0.50},
    "gemini-3.1-flash-lite": {"input": 0.10, "output": 0.40, "cached_input": 0.025},
    "gemini-3.1-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.075},
    "gemini-3-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.075},
    # ── Gemini 3.5 / 3.6 family ──
    # Priced at their tier's going rate (Flash-Lite / Flash). Both ids have been
    # selectable in Admin → Chat and 3.5 Flash-Lite is now the
    # LITE tier, but neither was on the rate card — so every call on them fell
    # through to _FALLBACK_RATE (a mid-Flash rate) and reported Lite spend at
    # roughly 3x input / 6x output. Correct these against the live pricing page
    # when convenient; LLM_PRICING_JSON overrides without a deploy.
    "gemini-3.5-flash-lite": {"input": 0.10, "output": 0.40, "cached_input": 0.025},
    "gemini-3.5-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.075},
    "gemini-3.6-flash-lite": {"input": 0.10, "output": 0.40, "cached_input": 0.025},
    "gemini-3.6-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.075},
    # ── Gemini 3.7 / 3.8 family ──
    # 3.8 Flash became the FLASH tier on 2026-09-08 and 3.7 Flash also serves, so
    # both are listed to keep them off _FALLBACK_RATE. Google's notice said 3.8
    # ships at the same cost as 3.7 but published no figure we transcribed, so
    # these carry the Flash tier's going rate as a PLACEHOLDER, exactly like the
    # 3.5/3.6 rows above — treat per-call dollars on them as approximate until
    # someone checks the live pricing page. LLM_PRICING_JSON corrects it without
    # a deploy. There is no 3.8 Flash-Lite or 3.8 Pro id to price (both 404).
    "gemini-3.7-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.075},
    "gemini-3.8-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.075},
    # ── Gemini 4 ──
    # INTRODUCTORY price, from Google's Gemini 4 Argon launch email (2026-10):
    # $2 input / $10 output per 1M, cached input 95% off. Third-party reports
    # say it doubles to $4 / $20 after an intro period of unstated length, and
    # the email gave no long-context tier and did not say whether Vertex matches
    # the Gemini API price. When the price changes, set LLM_PRICING_JSON
    # ({"gemini-4-argon": {"input": 4, "output": 20, "cached_input": 0.2}})
    # rather than waiting on a deploy. No tier pins Argon (probed serving
    # 2026-10-14; see config.py) — it is priced so the PRO-tier replay benchmark
    # and any trial call cost correctly instead of at _FALLBACK_RATE.
    "gemini-4-argon": {"input": 2.00, "output": 10.00, "cached_input": 0.10},
    # ── Floating GA aliases (mapped to their current underlying family) ──
    # config.py pins FLASH → "gemini-3.8-flash" and LITE → "gemini-3.5-flash-
    # lite", so no tier rides these today. Keep them priced explicitly anyway so
    # a call logged under an alias id still costs correctly — they stay
    # selectable in Admin → Chat as roll-back options.
    "gemini-flash-latest": {"input": 0.30, "output": 2.50, "cached_input": 0.075},
    "gemini-flash-lite-latest": {"input": 0.10, "output": 0.40, "cached_input": 0.025},
    "gemini-pro-latest": {"input": 2.00, "output": 12.00, "cached_input": 0.50},
    # ── Embeddings (input tokens only; output/cached irrelevant) ──
    "text-embedding-005": {"input": 0.025, "output": 0.0, "cached_input": 0.0},
    "text-embedding-004": {"input": 0.025, "output": 0.0, "cached_input": 0.0},
    "text-multilingual-embedding": {"input": 0.025, "output": 0.0, "cached_input": 0.0},
    "gemini-embedding": {"input": 0.025, "output": 0.0, "cached_input": 0.0},
}

# Last-resort fallback when a model id matches nothing above. Deliberately
# non-zero so an unpriced model surfaces as a visible (if approximate) cost
# rather than silently reading $0 and hiding real spend. Tuned to a mid Flash
# rate. Unpriced models are flagged in the rate-card API payload.
_FALLBACK_RATE: dict[str, float] = {
    "input": 0.30,
    "output": 2.50,
    "cached_input": 0.075,
}


@lru_cache(maxsize=1)
def get_rate_card() -> dict[str, dict[str, float]]:
    """Resolved rate card: defaults merged with the ``LLM_PRICING_JSON`` env
    override (if set and valid). Cached — call :func:`reset_rate_card_cache`
    after mutating the env in tests.
    """
    rates = {k: dict(v) for k, v in _DEFAULT_RATES.items()}
    raw = os.getenv("LLM_PRICING_JSON", "").strip()
    if not raw:
        return rates
    try:
        override = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning("LLM_PRICING_JSON failed to parse (%s) — using defaults.", e)
        return rates
    if not isinstance(override, dict):
        logger.warning("LLM_PRICING_JSON is not a JSON object — ignored.")
        return rates

    # Merge each entry independently: validate every known rate key into a temp
    # dict FIRST and only commit if the whole entry is clean. A malformed value
    # skips just that one entry (logged) instead of aborting the loop and
    # caching a half-applied card. Unknown keys (e.g. a descriptive note) are
    # ignored, not treated as rates.
    known = ("input", "output", "cached_input")
    for model_id, entry in override.items():
        if not isinstance(entry, dict):
            logger.warning(
                "LLM_PRICING_JSON['%s'] is not an object — skipped.", model_id
            )
            continue
        clean: dict[str, float] = {}
        bad = False
        for k, v in entry.items():
            if k not in known:
                continue
            try:
                clean[k] = float(v)
            except (ValueError, TypeError):
                logger.warning(
                    "LLM_PRICING_JSON['%s'].%s is not numeric — entry skipped.",
                    model_id,
                    k,
                )
                bad = True
                break
        if bad or not clean:
            continue
        merged = dict(rates.get(model_id, {}))
        merged.update(clean)
        rates[model_id] = merged
    return rates


def reset_rate_card_cache() -> None:
    """Clear the cached rate card (tests that mutate ``LLM_PRICING_JSON``)."""
    get_rate_card.cache_clear()


def _resolve_rate(
    model: str, provider: str | None = None
) -> tuple[dict[str, float], bool]:
    """Return ``(rate_entry, matched)`` for a raw model id.

    Longest-prefix match against the rate card so "gemini-2.5-flash-002"
    resolves to the "gemini-2.5-flash" entry, not the broader alias. Returns
    the fallback rate with ``matched=False`` when nothing matches.
    """
    if not model:
        return _FALLBACK_RATE, False
    m = model.strip().lower()
    # Strip a leading "models/" or "publishers/google/models/" qualifier the
    # SDK sometimes prepends, and any "@version" / "-preview" suffix noise is
    # handled naturally by prefix matching.
    m = m.rsplit("/", 1)[-1]
    if provider and provider not in ("vertex_managed", "vertex_client", "gemini"):
        m = f"{provider}:{m}"
    best_key: str | None = None
    for key in get_rate_card():
        if m.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is None:
        return _FALLBACK_RATE, False
    return get_rate_card()[best_key], True


def compute_cost_usd(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    thinking_tokens: int = 0,
    *,
    provider: str | None = None,
) -> float:
    """Dollar cost of one call given its token counts.

    ``cached_tokens`` are billed at the discounted cached-input rate and are
    assumed to be a subset already included in ``prompt_tokens`` (that is how
    Vertex reports ``cached_content_token_count``), so the non-cached prompt
    portion is ``prompt_tokens - cached_tokens``.

    ``thinking_tokens`` (Vertex ``thoughts_token_count``) are NOT a subset of
    ``completion_tokens``: Vertex reports them as their own slice
    (``total == prompt + candidates + thoughts``, verified 2026-10-14) and bills
    them at the OUTPUT rate. Before they were passed here, a PRO call with
    dynamic thinking was priced at a quarter or less of its bill.

    Always returns >= 0.0 and never raises — cost accounting must not break a
    live call path.
    """
    try:
        rate, _matched = _resolve_rate(model, provider)
        cached = max(0, int(cached_tokens))
        prompt = max(0, int(prompt_tokens))
        completion = max(0, int(completion_tokens))
        thinking = max(0, int(thinking_tokens or 0))
        non_cached_prompt = max(0, prompt - cached)

        cost = (
            non_cached_prompt * rate.get("input", 0.0)
            + cached * rate.get("cached_input", rate.get("input", 0.0))
            + (completion + thinking) * rate.get("output", 0.0)
        ) / 1_000_000.0
        return round(max(0.0, cost), 8)
    except Exception:  # noqa: BLE001 — cost must never raise into a call path
        logger.debug("compute_cost_usd failed for model=%s", model, exc_info=True)
        return 0.0


def is_priced(model: str, *, provider: str | None = None) -> bool:
    """True when ``model`` has an explicit rate-card entry (not the fallback)."""
    _rate, matched = _resolve_rate(model, provider)
    return matched
