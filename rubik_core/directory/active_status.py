"""Deactivated-user detection — the port, the combiner, and the Google source.

A :class:`DeactivationSource` answers one question over a candidate set of
emails: *which of these belong to a deactivated employee?* Multiple sources are
unioned by :func:`resolve_deactivated_emails`, which is **fail-open** — a source
that raises is logged and treated as contributing nothing, so a detection
outage can never *block* a priorities run (the worst case is that an
already-departed employee gets one more empty bundle, exactly today's
behaviour). All emails are normalised to lower-case and the result is always a
subset of the candidates.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class DeactivationSource(Protocol):
    """A provider that flags deactivated employees within a candidate set.

    Implementations MUST be scoped to the passed ``candidates`` (never return
    an email that wasn't asked about) and SHOULD be cheap to call once per batch
    (cache any full-directory sweep on the instance). Raising is acceptable —
    the combiner treats a failed source as "found nothing".
    """

    def deactivated_emails(self, candidates: Iterable[str]) -> set[str]:
        """Return the lower-cased subset of ``candidates`` that are deactivated."""
        ...


def _normalize(emails: Iterable[str]) -> set[str]:
    return {e.strip().lower() for e in emails if e and e.strip()}


def resolve_deactivated_emails(
    candidates: Iterable[str],
    sources: Iterable[DeactivationSource],
) -> set[str]:
    """Union every source's deactivated set, restricted to ``candidates``.

    Fail-open: a source that raises is logged (never re-raised) and contributes
    nothing. Returns lower-cased emails, always a subset of ``candidates``.
    """
    cand = _normalize(candidates)
    if not cand:
        return set()
    found: set[str] = set()
    for src in sources:
        name = type(src).__name__
        try:
            hits = src.deactivated_emails(cand)
        except Exception:  # noqa: BLE001 — a detection failure must never block a run
            logger.warning(
                "[offboard] deactivation source %s failed — treating as no-op",
                name,
                exc_info=True,
            )
            continue
        hits_norm = _normalize(hits or set()) & cand
        if hits_norm:
            logger.info(
                "[offboard] source %s flagged %d deactivated: %s",
                name,
                len(hits_norm),
                sorted(hits_norm),
            )
        found |= hits_norm
    return found


class GoogleDirectorySuspendedSource:
    """Deactivation via Google Workspace Admin Directory ``suspended``/``archived``.

    The **more authoritative** complement to the Slack signal: a departed
    employee's Workspace account is typically *suspended* (or *archived*) before
    it is fully deleted, and that is the same account whose DWD impersonation
    powers every gmail/calendar/drive/meet feed — so ``suspended:true`` is the
    ground truth for "all Google signals will be empty for this user".

    Requires the ``admin.directory.user.readonly`` DWD scope
    (:class:`rubik_core.tools.directory.GoogleDirectoryClient`). That grant may
    not be in place, so this source is written to **self-disable on the first
    auth/scope failure** (it returns an empty set rather than hammering N failing
    ``users.get`` calls) and to add an email ONLY on a positive ``suspended`` /
    ``archived`` signal — never on a 404 or transient error. The net effect: safe
    to include unconditionally; it either adds authority or quietly no-ops.
    """

    def deactivated_emails(self, candidates: Iterable[str]) -> set[str]:
        cand = _normalize(candidates)
        if not cand:
            return set()
        try:
            from rubik_core.tools.directory import GoogleDirectoryClient

            client = GoogleDirectoryClient()
        except Exception:  # noqa: BLE001 — no key / no scope / build failure
            logger.info(
                "[offboard] Google Directory client unavailable — skipping the "
                "suspended-account check (Slack remains the primary signal)",
                exc_info=True,
            )
            return set()
        try:
            return client.suspended_emails(cand)
        except Exception:  # noqa: BLE001 — self-disable on any directory failure
            logger.info(
                "[offboard] Google Directory suspended-check unavailable "
                "(scope not granted?) — skipping",
                exc_info=True,
            )
            return set()
