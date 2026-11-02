"""Drive-labels signal source for the UC-17 multi-source priority pipeline.

Emits a Signal for each **recently-updated** Google Drive file that carries an
**importance label** (Important / Cubie / Project / …, per the admin-managed
``drive_label_priorities`` catalog). The "recent + labeled" combination is the
whole point: recency is enforced by the pipeline's ``since`` window (default 24h
lookback), and the catalog enforces "labeled-and-important" — so stale labeled
files never reach the judge.

The signal's importance comes from the catalog ``priority`` (1.0–2.0), mapped to
a ``severity_hint``. A file with no mapped label, or whose best label is the
neutral 1.0 weight, emits nothing.

For the most recently modified labeled files (latest-N by modified time), the
signal also reads a bounded excerpt of the file's **content**
(``payload.content_excerpt``) — Docs,
Sheets, Slides, PDFs, .docx/.xlsx and plain text — so the judge grounds on what
the document actually says, not just its name. Content is read through the same
user-delegated Drive client (inheriting the user's live ACLs) via the already-
granted ``…/auth/drive`` scope; reads are capped and best-effort (see
``CUBIE_DRIVE_LABEL_CONTENT_*`` in config), degrading to name + label on any
failure or unsupported type.

Grant-gated: the underlying Drive client reads labels through an isolated,
labels-scoped credentials object. Until a Workspace admin grants the
``drive.labels.readonly`` DWD scope the client returns no labeled files, so this
source is a clean no-op.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.signals.types import Signal
from rubik_core.config import settings

logger = logging.getLogger(__name__)


def _priority_to_severity(priority: float) -> str:
    """Map a catalog priority weight (1.0–2.0) to a Signal severity_hint."""
    if priority >= 1.4:
        return "high"
    if priority >= 1.1:
        return "medium"
    return "low"


def _parse_dt(value: Any) -> datetime:
    """Parse an RFC-3339 modifiedTime; fall back to now() on anything odd."""
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class DriveSignalSource:
    """Implements `SignalSource`. Surfaces recent, importance-labeled Drive files.

    `label_map` and `drive_client` are injectable for testing; in production
    they default to the admin catalog and a per-user `GoogleDriveClient`.
    """

    name = "drive"

    def __init__(
        self,
        drive_client: Any | None = None,
        *,
        label_map: dict[str, float] | None = None,
        now: datetime | None = None,
    ):
        self._client = drive_client
        self._label_map = label_map
        self._now = now

    def _client_for(self, user_email: str) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            from rubik_core.tools.drive import GoogleDriveClient

            return GoogleDriveClient(user_email=user_email)
        except Exception as e:
            logger.warning("[signals.drive] GoogleDriveClient init failed: %s", e)
            return None

    def _labels(self) -> dict[str, float]:
        if self._label_map is not None:
            return self._label_map
        try:
            from rubik_core.database.repository import SQLMetadataRegistry

            return SQLMetadataRegistry().drive_label_priority_map()
        except Exception as e:
            logger.warning("[signals.drive] could not load label catalog: %s", e)
            return {}

    def fetch(self, user_email: str, *, since: datetime) -> list[Signal]:
        if not settings.CUBIE_DRIVE_LABELS_SIGNAL_ENABLED:
            return []

        label_map = self._labels()
        if not label_map:
            # No importance labels configured → nothing to surface.
            return []

        client = self._client_for(user_email)
        if client is None:
            return []

        try:
            files = client.list_recent_labeled_files(since=since)
        except Exception as e:
            logger.warning("[signals.drive] list_recent_labeled_files failed: %s", e)
            return []

        # Pass 1 — keep only files whose best resolved label is an importance
        # boost (> 1.0), carrying the matched values + best weight forward.
        # Case-insensitive match against the admin catalog; 1.0 = "no boost".
        qualifying: list[tuple[dict[str, Any], list[str], float]] = []
        for f in files or []:
            matched: list[str] = []
            best = 0.0
            for value in f.get("label_values") or []:
                weight = label_map.get((value or "").strip().lower())
                if weight is None:
                    continue
                matched.append(value)
                best = max(best, weight)
            if not matched or best <= 1.0:
                continue
            qualifying.append((f, matched, best))

        # Pass 2 — read a bounded CONTENT excerpt for the most recently modified
        # labeled files so the judge grounds on what's *inside* the doc, not just
        # its name. Only the latest-N by modified time are read (token-cost
        # guard); a failure or an unsupported type degrades cleanly to name +
        # label only.
        excerpts = self._read_excerpts(client, qualifying)

        signals: list[Signal] = []
        for f, matched, best in qualifying:
            name = f.get("name") or "Untitled document"
            label_str = ", ".join(matched)
            file_id = f.get("id")
            excerpt = excerpts.get(file_id or "")

            summary = f"Recently updated document '{name}' labeled {label_str}."
            if excerpt:
                # Keep summary compact (the judge caps it at 200 chars); the full
                # bounded excerpt rides in payload.content_excerpt.
                gist = " ".join(excerpt.split())[:180]
                summary = (
                    f"Recently updated document '{name}' labeled {label_str}. "
                    f"Content: {gist}"
                )

            payload: dict[str, Any] = {
                "file_id": file_id,
                "file_name": name,
                "label_values": matched,
                "web_view_link": f.get("web_view_link"),
                "modified_time": f.get("modified_time"),
                "priority": best,
                "owner": f.get("owner"),
            }
            if excerpt:
                payload["content_excerpt"] = excerpt

            signals.append(
                Signal(
                    user_email=user_email.lower(),
                    source="drive",
                    signal_type="labeled_document_updated",
                    summary=summary,
                    occurred_at=_parse_dt(f.get("modified_time")),
                    severity_hint=_priority_to_severity(best),
                    # No CRM entity by default → the correlator makes this a solo
                    # orphan group the judge still sees (see correlator Tier 6).
                    entity_refs={},
                    payload=payload,
                )
            )
        return signals

    def _read_excerpts(
        self,
        client: Any,
        qualifying: list[tuple[dict[str, Any], list[str], float]],
    ) -> dict[str, str]:
        """Read bounded content excerpts for the most recently modified labeled files.

        Returns ``{file_id: excerpt}``. Empty when content-reading is disabled,
        the client can't read content, or nothing extracts — callers fall back
        to name + label only. Content is read through the same user-delegated
        client (so it inherits the user's live Drive ACLs); each read is capped
        at ``CUBIE_DRIVE_LABEL_CONTENT_MAX_CHARS`` and failures are swallowed.
        """
        if not settings.CUBIE_DRIVE_LABEL_CONTENT_ENABLED:
            return {}
        reader = getattr(client, "export_document_text", None)
        if not callable(reader):
            return {}

        # Latest-modified first, so the excerpts reflect what the user is
        # actively working on (not merely the highest-importance labels).
        ranked = sorted(
            qualifying,
            key=lambda t: _parse_dt(t[0].get("modified_time")),
            reverse=True,
        )
        limit = settings.CUBIE_DRIVE_LABEL_CONTENT_MAX_FILES
        excerpts: dict[str, str] = {}
        for f, _matched, _best in ranked[:limit]:
            file_id = f.get("id")
            if not file_id:
                continue
            try:
                text = reader(
                    file_id,
                    f.get("mime_type"),
                    max_chars=settings.CUBIE_DRIVE_LABEL_CONTENT_MAX_CHARS,
                )
            except Exception as e:
                logger.warning(
                    "[signals.drive] content read failed for %s: %s", file_id, e
                )
                continue
            if text and text.strip():
                excerpts[file_id] = text.strip()
        return excerpts
