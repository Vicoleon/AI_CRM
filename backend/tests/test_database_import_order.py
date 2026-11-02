"""Every table module must import cleanly when it is the FIRST import.

This file exists because of a weeks-long outage that no other test could see.

``rubik_core/database/models.py`` ends by importing its sibling table modules
(``call_capture``, ``call_scripts``, ``commitments``, ``eos``,
``smart_teams``) so their tables register on ``Base.metadata``. Each of those
siblings, in turn, imports ``Base`` from ``models``. That is a cycle by
construction, and Python resolves it or not purely on which module the process
reaches first:

* touch ``models`` first and every sibling is imported from a ``models`` that
  has already bound ``Base`` — the cycle closes cleanly;
* touch a *sibling* first and it stops at its own ``Base`` import while
  ``models`` runs the registration block, so the next sibling meets a
  half-executed module and any module-level name it wanted is simply not there
  yet.

Once ``call_capture`` began importing two CHECK helpers from
``rubik_core.database.smart_teams``, which closed the second loop. Nothing went
red. The API imports ``models`` first (via ``backend/main.py``), and so does
pytest under its conftest, so both took the benign path — while every Cloud Run
Job that reaches a table module through business logic took the other one and
died on startup with ``ImportError: cannot import name '_in_check' from
partially initialized module``. Work Style Interview results were lost and the
nightly skill sweep failed every night until the fix. Each failure was a job:
the endpoint had already returned 202, so the person was shown a calm "scoring
in progress" screen for work that had exited 1 minutes earlier.

The helpers now live in ``rubik_core.database.constraints``, a leaf that imports
nothing from the package. These tests are what keep it that way: each module is
imported in its OWN interpreter, because by the time the suite runs, ``models``
is long since in ``sys.modules`` and the import order under test is impossible
to reproduce in-process. A new table module is covered automatically — the list
is discovered from the tree, not maintained by hand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The Smart Teams business modules the failing jobs actually reach. They are
# listed explicitly rather than discovered because these are the paths that cost
# somebody a result: `smart_teams_score_job` imports `scoring` inside `main()`,
# so importing the JOB module proves nothing — the cycle only fires once the
# entry point asks for the logic. Each of these was unimportable in a fresh
# interpreter for three weeks.
JOB_ENTRY_MODULES = (
    "rubik_core.smart_teams.scoring",
    "rubik_core.smart_teams.skill_inference",
    "rubik_core.smart_teams.guide",
    "rubik_core.smart_teams.uploads",
    "rubik_core.smart_teams.comparison",
    # The nightly stale-work sweep. Reaches three features' tables and is
    # imported inside `main()` like the rest, so it takes the same risk — and
    # it is the one module whose silent death would take the alerting for all
    # three down with it.
    "rubik_core.reliability.stale_work",
)


def _database_modules() -> list[str]:
    """Every importable module under ``rubik_core/database``, dotted."""
    root = REPO_ROOT / "rubik_core" / "database"
    names = []
    for path in sorted(root.rglob("*.py")):
        target = path.parent if path.name == "__init__.py" else path.with_suffix("")
        names.append(str(target.relative_to(REPO_ROOT)).replace("/", "."))
    return names


def _import_alone(module: str) -> subprocess.CompletedProcess:
    """Import ``module`` as the first thing a fresh interpreter does."""
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        cwd=REPO_ROOT,
        timeout=120,
    )


@pytest.mark.parametrize("module", _database_modules())
def test_database_module_imports_first(module: str) -> None:
    """A table module must not depend on ``models`` having been imported first.

    Failing here means a module-level import between two modules in ``models``'s
    registration block has re-formed the cycle. The fix is to move whatever is
    being shared into ``rubik_core.database.constraints`` (or another leaf), not
    to reorder imports at the call site — reordering only moves which entry
    point breaks, and the one that breaks is a background job nobody is watching.
    """
    proc = _import_alone(module)
    assert proc.returncode == 0, (
        f"`import {module}` fails in a fresh interpreter, so any entry point "
        f"that reaches it before rubik_core.database.models dies on startup — "
        f"which for a Cloud Run Job means silently, after the endpoint already "
        f"returned 202.\n{proc.stderr.decode()[-1500:]}"
    )


@pytest.mark.parametrize("module", JOB_ENTRY_MODULES)
def test_smart_teams_job_entry_imports_first(module: str) -> None:
    """The logic a Smart Teams job imports inside ``main()`` must import alone."""
    proc = _import_alone(module)
    assert proc.returncode == 0, (
        f"`import {module}` fails in a fresh interpreter. This is the exact "
        f"shape of a Smart Teams scoring outage: the job launches, "
        f"exits 1 on import, and the person waits in front of a screen that "
        f"says their answers are being scored.\n{proc.stderr.decode()[-1500:]}"
    )


def test_constraints_module_stays_a_leaf() -> None:
    """``constraints`` must import nothing from ``rubik_core.database``.

    This is the property that makes the cycle unable to regrow: the shared
    helpers are reachable from any module in the registration block without
    dragging a sibling in. A future edit that reaches for ``Base`` or a table
    here would quietly restore the outage, so it is asserted rather than
    trusted to a comment.
    """
    source = (REPO_ROOT / "rubik_core" / "database" / "constraints.py").read_text()
    offenders = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
        and "rubik_core" in line
        and "__future__" not in line
    ]
    assert not offenders, (
        "rubik_core.database.constraints must stay a leaf module — it is "
        "imported BY the table modules, so importing one back would rebuild the "
        f"circular-import outage it exists to prevent. Found: {offenders}"
    )
