"""AI cost accounting for Cubie.

Two public surfaces:

* :mod:`rubik_core.cost.pricing` — the per-model rate card and the
  ``cost_usd`` calculator. Single source of truth for dollar figures.
* :mod:`rubik_core.cost.usage` — the feature-attribution context var and the
  best-effort ``record_usage`` recorder that writes one ``LLMUsageEvent`` row
  per LLM call. The shared google-genai client (``rubik_core.models``) is
  wrapped so every ``generate_content`` / ``embed_content`` call is captured
  automatically; features only need to declare *which* feature they are via
  :func:`rubik_core.cost.usage.feature_context`.
"""

from rubik_core.cost.pricing import compute_cost_usd, get_rate_card
from rubik_core.cost.usage import (
    attribute_to,
    current_feature,
    default_feature,
    feature_context,
    record_usage,
)

__all__ = [
    "compute_cost_usd",
    "get_rate_card",
    "attribute_to",
    "current_feature",
    "default_feature",
    "feature_context",
    "record_usage",
]
