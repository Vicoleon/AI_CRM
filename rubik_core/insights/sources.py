"""Helpers the Phase-2 dashboards share: department-only sources, and sources
whose history starts partway through a period.

**Department-only sources** (README §1). Google Meet's terms forbid tracking
individuals' call use, so anything that comes from a recorded call (Call
Capture write-backs, the call scripts attached to a call, the Sales Cockpit
commitments a call wrap-up opens) is shown per department and never against a
name. A department figure can still single someone out when the group behind
it is tiny, so such a figure is withheld FOR EVERY VIEWER, admins included,
when it covers fewer than three people. The group is the department's people
who can reach the feature (Admin → Feature Access) plus anyone whose calls
actually produced rows, so a feature switched off today cannot make one
person's old calls look like a department's.

**Partial history.** Several sources start recording only when they are turned
on (Loose Ends, for one). A note names the day each one starts, computed from
the data rather than hard-coded, so a short history is never read as a quiet
period.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from rubik_core.insights.context import InsightsContext, fmt_day

#: The Feature Access key that decides who can record calls.
CALL_FEATURE = "sales_call_capture"

#: Shown when some department's call-derived figures were left out.
CALL_HIDDEN_NOTE = (
    "Figures that come from recorded calls are shown per department only, and "
    "are left out for any department where fewer than 3 people use call "
    "capture: they would point to one person's calls."
)


def visible_departments(
    ctx: InsightsContext,
    contributors: Iterable[str],
    *,
    feature: str = CALL_FEATURE,
) -> frozenset[str]:
    """Departments whose department-only figures may be shown, to anyone.

    ``contributors`` are the (folded) emails behind the rows: the AE on each
    call, the owner of each commitment.
    """
    eligible = ctx.population.eligible(feature)
    found: dict[str, set[str]] = defaultdict(set)
    for email in contributors:
        dept = ctx.population.dept_of(email)
        if dept:
            found[dept].add(email)
    members = ctx.dept_people()
    out = set()
    for dept in set(found) | set(members):
        group = (set(members.get(dept, ())) & eligible) | found.get(dept, set())
        if not ctx.hide_small(len(group), always=True):
            out.add(dept)
    return frozenset(out)


def history_note(ctx: InsightsContext, what: str, first: date | None) -> str | None:
    """A note when ``what`` started recording after the comparison period
    began (or has recorded nothing yet), else None."""
    if first is None:
        return f"{what}: nothing has been recorded yet."
    if first > ctx.period.previous.start:
        return (
            f"{what} is recorded from {fmt_day(first)}. Days before that read as "
            "empty, not as a quiet period."
        )
    return None


def plain_label(value: str | None, labels: dict[str, str] | None = None) -> str:
    """A vocabulary value as words: the label map first, else 'Snake case'."""
    if not value:
        return "Not recorded"
    if labels and value in labels:
        return labels[value]
    return value.replace("_", " ").strip().capitalize()
