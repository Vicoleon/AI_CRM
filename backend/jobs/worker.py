"""Default entrypoint of the ``cubie-worker`` Cloud Run Job.

The worker job never does real work through this module: every launch from
the service (``backend/jobs/launcher.py``) overrides the container args with
the actual task, e.g. ``-m rubik_core.scripts.sync_meet_transcripts --days 7``
or ``-m backend.jobs.dam_ingest_job --doc-id …``.

This default exists so a bare ``gcloud run jobs execute cubie-worker`` (no
overrides) exits cleanly with a hint instead of crashing or, worse, silently
running something.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "cubie-worker was executed without container-arg overrides — nothing "
        "to do. Real work is dispatched by the cubie service via "
        "backend/jobs/launcher.py with args like: "
        "-m rubik_core.scripts.sync_meet_transcripts --days 7"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
