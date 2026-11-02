"""Close (or just list) still-open EOS L10 sessions that are actually over.

An L10 session stays "live" until someone hits Conclude. Sessions left open —
from demoing/testing, or a facilitator who closed the browser tab without
concluding — keep their meeting stuck on a green "Rejoin" forever and make the
next occurrence silently resume a weeks-old row.

By default this closes only the sessions the app itself considers ABANDONED,
using the one shared rule in ``rubik_core/eos/session_lifecycle.py`` (past the
meeting's box AND hours with no write). That is the same predicate the nightly
sweep and the meetings board use, so this script can never disagree with them,
and it will not close a meeting that is genuinely still going. Each closed row
gets ``actual_end`` = its last real activity (not "now" — a meeting that
stopped at 10:30 should not report a 15-hour duration) and ``auto_closed =
true``, which is what keeps it distinguishable from a human Conclude.

``--all`` restores the old blunt behaviour: close EVERY open session, including
one in progress right now. That is for clearing demo residue, not for routine
cleanup.

READ-ONLY by default (dry run). It connects to whatever the environment's
``DATABASE_URL`` / ``CLOUDSQL_CONNECTION_NAME`` points at — on this project
that is the live dev database, so treat every ``--apply`` with prod caution.

Usage (runnable from any directory):
    # list what WOULD be closed — no writes
    cd <repo> && ./venv/bin/python \
        scripts/close_stale_l10_sessions.py

    # actually close the abandoned ones
    cd <repo> && ./venv/bin/python \
        scripts/close_stale_l10_sessions.py --apply

    # restrict to one team (case-insensitive department name)
    cd <repo> && ./venv/bin/python \
        scripts/close_stale_l10_sessions.py --department Operations --apply

    # demo cleanup: close every open session, live ones included
    cd <repo> && ./venv/bin/python \
        scripts/close_stale_l10_sessions.py --all --apply

``scripts/check_eos_sessions.py`` is the read-only diagnostic that shows the
same open set bucketed LIVE / IDLE / STALE.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

# Allow both `python scripts/close_stale_l10_sessions.py` and `python -m …`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rubik_core.config import settings  # noqa: E402

# Importing config promotes GOOGLE_WORKSPACE_SERVICE_ACCOUNT_KEY into
# GOOGLE_APPLICATION_CREDENTIALS so the Workspace DWD tools can impersonate
# users. That service account has no Cloud SQL permission, so leaving it set
# makes the connector 403 on cloudsql.instances.get. Drop it before the lazy
# engine is built so the DB connection falls back to ADC — same reason
# scripts/check_eos_sessions.py does it.
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

from rubik_core.database.db import SessionLocal  # noqa: E402
from rubik_core.database.eos import L10Session  # noqa: E402
from rubik_core.eos.session_lifecycle import (  # noqa: E402
    AUTO_CLOSE_ACTOR,
    BOX_GRACE,
    INACTIVITY_WINDOW,
    close_abandoned_sessions,
    open_sessions,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually close the sessions. Omit for a read-only dry run.",
    )
    p.add_argument(
        "--department",
        default=None,
        help="Only sessions whose meeting is in this department (case-insensitive).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=(
            "Close EVERY open session, not just abandoned ones — including a "
            "meeting happening right now. Demo cleanup only."
        ),
    )
    return p.parse_args()


def main() -> int:
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    args = _parse_args()
    now = datetime.utcnow()
    print(
        f"DB : {settings.CLOUDSQL_CONNECTION_NAME or settings.DATABASE_URL} "
        f"(db={settings.DB_NAME or '-'})"
    )
    print(f"Now: {now:%Y-%m-%d %H:%M} UTC")
    print(
        f"Rule: abandoned = past box + {int(BOX_GRACE.total_seconds() // 3600)}h "
        f"AND no write for {int(INACTIVITY_WINDOW.total_seconds() // 3600)}h"
        + ("   [OVERRIDDEN by --all]" if args.all else "")
    )
    print()

    db = SessionLocal()
    try:
        scanned = open_sessions(db)
        if args.department:
            wanted = args.department.strip().lower()
            scanned = [
                s for s in scanned if (s.department_name or "").lower() == wanted
            ]
        if not scanned:
            print("No open L10 sessions matched — nothing to close.")
            return 0

        targets = scanned if args.all else [s for s in scanned if s.is_abandoned(now)]

        print(f"{len(scanned)} open session(s):\n")
        for row in scanned:
            print("  " + row.describe(now))
        print()

        if not targets:
            print("Nothing is abandoned — every open session is still within the")
            print("rule's window. Use --all to close them anyway (demo cleanup).")
            return 0

        verb = "CLOSING" if args.apply else "WOULD CLOSE"
        print(f"{verb} {len(targets)} session(s).")

        if not args.apply:
            print("\nDry run — no changes written. Re-run with --apply to close them.")
            return 0

        if args.all:
            # Blunt path: the shared rule does not apply, so stamp each row
            # here. ``last_activity`` is still the honest end for a session
            # that stopped hours ago; for one that is live right now it is
            # effectively "now" anyway.
            for row in targets:
                session = (
                    db.query(L10Session)
                    .filter(
                        L10Session.id == row.session_id,
                        L10Session.actual_end.is_(None),
                    )
                    .first()
                )
                if session is None:
                    continue
                session.actual_end = row.last_activity
                session.auto_closed = True
                session.updated_by = AUTO_CLOSE_ACTOR
                session.state_version = (session.state_version or 0) + 1
            db.commit()
            closed = len(targets)
        else:
            # Scope to the meetings that survived --department, or a filtered
            # run would quietly sweep the whole company.
            report = close_abandoned_sessions(
                db, now=now, meeting_ids=[s.meeting_id for s in targets]
            )
            closed = len(report.closed)

        print(f"\nClosed {closed} session(s). Meetings now show Start again.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
