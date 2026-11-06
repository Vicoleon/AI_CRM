"""The AI summary of one Insights view ("Summarize this view").

One Gemini Flash call, made only when someone asks, cached for an hour by a
hash of exactly what was sent (the same view twice costs once). Its own AI
Cost category, ``insights_summary``.

What the model is given — and what it is NOT:

* The view's KPIs with their catalog label, definition, numerator and
  denominator, the department-level breakdowns (bars, the funnel, matrices)
  and the coverage notes.
* NEVER a person. Tables are dropped, any breakdown drilled by person is
  dropped, and any string containing an ``@`` is dropped as a last guard, so
  no name or email can reach the prompt (README §4).

What the reader is given: a headline, three to five bullets and up to three
"watch" items, in business language. Every number must come from the input,
and a rate must say what it is out of ("12 of 15 people"). The prompt asks for
that; :func:`grounded` enforces it — any bullet carrying a number that is not
in the input (or a rounded or percentage form of one) is dropped: dropping
beats correcting, because a sentence whose evidence cannot be checked has
nothing left to stand on.

Any failure (no model access, bad JSON, nothing left after the check) answers
``{"available": False, "reason": …}``; the page shows the numbers regardless.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import ProviderRequestError, generate_text_on_route
from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.profile import company_name_or_default, get_company_profile
from rubik_core.cost.usage import attribute_to
from rubik_core.insights.cache import cached
from rubik_core.insights.catalog import METRICS

logger = logging.getLogger(__name__)

COST_FEATURE = "insights_summary"
CACHE_TTL_SECONDS = 3600
MAX_BULLETS = 5
MAX_WATCH = 3

#: A figure in the model's prose: digits not glued to a letter, thousands
#: separators allowed ("1,234"), an optional decimal part.
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![A-Za-z0-9.])\d+(?:\.\d+)?"
)
_EPSILON = 1e-6


class SummaryUnavailable(RuntimeError):
    """The summary could not be produced; ``str(exc)`` is safe to show."""


# ── what the model sees ─────────────────────────────────────────────────────


def _pct(value: Any) -> float | None:
    return round(float(value) * 100, 1) if isinstance(value, (int, float)) else None


def _kpi_input(k: dict) -> dict:
    m = METRICS.get(k.get("id", ""))
    unit = m.unit if m else None
    value, prev = k.get("value"), k.get("prev_value")
    out: dict[str, Any] = {
        "metric": m.label if m else k.get("id"),
        "definition": m.definition if m else "",
        "unit": unit,
        "value": value,
        "previous_period_value": prev,
    }
    if unit == "pct":
        out["value_percent"] = _pct(value)
        out["previous_period_percent"] = _pct(prev)
        if isinstance(value, (int, float)) and isinstance(prev, (int, float)):
            out["change_percentage_points"] = round((value - prev) * 100, 1)
    elif isinstance(value, (int, float)) and isinstance(prev, (int, float)):
        out["change"] = round(value - prev, 2)
    if "numerator" in k or "denominator" in k:
        out["numerator"] = k.get("numerator")
        out["denominator"] = k.get("denominator")
        if m and m.denominator:
            out["out_of"] = m.denominator
    return out


def _items_input(items: list[dict], unit: str | None) -> list[dict]:
    out = []
    for it in items:
        row: dict[str, Any] = {"label": it.get("label"), "value": it.get("value")}
        if unit == "pct":
            row["value_percent"] = _pct(it.get("value"))
        if "numerator" in it or "denominator" in it:
            row["numerator"] = it.get("numerator")
            row["denominator"] = it.get("denominator")
        out.append(row)
    return out


def _section_input(s: dict) -> dict | None:
    kind, data = s.get("kind"), s.get("data") or {}
    if kind in ("table", "timeseries") or s.get("drill") == "person":
        return None
    base = {"title": s.get("title"), "kind": kind}
    if s.get("subtitle"):
        base["about"] = s["subtitle"]
    if kind == "bar":
        return {
            **base,
            "unit": data.get("unit"),
            "items": _items_input(data.get("items") or [], data.get("unit")),
        }
    if kind == "funnel":
        return {
            **base,
            "steps": [
                {"label": st.get("label"), "value": st.get("value")}
                for st in data.get("steps") or []
            ],
        }
    if kind == "matrix":
        unit = data.get("unit")
        cols = [c.get("label") for c in data.get("cols") or []]
        rows = []
        for r, cells in zip(
            data.get("rows") or [], data.get("cells") or [], strict=False
        ):
            row_cells = {}
            for label, c in zip(cols, cells, strict=False):
                if c is None:
                    continue
                entry: dict[str, Any] = {"value": c.get("value")}
                if unit == "pct":
                    entry["value_percent"] = _pct(c.get("value"))
                if "numerator" in c:
                    entry["numerator"] = c.get("numerator")
                    entry["denominator"] = c.get("denominator")
                row_cells[label] = entry
            rows.append({"row": r.get("label"), "cells": row_cells})
        return {**base, "unit": unit, "rows": rows}
    return None


def _scrub(value: Any) -> Any:
    """Drop any string that looks like it carries an email address."""
    if isinstance(value, str):
        return None if "@" in value else value
    if isinstance(value, dict):
        return {
            k: _scrub(v)
            for k, v in value.items()
            if not (isinstance(v, str) and "@" in v)
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value if not (isinstance(v, str) and "@" in v)]
    return value


def summary_input(payload: dict) -> dict:
    """The department-level slice of a dashboard payload the model may see."""
    period = payload.get("period") or {}
    scope = payload.get("scope") or {}
    sections = [
        x for x in (_section_input(s) for s in payload.get("sections") or []) if x
    ]
    return _scrub(
        {
            "dashboard": payload.get("title"),
            "period": {
                "label": period.get("label"),
                "days": period.get("days"),
                "compared_with": "the same number of days just before",
            },
            "scope": {
                "departments": scope.get("labels") or [],
                "whole_company": bool(scope.get("all")),
                "people_in_scope": scope.get("people"),
            },
            "kpis": [_kpi_input(k) for k in payload.get("kpis") or []],
            "breakdowns": sections,
            "notes": payload.get("notes") or [],
        }
    )


# ── the number guard ────────────────────────────────────────────────────────


def _harvest(value: Any, into: set[float], *, key: str | None = None) -> None:
    """Every number in the input's numeric fields. Strings are NOT scanned:
    a date or a label must not license a figure (capacity.py's lesson)."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        into.add(abs(float(value)))
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _harvest(v, into, key=k)
    elif isinstance(value, list):
        for v in value:
            _harvest(v, into, key=key)


def allowed_numbers(inp: dict) -> set[float]:
    raw: set[float] = set()
    _harvest(inp, raw)
    allowed: set[float] = set()
    for v in raw:
        allowed |= {v, float(round(v)), float(round(v, 1)), float(round(v, 2))}
        if v <= 1:  # a 0–1 rate quoted as a percentage
            pct = v * 100
            allowed |= {float(round(pct)), float(round(pct, 1))}
    return allowed


def numbers_in(text: str) -> list[float]:
    out = []
    for match in _NUMBER_RE.findall(text or ""):
        try:
            out.append(abs(float(match.replace(",", ""))))
        except ValueError:  # pragma: no cover — the regex guarantees a number
            continue
    return out


def grounded(text: str, allowed: set[float]) -> bool:
    return all(any(abs(n - ok) <= _EPSILON for ok in allowed) for n in numbers_in(text))


# ── the call ────────────────────────────────────────────────────────────────


def build_prompt(inp: dict) -> str:
    return (
        "You write a short briefing for the leadership of "
        f"{company_name_or_default(get_company_profile())} about one dashboard in "
        f"{PRODUCT_NAME} Insights ({PRODUCT_NAME} is the company's internal AI assistant). The data is "
        "below as JSON.\n\n"
        "Rules:\n"
        "- Plain business language for executives. No jargon, no technical terms.\n"
        "- Every number you write must appear in the data exactly (you may round "
        "a value or use its *_percent form). Do not calculate new numbers.\n"
        "- Whenever you give a rate or share, say what it is out of, using the "
        "numerator and denominator given, e.g. '12 of 15 people (80%)'.\n"
        "- Never name or describe an individual person.\n"
        "- Say what changed versus the previous period when the data shows it. "
        "Do not guess at causes the data does not show.\n"
        "- 'headline': one sentence. 'bullets': 3 to 5 short findings, most "
        "important first. 'watch': up to 3 things leadership should keep an eye "
        "on (may be empty).\n\n"
        'Return ONLY JSON: {"headline": "...", "bullets": ["..."], "watch": ["..."]}\n\n'
        f"DATA:\n{json.dumps(inp, ensure_ascii=False, sort_keys=True)}"
    )


def _managed_invoke(prompt: str, model: str) -> str:
    from google.genai import types as genai_types

    from rubik_core.gemini_tuning import generation_config_for
    from rubik_core.models import get_genai_client

    response = get_genai_client().models.generate_content(
        model=model,
        contents=prompt,
        config=generation_config_for(
            model,
            genai_types.GenerateContentConfig(
                temperature=0.2, response_mime_type="application/json"
            ),
        ),
    )
    return response.text or ""


def _default_invoke(prompt: str) -> str:
    """The existing managed Vertex Flash path, retained for the default route."""
    from rubik_core.config import settings

    return _managed_invoke(prompt, settings.VERTEX_LLM_MODEL_FLASH)


def _selected_invoke(route: ModelRoute, prompt: str) -> str:
    """Use the route selected before cache lookup; never switch providers."""
    from rubik_core.config import settings

    if route.provider == "vertex_managed":
        live = resolve_model_route(
            COST_FEATURE, "json", default_model=settings.VERTEX_LLM_MODEL_FLASH
        )
        if live != route:
            raise IncompatibleModelRoute("Workspace AI route changed")
        if route.model == settings.VERTEX_LLM_MODEL_FLASH:
            return _default_invoke(prompt)
        return _managed_invoke(prompt, route.model)
    return generate_text_on_route(route, prompt, json_mode=True).text


def _parse(raw: Any) -> dict:
    data = raw
    if isinstance(raw, str):
        text = (
            raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        )
        try:
            data = json.loads(text.strip())
        except ValueError:
            raise SummaryUnavailable(
                "The AI returned something that was not a summary."
            ) from None
    if not isinstance(data, dict):
        raise SummaryUnavailable("The AI returned something that was not a summary.")
    return data


def _strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, str) and v.strip()][
        : limit * 2
    ]


@attribute_to(COST_FEATURE)
def _generate(inp: dict, invoke: Callable[[str], Any]) -> dict:
    data = _parse(invoke(build_prompt(inp)))
    allowed = allowed_numbers(inp)

    def keep(texts: list[str], limit: int, label: str) -> list[str]:
        kept = []
        for t in texts:
            if "@" in t:
                continue
            if grounded(t, allowed):
                kept.append(t)
            else:
                logger.info(
                    "insights summary: dropped a %s with an unsupported number", label
                )
        return kept[:limit]

    bullets = keep(_strings(data.get("bullets"), MAX_BULLETS), MAX_BULLETS, "bullet")
    watch = keep(_strings(data.get("watch"), MAX_WATCH), MAX_WATCH, "watch item")
    headline = str(data.get("headline") or "").strip()
    if not headline or "@" in headline or not grounded(headline, allowed):
        headline = bullets[0] if bullets else ""
    if not headline and not bullets:
        raise SummaryUnavailable("The AI summary did not hold up against the numbers.")
    return {
        "available": True,
        "headline": headline,
        "bullets": bullets,
        "watch": watch,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def summarize_payload(
    payload: dict,
    *,
    invoke: Callable[[str], Any] | None = None,
    refresh: bool = False,
) -> dict:
    """The summary for one dashboard payload. Never raises."""
    inp = summary_input(payload)
    digest = hashlib.sha256(
        json.dumps(inp, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    try:
        if invoke is None:
            from rubik_core.config import settings

            route = resolve_model_route(
                COST_FEATURE, "json", default_model=settings.VERTEX_LLM_MODEL_FLASH
            )

            def fn(prompt: str) -> str:
                return _selected_invoke(route, prompt)

            # A summary generated on one provider/model must never be reused
            # after the admin changes this workspace's selected route.
            cache_route: Any = route
        else:
            route = None
            fn = invoke
            cache_route = ("injected", id(invoke))
        value, was_cached = cached(
            ("insights-summary", digest, cache_route),
            lambda: _generate(inp, fn),
            refresh=refresh,
            ttl=CACHE_TTL_SECONDS,
        )
    except (IncompatibleModelRoute, ProviderRequestError):
        return {
            "available": False,
            "reason": "The selected AI provider is unavailable. Ask a workspace admin to check Admin → Workspace → AI provider credentials.",
        }
    except SummaryUnavailable as exc:
        if route is not None and route.provider != "vertex_managed":
            return {
                "available": False,
                "reason": "The selected AI provider did not return a usable summary. Ask a workspace admin to check its model in Admin → Workspace.",
            }
        return {"available": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 — the page works without a summary
        logger.warning("insights summary failed: %s", type(exc).__name__)
        return {
            "available": False,
            "reason": "The AI summary is not available right now.",
        }
    return {**value, "cached": was_cached}
