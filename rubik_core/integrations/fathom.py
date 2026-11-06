import base64
import binascii
import hashlib
import hmac
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

import requests

from rubik_core.tenancy.secrets import workspace_secret

logger = logging.getLogger(__name__)

#: How far a webhook's timestamp may sit from now, either way, before it is
#: refused as a replay (Fathom's own guidance: five minutes).
WEBHOOK_TOLERANCE_SECONDS = 5 * 60

#: How long an accepted workspace/event pair is remembered, so a repeat is not
#: imported twice: every moment its timestamp still passes the check above
#: (the tolerance either side of when it was sent). After that, the timestamp
#: check refuses the repeat on its own.
WEBHOOK_REPLAY_WINDOW_SECONDS = 2 * WEBHOOK_TOLERANCE_SECONDS

#: The most event ids remembered at once; past it, the oldest is forgotten.
WEBHOOK_REPLAY_MAX_IDS = 2048

#: The three headers that carry a webhook's signature. Fathom sends the
#: Standard Webhooks names; the ``svix-`` names are the same values under the
#: older spelling of the same scheme, accepted so neither breaks the import.
_SIGNATURE_HEADERS = (
    ("webhook-id", "webhook-timestamp", "webhook-signature"),
    ("svix-id", "svix-timestamp", "svix-signature"),
)


def _signature_headers(headers: Mapping[str, str]) -> tuple[str, str, str] | None:
    """The id, timestamp and signature list, from whichever spelling is complete."""
    for id_name, stamp_name, signature_name in _SIGNATURE_HEADERS:
        msg_id = (headers.get(id_name) or "").strip()
        stamp = (headers.get(stamp_name) or "").strip()
        signatures = (headers.get(signature_name) or "").strip()
        if msg_id and stamp and signatures:
            return msg_id, stamp, signatures
    return None


def webhook_event_id(headers: Mapping[str, str]) -> str | None:
    """The event's ``webhook-id`` (or ``svix-id``): one id per event, and the
    same id on every retry of it."""
    found = _signature_headers(headers)
    return found[0] if found else None


class WebhookReplayGuard:
    """The workspace-scoped webhook event ids accepted inside the replay window.

    A small, bounded, in-process cache: an id is forgotten once the window has
    passed (the timestamp check refuses a repeat after that) or, oldest first,
    when more than ``max_ids`` arrive inside it. Each instance keeps its own,
    so it stops a repeat that reaches the same instance; the import a repeat
    would start is itself keyed by meeting, so a repeat on another instance
    costs a second look, not a second copy.
    """

    def __init__(
        self,
        window_seconds: float = WEBHOOK_REPLAY_WINDOW_SECONDS,
        max_ids: int = WEBHOOK_REPLAY_MAX_IDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_seconds = window_seconds
        self._max_ids = max_ids
        self._clock = clock
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def first_delivery(self, event_id: str) -> bool:
        """True, and the id is remembered, unless it was accepted inside the window."""
        now = self._clock()
        with self._lock:
            while self._seen:
                oldest, accepted_at = next(iter(self._seen.items()))
                if now - accepted_at <= self.window_seconds:
                    break
                del self._seen[oldest]
            if event_id in self._seen:
                return False
            self._seen[event_id] = now
            while len(self._seen) > self._max_ids:
                self._seen.popitem(last=False)
            return True

    def forget(self, event_id: str) -> None:
        """Drop one id, so a retry of a delivery that failed is handled again."""
        with self._lock:
            self._seen.pop(event_id, None)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


def verify_webhook_signature(body: bytes, headers: Mapping[str, str]) -> bool:
    """Is ``body`` an event Fathom signed with this workspace's webhook secret?

    Fathom signs the Standard Webhooks way: an HMAC-SHA256 of
    ``"{id}.{timestamp}.{raw body}"``, keyed with the base64-decoded part of the
    ``whsec_…`` workspace secret, sent base64-encoded in a
    space-separated list of ``v1,<signature>`` entries. One match is enough.

    Fails CLOSED: no secret configured, a missing header, a timestamp more than
    :data:`WEBHOOK_TOLERANCE_SECONDS` from now (a replay), a secret that is not
    base64, or no matching entry, and the answer is False.
    """
    found = _signature_headers(headers)
    if found is None:
        logger.warning("Fathom webhook refused: its signature headers are missing.")
        return False
    msg_id, stamp, signatures = found

    try:
        sent_at = int(stamp)
    except ValueError:
        logger.warning("Fathom webhook refused: its timestamp is not a number.")
        return False
    if abs(time.time() - sent_at) > WEBHOOK_TOLERANCE_SECONDS:
        logger.warning("Fathom webhook refused: its timestamp is outside the window.")
        return False

    configured = (workspace_secret("fathom", "webhook_secret") or "").strip()
    if not configured:
        logger.error(
            "Fathom webhook secret is not configured for this workspace: webhook refused."
        )
        return False

    try:
        key = base64.b64decode(configured.removeprefix("whsec_"), validate=True)
    except (binascii.Error, ValueError):
        logger.error("Fathom webhook secret is not a whsec_ base64 secret.")
        return False

    signed = f"{msg_id}.{stamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
    for entry in signatures.split():
        version, _, value = entry.partition(",")
        if version == "v1" and hmac.compare_digest(value.encode(), expected):
            return True

    logger.warning("Fathom webhook refused: no signature matches.")
    return False


class FathomClient:
    """Client for interacting with the Fathom Video API."""

    BASE_URL = "https://api.fathom.ai/external/v1"

    @classmethod
    def from_settings(cls):
        """Compatibility factory; the API key is resolved from workspace secrets."""
        return cls()

    def __init__(self, api_key: str | None = None):
        raw_key = (
            api_key if api_key is not None else workspace_secret("fathom", "api_key")
        )
        self.api_key = raw_key.strip() if raw_key else None
        if not self.api_key:
            logger.warning("Fathom API key is not configured for this workspace.")

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
        }

    def list_meetings(
        self,
        limit: int = 20,
        include_transcript: bool = True,
        *,
        raise_on_error: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Lists meetings from Fathom containing details like name, duration, participants.
        If include_transcript is True, the transcript is embedded in the response.
        Scheduled ingestion opts into raising fetch errors so a failed API call
        cannot look like a successful empty poll.
        """
        if not self.api_key:
            logger.error("Cannot list Fathom meetings without an API Key.")
            return []

        url = f"{self.BASE_URL}/meetings"
        params = {"limit": limit, "include_transcript": str(include_transcript).lower()}

        try:
            response = requests.get(
                url, headers=self.headers, params=params, timeout=15
            )
            response.raise_for_status()
            data = response.json()
            return data.get("items", [])

        except Exception as exc:
            # requests can copy a rejected X-Api-Key value into InvalidHeader.
            logger.error("Failed to fetch Fathom meetings (%s)", type(exc).__name__)
            if raise_on_error:
                raise
            return []

    def find_meeting(
        self, recording_id: str | int, *, limit: int = 50
    ) -> dict[str, Any] | None:
        """One meeting by Fathom id, transcript included, or None.

        The external API's listing is the only read this client has needed, so
        this scans the most recent ``limit`` meetings rather than adding a
        per-id endpoint whose shape has not been verified against the live
        account. Fifty covers weeks of a sales team's recordings; a recording
        older than that is also older than ``CCS_FATHOM_MAX_AGE_DAYS`` admits.
        """
        wanted = str(recording_id)
        for meeting in self.list_meetings(limit=limit, include_transcript=True):
            candidate = meeting.get("id") or meeting.get("recording_id")
            if candidate is not None and str(candidate) == wanted:
                return meeting
        return None

    def extract_transcript_text(self, meeting_data: dict[str, Any]) -> str:
        """
        Given a single meeting object (containing a 'transcript' array),
        extracts and formats it into a single readable string with speakers.
        """
        transcript = meeting_data.get("transcript", [])
        if not transcript:
            return ""

        lines = []
        for entry in transcript:
            speaker = entry.get("speaker_name", "Unknown Speaker")
            text = entry.get("text", "")
            # Optionally include timestamps if available, e.g. entry.get("start_time")
            if text:
                lines.append(f"[{speaker}]: {text}")

        return "\n".join(lines)

    def extract_summary_text(self, meeting_data: dict[str, Any]) -> str:
        """Best-effort extraction of Fathom's AI meeting summary, or "".

        Fathom exposes the summary under a few possible keys depending on the
        API version / account config (``default_summary``, ``summary``,
        ``ai_summary``), and the value may be a markdown string or a dict
        carrying ``markdown``/``text``. We read whatever is present and return
        "" when none exists, so a schema difference can never break ingest.
        """
        for key in ("default_summary", "summary", "ai_summary"):
            val = meeting_data.get(key)
            if not val:
                continue
            if isinstance(val, str):
                return val.strip()
            if isinstance(val, dict):
                for sub in ("markdown", "text", "content", "body"):
                    s = val.get(sub)
                    if isinstance(s, str) and s.strip():
                        return s.strip()
        return ""

    def extract_action_items_text(self, meeting_data: dict[str, Any]) -> str:
        """Best-effort extraction of Fathom's action items as a bulleted list, or "".

        Fathom's meeting object exposes ``action_items`` (a list). Each item is
        usually a dict with a ``description``/``text`` and an optional
        ``assignee`` (dict with ``name``/``email``). Tolerates plain strings and
        missing fields so a shape change can't break ingest.
        """
        items = meeting_data.get("action_items") or meeting_data.get("actionItems")
        if not isinstance(items, list):
            return ""
        lines: list[str] = []
        for it in items:
            if isinstance(it, str):
                text, assignee = it.strip(), ""
            elif isinstance(it, dict):
                text = (
                    it.get("description") or it.get("text") or it.get("title") or ""
                ).strip()
                a = it.get("assignee") or it.get("assigned_to") or {}
                if isinstance(a, dict):
                    assignee = (a.get("name") or a.get("email") or "").strip()
                elif isinstance(a, str):
                    assignee = a.strip()
                else:
                    assignee = ""
            else:
                continue
            if not text:
                continue
            lines.append(f"- {text}" + (f" (owner: {assignee})" if assignee else ""))
        return "\n".join(lines)

    def build_ingest_text(self, meeting_data: dict[str, Any]) -> str:
        """Assemble the text stored for RAG: transcript + Fathom action items + summary.

        Appending Fathom's own action items and AI summary to the ingested
        transcript means a "what did we cover" / "draft a follow-up" query
        surfaces the meeting's to-do's and next steps even when they'd otherwise
        sit past the transcript's truncation window — the exact gap that made
        drafted follow-ups drop call to-do's.
        """
        transcript = self.extract_transcript_text(meeting_data)
        action_items = self.extract_action_items_text(meeting_data)
        summary = self.extract_summary_text(meeting_data)

        sections: list[str] = []
        if transcript:
            sections.append(transcript)
        if action_items:
            sections.append(
                "## Action Items & Next Steps (from Fathom)\n" + action_items
            )
        if summary:
            sections.append("## Meeting Summary (from Fathom)\n" + summary)
        return "\n\n".join(sections)

    @staticmethod
    def _transcript_speaker_tokens(meeting_data: dict[str, Any]) -> set[str]:
        """Lowercased name tokens of everyone who SPOKE in the transcript.

        Fathom labels each turn with a full ``speaker_name`` (e.g. "Jordan
        Hayes"), so this is the record of who actually attended and talked.
        """
        tokens: set[str] = set()
        for entry in meeting_data.get("transcript") or []:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("speaker_name") or "").strip().lower()
            if not name or name == "unknown speaker":
                continue
            for tok in re.split(r"[^a-z0-9]+", name):
                if len(tok) > 1:
                    tokens.add(tok)
        return tokens

    @staticmethod
    def _invitee_spoke(invitee: dict[str, Any], speaker_tokens: set[str]) -> bool:
        """True if this invitee appears among the transcript speakers.

        Matches on the invitee's family-name token (last token of their name, or
        of the email local-part). Family names collide far less than first names,
        so an invited-but-absent person who merely shares a first name with a
        speaker isn't wrongly granted access. Speaker labels are full names, so
        the family name is present whenever the person actually spoke. Fails open
        (returns True) when no name can be derived, so we never exclude on a
        parse miss.
        """
        name = (invitee.get("name") or "").strip().lower()
        tokens = [t for t in re.split(r"[^a-z0-9]+", name) if len(t) > 1]
        if not tokens:
            local = (invitee.get("email") or "").split("@", 1)[0].lower()
            tokens = [t for t in re.split(r"[^a-z0-9]+", local) if len(t) > 1]
        if not tokens:
            return True
        return tokens[-1] in speaker_tokens

    def extract_participants_from_meeting(
        self, meeting_data: dict[str, Any]
    ) -> list[str]:
        """Emails allowed to query this meeting's transcript — gated on ATTENDANCE.

        Access is who actually attended, not who was invited. Fathom's object
        gives the recorder (``recorded_by``) and the invited people
        (``calendar_invitees``, each ``email``/``name``), but attendance only
        shows up as the transcript's ``speaker_name`` labels. So an invitee is
        granted access only if their name appears among the transcript speakers —
        being on the calendar invite is NOT enough. This stops an invited-but-
        absent person (e.g. an exec cc'd on a team's L10) from reading notes for a
        meeting they never joined.

        Safeguards so real attendees are never locked out:
          * ``recorded_by`` is always included (they ran the recording).
          * No transcript speakers yet (unprocessed meeting) → fall back to the
            full invite list rather than granting no one.
          * NObody on the invite matches the speakers (likely an unexpected
            name-format, not a wholly-skipped meeting) → fail open to the invite
            list and log it.
        """
        emails: set[str] = set()

        # The recorder was in the meeting by definition.
        recorded_by = meeting_data.get("recorded_by") or {}
        if isinstance(recorded_by, dict) and recorded_by.get("email"):
            emails.add(recorded_by["email"].lower())

        invitees = [
            inv
            for inv in (meeting_data.get("calendar_invitees") or [])
            if isinstance(inv, dict) and inv.get("email")
        ]
        speaker_tokens = self._transcript_speaker_tokens(meeting_data)

        if not speaker_tokens:
            # No transcript to check attendance against — keep the invite list
            # (old behavior) rather than lock everyone out of an unprocessed call.
            for inv in invitees:
                emails.add(inv["email"].lower())
        else:
            attended = [
                inv for inv in invitees if self._invitee_spoke(inv, speaker_tokens)
            ]
            if invitees and not attended:
                # Nobody matched — almost certainly a speaker-name format we
                # didn't expect, not a meeting the whole invite list skipped.
                # Fail OPEN so real attendees keep access; log for visibility.
                logger.warning(
                    "Fathom ACL: no calendar invitee matched transcript speakers "
                    "for meeting %s; falling back to the full invite list.",
                    meeting_data.get("id") or meeting_data.get("title") or "?",
                )
                for inv in invitees:
                    emails.add(inv["email"].lower())
            else:
                for inv in attended:
                    emails.add(inv["email"].lower())

        # Back-compat: the older/webhook shape lists ACTUAL attendees directly
        # under `attendees`/`host_email` (not invitees), so trust them as
        # attendance without a transcript cross-check.
        for attendee in meeting_data.get("attendees") or []:
            if isinstance(attendee, dict) and attendee.get("email"):
                emails.add(attendee["email"].lower())
        host_email = meeting_data.get("host_email")
        if host_email:
            emails.add(host_email.lower())

        return sorted(emails)
