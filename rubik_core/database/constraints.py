"""Portable CHECK-constraint bodies, shared by every table module.

These two helpers are needed by ``call_capture``, ``call_scripts``,
``commitments`` and ``smart_teams``. They live here — in a module that imports
NOTHING from ``rubik_core.database`` — because their previous home created an
import cycle that silently broke background work.

**The cycle, and why it only killed the jobs.** ``_in_check``/``_lower_check``
used to be defined in ``rubik_core/database/smart_teams.py``, and the sibling
table modules imported them from there. But ``smart_teams`` imports ``Base``
from ``models``, and the bottom of ``models`` imports the whole sibling set
(``call_capture`` … ``smart_teams``) to register their tables on
``Base.metadata``. So the outcome depended entirely on which module a process
touched first:

* ``models`` first (the web service, and pytest under its conftest) — ``Base``
  is already bound when ``smart_teams`` re-enters ``models``, ``smart_teams``
  runs to completion, and ``call_capture`` finds ``_in_check``. Fine.
* ``rubik_core.database.smart_teams`` first (every Smart Teams Cloud Run Job,
  which reaches it through ``rubik_core.smart_teams.*``) — ``smart_teams``
  stops at its ``Base`` import, ``models`` runs its registration block,
  ``call_capture`` asks the half-built ``smart_teams`` for ``_in_check``, and
  the process dies with ``ImportError: cannot import name '_in_check' from
  partially initialized module``.

That asymmetry is why it stays invisible: the API and the whole test suite
import ``models`` first and never see it. It can cost people their Work Style
Interview result and fail the nightly skill sweep every night — each time as
a job that returned 202 to the caller and then exited 1 where nobody was
looking.

Anything imported by ``models``'s registration block must not import from a
sibling that is also in it. Keeping these helpers in a leaf module is what
makes that true no matter which module an entry point reaches for first;
``backend/tests/test_database_import_order.py`` holds the line.
"""

from __future__ import annotations


def in_check(column: str, allowed: tuple[str, ...], *, nullable: bool = False) -> str:
    """Render a portable ``column IN (...)`` CHECK body.

    Spelled out as literals rather than a bound parameter because a CHECK is
    compiled into the table definition once, and SQLite will not accept a bound
    parameter there. ``nullable`` adds the NULL escape hatch for the columns
    that legitimately have no value yet.
    """
    values = ", ".join(f"'{value}'" for value in allowed)
    body = f"{column} IN ({values})"
    return f"{column} IS NULL OR {body}" if nullable else body


def lower_check(column: str, *, nullable: bool = False) -> str:
    """CHECK body asserting an identity column is already folded.

    The ``@validates`` hooks fold on every ORM write, but the ORM is not the
    only writer: ``bulk_insert_mappings``, a Core ``insert()``, an import job's
    fast path, a seed script and a psql session all bypass instance attribute
    assignment entirely. That matters wherever identity is keyed on an email —
    an un-folded write does not merely look wrong, it makes the same human two
    different people, which is how ``st_profiles`` would lose "one version
    number per person" and how an imported contact would arrive twice.
    ``lower()`` is available in both SQLite and Postgres.

    ``nullable`` is for the columns that are legitimately empty until someone
    acts (``applied_by``, ``requested_by``); without it the CHECK would reject
    every un-acted row.
    """
    body = f"{column} = lower({column})"
    return f"{column} IS NULL OR {body}" if nullable else body
