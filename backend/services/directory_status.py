"""Backend composition of the deactivated-user detection sources.

The domain layer (:mod:`rubik_core.directory`) owns the port + the pure combiner
+ the Google Directory ``suspended`` source. The **primary** signal — Slack
``users.list`` ``deleted:true`` — lives here, because it needs the backend
:class:`~backend.services.slack_service.SlackBotClient`, which the domain layer
must not import.

Callers use :func:`deactivated_priorities_emails` and never touch the sources
directly:

* generation roster — ``rubik_core.priorities.batch_runner`` injects this as the
  ``deactivated_lookup`` for ``list_active_users`` (via a lazy import, so the
  Cloud Run Job container stays free of any FastAPI import);
* Slack delivery — ``backend.agents.slack_delivery`` calls it with its already
  constructed ``SlackBotClient`` so the ``users.list`` sweep is paid once.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from rubik_core.config import settings
from rubik_core.directory.active_status import (
    GoogleDirectorySuspendedSource,
    resolve_deactivated_emails,
)

logger = logging.getLogger(__name__)


class SlackDeactivationSource:
    """Primary source: a deactivated member is ``deleted:true`` in ``users.list``.

    Reuses the delivery path's Slack bot client when one is supplied (so the
    workspace ``users.list`` sweep is fetched once and cached), else constructs
    its own. A no-op (empty set) when no workspace bot token is installed.
    """

    def __init__(self, bot=None):
        self._bot = bot

    def _client(self):
        if self._bot is not None:
            return self._bot
        # Lazy import keeps this module importable without eagerly touching the
        # Slack client (and its DB session) at import time.
        from backend.services.slack_service import SlackBotClient

        self._bot = SlackBotClient()
        return self._bot

    def deactivated_emails(self, candidates: Iterable[str]) -> set[str]:
        bot = self._client()
        if not getattr(bot, "is_available", False):
            return set()
        deleted = bot.deleted_member_emails()
        return {
            e
            for e in ((c or "").strip().lower() for c in candidates)
            if e and e in deleted
        }


def deactivated_priorities_emails(candidates: Iterable[str], *, bot=None) -> set[str]:
    """Emails in ``candidates`` that belong to a deactivated / offboarded employee.

    Union of the Slack ``deleted`` signal (primary) and the Google Directory
    ``suspended``/``archived`` signal (authoritative complement). Fully
    fail-open (a source outage contributes nothing) and gated by the master
    ``CUBIE_OFFBOARD_DETECTION_ENABLED`` kill switch. Pass an existing
    ``SlackBotClient`` as ``bot`` to reuse its cached ``users.list`` sweep.
    """
    if not getattr(settings, "CUBIE_OFFBOARD_DETECTION_ENABLED", True):
        return set()
    sources = [SlackDeactivationSource(bot), GoogleDirectorySuspendedSource()]
    return resolve_deactivated_emails(candidates, sources)
