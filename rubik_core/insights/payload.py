"""Builders for the Insights payload JSON (contract §4, field for field).

Dashboards assemble plain dicts through these helpers rather than by hand so
the shapes the frontend renders (Kpi, Section and each section ``kind``) are
written in one place. Rounding happens here too: a percentage is a 0–1 value
to four places, money to cents, a ratio to two places.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from rubik_core.insights.catalog import metric


@dataclass(frozen=True)
class Measure:
    """One computed number: its value and, for a rate, what it is out of."""

    value: float | int | None
    numerator: float | int | None = None
    denominator: float | int | None = None


def ratio(numerator: float | int, denominator: float | int) -> Measure:
    """``numerator / denominator`` carried with both parts; None over zero."""
    value = (numerator / denominator) if denominator else None
    return Measure(value, numerator, denominator)


def round_for(unit: str, value: float | int | None) -> float | int | None:
    if value is None:
        return None
    if unit == "pct" or unit == "score":
        return round(float(value), 4)
    if unit == "usd":
        return round(float(value), 2)
    if unit in ("ratio", "days"):
        return round(float(value), 2)
    if unit == "ms":
        return int(round(float(value)))
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return round(float(value), 2) if isinstance(value, float) else value


def _clean_part(value: float | int | None) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 2)
    return value


def kpi(
    metric_id: str,
    current: Measure,
    previous: Measure | None = None,
    spark: Iterable[Measure | float | int | None] = (),
    href: str | None = None,
) -> dict:
    """A Kpi dict. ``metric_id`` must be in the catalog (KeyError otherwise)."""
    unit = metric(metric_id).unit
    out: dict[str, Any] = {
        "id": metric_id,
        "value": round_for(unit, current.value),
        "prev_value": round_for(unit, previous.value) if previous else None,
        "spark": [
            round_for(unit, s.value if isinstance(s, Measure) else s) for s in spark
        ],
    }
    if current.numerator is not None or current.denominator is not None:
        out["numerator"] = _clean_part(current.numerator)
        out["denominator"] = _clean_part(current.denominator)
    elif unit == "pct":
        # The rule is that every percentage says what it is out of; a rate
        # with nothing to show is reported as 0 of 0 rather than a bare value.
        out["numerator"] = 0
        out["denominator"] = 0
    if href:
        out["href"] = href
    return out


def kpi_over(
    metric_id: str,
    fn: Callable[[Any], Measure],
    period: Any,
    href: str | None = None,
    *,
    tracked_from: date | None | bool = False,
) -> dict:
    """A Kpi computed by ``fn(window)`` for the period, the previous period and
    each chart bucket — the common case.

    ``tracked_from`` is for a number whose source only started recording
    recently (``activity_daily``, ``priority_events``). A window that begins
    before tracking started has no honest value — it would read as zero and
    turn the first real figure into a "+120" jump — so the previous value and
    those spark points become None. Pass the first tracked day, or None when
    nothing has been recorded yet; leave it False for sources with full history.
    """

    def untracked(win: Any) -> bool:
        if tracked_from is False:
            return False
        return tracked_from is None or win.start < tracked_from

    return kpi(
        metric_id,
        fn(period.current),
        None if untracked(period.previous) else fn(period.previous),
        [None if untracked(b.window) else fn(b.window) for b in period.buckets],
        href,
    )


def section(
    section_id: str,
    kind: str,
    title: str,
    data: dict,
    *,
    width: str = "half",
    drill: str | None = None,
    subtitle: str | None = None,
    empty_hint: str | None = None,
) -> dict:
    out: dict[str, Any] = {
        "id": section_id,
        "kind": kind,
        "title": title,
        "width": width,
        "drill": drill,
        "data": data,
    }
    if subtitle:
        out["subtitle"] = subtitle
    if empty_hint:
        out["empty_hint"] = empty_hint
    return out


def series(key: str, label: str, unit: str, values: Sequence[Any]) -> dict:
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "values": [round_for(unit, v) for v in values],
    }


def timeseries(
    x: Sequence[str],
    series_list: list[dict],
    *,
    chart: str = "line",
    stacked: bool = False,
) -> dict:
    return {"x": list(x), "chart": chart, "stacked": stacked, "series": series_list}


def bar_item(
    key: str,
    label: str,
    value: float | int | None,
    unit: str,
    numerator: float | int | None = None,
    denominator: float | int | None = None,
) -> dict:
    out: dict[str, Any] = {"key": key, "label": label, "value": round_for(unit, value)}
    if numerator is not None or denominator is not None:
        out["numerator"] = _clean_part(numerator)
        out["denominator"] = _clean_part(denominator)
    return out


def bar(items: list[dict], unit: str, *, sort: bool = True) -> dict:
    if sort:
        items = sorted(
            items,
            key=lambda i: (i["value"] is None, -(i["value"] or 0), i["label"]),
        )
    return {"unit": unit, "items": items}


def funnel(steps: list[tuple[str, str, int]], *, mode: str = "sequential") -> dict:
    """``sequential``: each step is a subset of the one before (a true funnel).
    ``breakdown``: the first step is the whole and the rest split it into
    outcomes that add up to it, so a step-to-step rate would be meaningless."""
    return {
        "mode": mode,
        "steps": [{"key": k, "label": lbl, "value": v} for k, lbl, v in steps],
    }


#: Shown instead of a department-level figure over too few people to be
#: anonymous (``InsightsContext.hide_small``). No numerator or denominator is
#: sent either: "1 of 1" is the person.
HIDDEN_NOTE = "Hidden: fewer than 3 people"


def hidden_cell() -> dict:
    return {"value": None, "note": HIDDEN_NOTE}


def hidden_bar_item(key: str, label: str) -> dict:
    return {"key": key, "label": label, "value": None, "note": HIDDEN_NOTE}


def cell(measure: Measure | None, unit: str) -> dict | None:
    if measure is None or measure.value is None:
        return None
    out: dict[str, Any] = {"value": round_for(unit, measure.value)}
    if measure.numerator is not None or measure.denominator is not None:
        out["numerator"] = _clean_part(measure.numerator)
        out["denominator"] = _clean_part(measure.denominator)
    return out


def matrix(
    rows: list[tuple[str, str]],
    cols: list[tuple[str, str]],
    cells: list[list[dict | None]],
    *,
    unit: str,
    scale: str = "sequential",
) -> dict:
    return {
        "unit": unit,
        "scale": scale,
        "rows": [{"key": k, "label": lbl} for k, lbl in rows],
        "cols": [{"key": k, "label": lbl} for k, lbl in cols],
        "cells": cells,
    }


def table(
    columns: list[tuple[str, str, str]],
    rows: list[dict],
    *,
    row_link: str | None = "person",
    default_sort: tuple[str, str] | None = None,
) -> dict:
    """``columns`` are ``(key, label, unit)``; each row is
    ``{"key": …, "cells": {column key: value}}``."""
    units = {k: u for k, _l, u in columns}
    clean_rows = []
    for row in rows:
        cells = {
            k: (
                round_for(units[k], v)
                if units.get(k) not in ("text", "date", "datetime")
                else v
            )
            for k, v in row["cells"].items()
        }
        clean_rows.append({"key": row["key"], "cells": cells})
    out: dict[str, Any] = {
        "columns": [{"key": k, "label": lbl, "unit": u} for k, lbl, u in columns],
        "rows": clean_rows,
        "row_link": row_link,
    }
    if default_sort:
        out["default_sort"] = {"key": default_sort[0], "dir": default_sort[1]}
    return out


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50)


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank-with-interpolation percentile; None for no values."""
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return float(data[0])
    rank = (pct / 100.0) * (len(data) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(data) - 1)
    frac = rank - lo
    return float(data[lo] + (data[hi] - data[lo]) * frac)
