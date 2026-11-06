"""Directory / active-status awareness for Cubie.

Detects **deactivated / offboarded** employees so the AI-Priorities pipeline
stops generating empty daily bundles (and tripping the false "no bundle for
their local today" ops alert) for people who have left the company. This is
deliberately *distinct from PTO* (:mod:`rubik_core.availability`): a user on PTO
still gets a friendly "holding your priorities" note, whereas a deactivated user
is simply dropped from the roster.

Layering note: the **primary** detection signal (Slack ``users.list``
``deleted:true``) lives in the backend Slack client, which the domain layer must
not import. So this package defines a small port —
:class:`~rubik_core.directory.active_status.DeactivationSource` — plus a pure
combiner (:func:`~rubik_core.directory.active_status.resolve_deactivated_emails`)
and the one source that *can* live in the domain layer (the Google Directory
``suspended`` check, backed by :mod:`rubik_core.tools.directory`). The Slack
source is implemented in ``backend.services.directory_status`` and injected by
the caller (``batch_runner`` for generation, ``slack_delivery`` for delivery).
"""

from rubik_core.directory.active_status import (
    DeactivationSource,
    GoogleDirectorySuspendedSource,
    resolve_deactivated_emails,
)

__all__ = [
    "DeactivationSource",
    "GoogleDirectorySuspendedSource",
    "resolve_deactivated_emails",
]
