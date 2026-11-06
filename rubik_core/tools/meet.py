"""Google Meet REST API v2 client — attendance, conference records, transcripts.

Originally (and still primarily) used to gate meeting-transcript access on real
attendance (the calendar invite over-grants: invited-but-absent people shouldn't
read a meeting's notes). We list a conference's signed-in participants; the
caller resolves their Google ids to company emails and seeds the transcript ACL
from them.

The read scope is requested in ISOLATION (never added to the shared
Calendar/Drive ``SCOPES``) so a missing or revoked grant can only disable
attendance-gating — never break calendar reads or transcript sync.

Sales Call Capture adds two more jobs on top of that: creating a space with
auto-transcription turned on (the ONLY programmatic way to start a Meet
transcript — there is no "start transcribing now" call), and reading conference
records / transcript entries so a call can be ingested and cited. Those space
WRITES need two additional scopes, and they live behind their own credential
set and their own kill switch; see
``SPACE_WRITE_SCOPES`` for why the isolation outlived the grant.
"""

import datetime
import logging
import random
import re
import time
from collections.abc import Iterator
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/meetings.space.readonly"]

# Isolated on purpose, exactly like ``SCOPES`` above. Even once both are
# granted on the DWD client id, the isolation is right:
# it is what confines any future scope problem — a revoked grant, a console edit
# that drops one entry, a propagation delay — to space writes alone. Folding
# these into ``SCOPES`` would make EVERY call in this module request them, so one
# bad scope would break the attendance gating that is live today and load-bearing
# for transcript ACLs on both the Meet and Fathom ingest paths. Two sets means a
# write-scope failure can only ever disable arming and space creation.
#
# ``drive.meet.readonly`` is deliberately NOT here: it is only needed for the
# "download the transcript Doc if entry-level ingest fails" fallback, which v1
# does not build, and an unused ungranted scope is pure risk.
SPACE_WRITE_SCOPES = [
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.settings",
]

# A conference record is correlated to a transcript only when its end time is
# within this window of the transcript's creation (Gemini writes the doc minutes
# after the meeting ends). Recurring meetings reuse ONE space — so filtering by
# meeting code returns every past occurrence, and this window picks the right one.
_CORRELATION_WINDOW_SECONDS = 6 * 3600

# Meet coerces any list pageSize above 100 down to 100, and defaults are much
# smaller (25 for conference records, 10 for transcript entries) — so always ask
# for the max or a one-hour call needs 100+ round trips.
_PAGE_SIZE = 100

# Safety stop on every paginated read. Not expected to trigger: 100 pages is
# 10,000 items, and a bounded meeting has far fewer. It exists so a server-side
# paging bug loops for a bounded time instead of forever, and it LOGS when it
# fires — silent truncation of a transcript would corrupt evidence citations.
_MAX_PAGES = 100

_RETRY_ATTEMPTS = 4
_MAX_BACKOFF_SECONDS = 32.0
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class MeetSpaceWriteDisabled(RuntimeError):
    """A space write was attempted while ``CCS_MEET_ENABLED`` is off.

    Subclasses ``RuntimeError`` so callers that only guard broadly still catch
    it. Raised BEFORE any credential request, so the ungranted-scope 403 never
    even happens.
    """


# The three below are the named outcomes of looking a space up and trying to arm
# it. They exist because the calendar path cannot arm every row, and a row it
# cannot arm still has to render with a REASON — "we could not arm this" is a
# legitimate result, "nothing happened and nobody said why" is not. All three
# subclass ``RuntimeError`` for the same reason ``MeetSpaceWriteDisabled`` does:
# every existing broad guard keeps catching them.


class MeetSpaceNotFound(RuntimeError):
    """``spaces.get`` returned 404 for the name we asked about.

    Two very different causes, and the API cannot tell them apart for us:

    * the meeting code is wrong, stale, or the space has been reclaimed;
    * the space belongs to ANOTHER Workspace tenant — a client-hosted Meet. Our
      impersonated subject is in our tenant, so a foreign space is not "denied",
      it simply does not exist as far as this credential is concerned.

    Either way the correct product answer is the same: bind the meeting to the
    company, hand the AE the calendar's own Meet link, and say the transcript
    cannot be armed from here.
    """


class MeetSpaceNotPatchable(RuntimeError):
    """A space write was refused (403 / 404) for THIS impersonated subject.

    Carries ``subject`` and ``space_name`` because those two together are the
    entire diagnosis and the caller has neither at the point it catches this:
    every Meet call in this module runs as exactly one impersonated user via
    ``with_subject``, and there is no admin-authority path that could retry as
    somebody else.

    Deliberately NOT a claim about who is permitted to patch a space they did
    not create — that is unverified on this tenant. It is a claim about what
    happened: this subject asked, and Meet said no.
    """

    def __init__(self, message: str, *, subject: str, space_name: str):
        super().__init__(message)
        self.subject = subject
        self.space_name = space_name


class MeetArtifactsUnsupported(RuntimeError):
    """Meet's answer named the FEATURE or the LICENCE, not our request.

    This is the Workspace-edition verdict (the edition cannot auto-transcribe)
    and it is TENANT-WIDE: retrying it on the next row produces the identical
    error. A caller sweeping a day of meetings should say it once for the whole
    day, not once per meeting, and should stop attempting arming for that run.
    """


def _with_meet_backoff(fn, *args, attempts: int = _RETRY_ATTEMPTS, **kwargs):
    """Retry a Meet call with truncated exponential backoff on 429 / 5xx.

    Meet's quotas are per-project AND per-user: 6,000 reads/min/project,
    600 reads/min/user, 100 ``spaces.create``/min/project, 10
    ``spaces.create``/min/user. A roster-wide sweep is exactly the shape that
    reaches the per-project read ceiling, and Google has announced over-quota
    use becomes billable later in 2026 — so a 429 is backed off, never hammered.
    Sleep is ``min((2**n) + jitter, max_backoff)`` with sub-second jitter, so N
    users backing off in the same sweep de-synchronise instead of colliding
    again on the retry.

    Any other status re-raises immediately. A 403 here is a verdict — an
    ungranted or revoked scope, or a space this subject may not touch — not a
    blip, and retrying it three more times only delays the log line that
    explains the failure.
    """
    from googleapiclient.errors import HttpError

    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in _RETRY_STATUSES or attempt == attempts - 1:
                raise
            delay = min((2**attempt) + random.random(), _MAX_BACKOFF_SECONDS)
            logger.info(
                "Meet API %s — backing off %.1fs (attempt %d/%d)",
                status,
                delay,
                attempt + 1,
                attempts,
            )
            time.sleep(delay)


def _on_off(enabled: bool) -> str:
    """The ``artifactConfig`` auto-generation enum. Only ``ON``/``OFF`` exist."""
    return "ON" if enabled else "OFF"


#: Each ``artifactConfig`` sub-message and the one enum leaf it carries. The
#: patch mask is built from these LEAF paths because Meet rejects the parent:
#: ``updateMask=config.artifactConfig`` came back ``400 Invalid update mask
#: provided`` from Google. A leaf mask also changes only the named field, so a
#: sub-message the body leaves out is untouched rather than reset.
_ARTIFACT_LEAVES = {
    "transcriptionConfig": "autoTranscriptionGeneration",
    "recordingConfig": "autoRecordingGeneration",
    "smartNotesConfig": "autoSmartNotesGeneration",
}


def _artifact_config_body(
    *,
    auto_transcribe: bool,
    auto_record: bool | None = None,
    auto_smart_notes: bool | None = None,
) -> dict[str, Any]:
    """The ``artifactConfig`` message for a patch body.

    A sub-message whose flag is ``None`` is OMITTED, and because
    :func:`_artifact_update_mask` names only the leaves present in the body,
    omitted means "leave the space's current value alone".
    """
    artifact: dict[str, Any] = {
        "transcriptionConfig": {"autoTranscriptionGeneration": _on_off(auto_transcribe)}
    }
    if auto_record is not None:
        artifact["recordingConfig"] = {"autoRecordingGeneration": _on_off(auto_record)}
    if auto_smart_notes is not None:
        artifact["smartNotesConfig"] = {
            "autoSmartNotesGeneration": _on_off(auto_smart_notes)
        }
    return artifact


def _artifact_update_mask(artifact: dict[str, Any]) -> str:
    """The ``spaces.patch`` field mask for an ``artifactConfig`` body: one leaf
    path per sub-message actually sent, comma-joined."""
    return ",".join(
        f"config.artifactConfig.{sub}.{_ARTIFACT_LEAVES[sub]}" for sub in artifact
    )


def _artifact_flag(
    artifact_config: dict | None, sub_message: str, field: str
) -> bool | None:
    """Read one auto-generation enum back out of a live ``artifactConfig``.

    ``True``/``False`` for ``ON``/``OFF``; ``None`` when the sub-message is
    absent, unset, or carries a value this code does not recognise.

    An unparseable value is deliberately NOT read as ``ON``. Meet documents only
    ``ON``/``OFF`` today, so a third value would be new, and reading it as "on"
    would let :meth:`GoogleMeetClient.arm_space_transcription` skip a patch the
    space still needs. It logs instead, loudly.
    """
    raw = ((artifact_config or {}).get(sub_message) or {}).get(field)
    if raw is None:
        return None
    value = str(raw).strip().upper()
    if value == "ON":
        return True
    if value == "OFF":
        return False
    if value.endswith("_UNSPECIFIED") or not value:
        # Google's own "caller left it unset" sentinel — same thing as absent.
        return None
    logger.warning(
        "Meet %s.%s returned unrecognised value %r — treating it as unset rather "
        "than guessing ON or OFF.",
        sub_message,
        field,
        raw,
    )
    return None


# A Meet meeting code as it appears in a ``hangoutLink`` or the ``meetingCode``
# field: alphanumeric groups joined by single dashes (``abc-defg-hij``, and the
# undashed form Meet also accepts). The point of validating is narrow and real —
# an empty or URL-shaped value would be pasted straight into the resource name
# and send a request for the literal string ``spaces/``, whose behaviour nobody
# has established.
_MEETING_CODE_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
_MEETING_CODE_MAX = 64


def _valid_meeting_code(meeting_code: str | None) -> str:
    code = (meeting_code or "").strip()
    if (
        not code
        or len(code) > _MEETING_CODE_MAX
        or not _MEETING_CODE_RE.fullmatch(code)
    ):
        raise ValueError(
            f"Not a Meet meeting code: {meeting_code!r}. Expected the code alone "
            "(e.g. 'abc-defg-hij'), not a URL and not a 'spaces/...' resource "
            "name — see meeting_code_from_uri in call_capture/calendar_scan.py."
        )
    return code


# ── Classifying a space failure ──────────────────────────────────────────────
#
# Reading the message is the only way to tell these apart, so this encodes the
# rule: a 403 naming the SCOPES is a domain-wide-delegation grant problem and says
# NOTHING about the edition, while a 400 or a 403 naming the FEATURE or the
# licence IS the edition answer. Status alone is not enough, and the two have
# different owners and different blast radii — one is a Workspace Admin console
# change, the other is tenant-wide and permanent for the run.

_SCOPE_HINTS = (
    "scope",
    "insufficient authentication",
)

_FEATURE_HINTS = (
    "licen",  # licence / license / licensing
    "edition",
    "entitle",
    "not supported",
    "unsupported",
    "not enabled",
    "not available",
    "feature",
)


def _http_status(exc: Exception) -> int | None:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _http_error_text(exc: Exception) -> str:
    """Everything the server said, flattened for keyword matching.

    ``content`` (the raw JSON body) is where the licence / feature wording
    actually lives; ``reason`` and ``str(exc)`` are the short forms. Reading all
    three means a classifier never misses the verdict because google-api-client
    summarised it away.
    """
    parts: list[str] = []
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    if content:
        parts.append(str(content))
    try:
        reason = getattr(exc, "reason", None)
    except Exception:  # noqa: BLE001 — .reason parses the body and can throw
        reason = None
    if reason:
        parts.append(str(reason))
    parts.append(str(exc))
    return "\n".join(parts)


def _classify_space_error(
    exc: HttpError, *, operation: str, space_name: str, subject: str
) -> Exception | None:
    """Which named space failure an ``HttpError`` is — or ``None``.

    ``None`` means "not a shape this code recognises", and the caller re-raises
    the original error untouched. That is the whole discipline here: a
    mis-classified error becomes a row that tells the AE "the client hosts this
    Meet" when the truth was a revoked grant, and a wrong reason is worse than a
    missing one because nobody goes looking behind it.

    ``operation`` is ``"get"`` or ``"patch"``. The same 404 means different
    things on each: on a read it is "no such space for this credential", on a
    write it is "this subject's write did not land".
    """
    status = _http_status(exc)
    text = _http_error_text(exc).lower()
    names_scope = any(hint in text for hint in _SCOPE_HINTS)
    names_feature = any(hint in text for hint in _FEATURE_HINTS)

    if names_scope and names_feature:
        # Both families named at once. The two verdicts are mutually exclusive,
        # so asserting either one would be a coin flip dressed as a diagnosis.
        logger.warning(
            "Meet spaces.%s failure names both a scope and a feature/licence — "
            "refusing to classify it (space=%s subject=%s status=%s)",
            operation,
            space_name,
            subject,
            status,
        )
        return None

    if names_feature and status in (400, 403):
        return MeetArtifactsUnsupported(
            "Google Meet refused the artifact configuration on grounds of the "
            f"feature or the licence (HTTP {status} on spaces.{operation}). This "
            "is tenant-wide — the Workspace edition either does not include "
            "automatic transcription or does not allow setting it through the "
            f"API — so retrying other meetings will fail identically. Meet said: "
            f"{_http_error_text(exc).strip()[:500]}"
        )

    if names_scope:
        # A grant problem, not a per-meeting verdict: it is neither "this space
        # is unreachable" nor "this tenant cannot transcribe". Re-raise so it
        # surfaces as the infrastructure failure it is instead of being filed
        # against one AE's 2pm call.
        logger.error(
            "Meet spaces.%s was refused for a SCOPE reason (subject=%s space=%s "
            "status=%s) — check the domain-wide-delegation grant, not the "
            "meeting.",
            operation,
            subject,
            space_name,
            status,
        )
        return None

    if operation == "patch" and status in (403, 404):
        return MeetSpaceNotPatchable(
            f"Google Meet would not apply the artifact settings to {space_name} "
            f"as {subject} (HTTP {status}). Every Meet call here runs as exactly "
            "one impersonated user and there is no admin-authority retry, so "
            f"this space cannot be armed from {PRODUCT_NAME} — the meeting is still usable "
            "and still bound to its company; only the transcript is not "
            "guaranteed.",
            subject=subject,
            space_name=space_name,
        )

    if operation == "get" and status == 404:
        return MeetSpaceNotFound(
            f"No Meet space visible as {space_name} for {subject} (HTTP 404): "
            "the meeting code is wrong or expired, or the space belongs to "
            "another Workspace tenant (a client-hosted Meet)."
        )

    return None


def _parse_rfc3339(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _format_rfc3339(value: datetime.datetime | str) -> str | None:
    """RFC3339-with-``Z`` for a ``conferenceRecords.list`` filter literal.

    Accepts a ``datetime`` or an already-serialised string (a sweep watermark
    read back out of the DB is a string), normalising both through
    :func:`_parse_rfc3339`. Naive datetimes are read as UTC — the same
    assumption ``_parse_rfc3339`` and ``_pick_record`` already make, and the
    filter is compared server-side in UTC regardless.
    """
    dt = _parse_rfc3339(value)
    if dt is None:
        return None
    return (
        dt.astimezone(datetime.timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="milliseconds")
        + "Z"
    )


class GoogleMeetClient:
    #: Lazily built, space-WRITE-scoped service. A class-level default rather
    #: than an ``__init__`` assignment because the tests construct this client
    #: with ``__new__`` to skip the DWD build (backend/tests/
    #: test_meet_transcripts.py:569), so instance attributes set in ``__init__``
    #: are not guaranteed to exist.
    _space_service: Any = None

    def __init__(self, user_email: str):
        self.user_email = user_email
        self.service = self._build_service()

    def _build_service(self):
        from rubik_core.tools._workspace_auth import google_workspace_configured

        if not google_workspace_configured():
            logger.error("Workspace DWD credentials are not configured.")
            return None
        try:
            from rubik_core.tools._workspace_auth import (
                load_workspace_credentials,
                safe_refresh,
            )

            creds = load_workspace_credentials(SCOPES)
            if creds is None:
                return None
            delegated = creds.with_subject(self.user_email)
            safe_refresh(delegated)
            return build("meet", "v2", credentials=delegated, cache_discovery=False)
        except Exception as e:
            logger.error(
                f"Failed to init Google Meet client for {self.user_email}: {e}"
            )
            return None

    def list_signed_in_participants(
        self, meeting_code: str | None, around_time: datetime.datetime | None = None
    ) -> list[dict[str, str]]:
        """``[{"user_id", "display_name"}]`` for the signed-in participants of the
        conference for ``meeting_code`` whose end time is nearest ``around_time``.

        Recurring meetings reuse one space, so ``around_time`` disambiguates
        which occurrence's attendance to read. Only signed-in (identifiable)
        users are returned — anonymous/phone participants have no Google id to
        resolve to an account in the company. Returns ``[]`` on any error or
        when no occurrence confidently matches, so the caller can fall back
        without guarding.
        """
        if not self.service or not meeting_code:
            return []
        try:
            resp = (
                self.service.conferenceRecords()
                .list(filter=f'space.meeting_code="{meeting_code}"', pageSize=50)
                .execute()
            )
            record = self._pick_record(resp.get("conferenceRecords") or [], around_time)
            if record is None:
                return []
            return self._participants_for(record["name"])
        except Exception as e:
            logger.warning(
                "Meet participant lookup failed for code=%r (subject=%s): %s",
                meeting_code,
                self.user_email,
                e,
            )
            return []

    @staticmethod
    def _pick_record(
        records: list[dict], around_time: datetime.datetime | None
    ) -> dict | None:
        if not records:
            return None
        if around_time is None:
            return records[0]
        if around_time.tzinfo is None:
            around_time = around_time.replace(tzinfo=datetime.timezone.utc)
        best, best_delta = None, None
        for r in records:
            end = _parse_rfc3339(r.get("endTime") or r.get("startTime"))
            if end is None:
                continue
            delta = abs((end - around_time).total_seconds())
            if best_delta is None or delta < best_delta:
                best, best_delta = r, delta
        if (
            best is not None
            and best_delta is not None
            and best_delta <= _CORRELATION_WINDOW_SECONDS
        ):
            return best
        return None

    def _participants_for(self, parent: str) -> list[dict[str, str]]:
        people: list[dict[str, str]] = []
        page_token: str | None = None
        for _ in range(10):  # page cap — a bounded meeting can't have 1000s
            kwargs: dict[str, Any] = {"parent": parent, "pageSize": 100}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = (
                self.service.conferenceRecords().participants().list(**kwargs).execute()
            )
            for p in resp.get("participants") or []:
                signed_in = p.get("signedinUser") or {}
                uid = signed_in.get("user")
                if uid:
                    people.append(
                        {"user_id": uid, "display_name": signed_in.get("displayName")}
                    )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return people

    def list_participants(self, conference_record: str) -> list[dict]:
        """Every participant of one conference, as the RAW Meet resources.

        ``GET /v2/{parent=conferenceRecords/*}/participants``, fully paginated.

        This is the join key for transcript ingest and it is deliberately not
        ``list_signed_in_participants``: that method returns a flattened
        ``{user_id, display_name}`` projection for attendance gating and drops
        the ``name`` field, which is the only thing a ``TranscriptEntry``'s
        ``participant`` resource name can be matched against. Callers that need
        identity per utterance need the raw rows; callers that need "who was in
        the room" want the projection.

        Raises, like the other reads added for CCS — see ``_require_service``.
        A silent ``[]`` here would attribute every utterance to "Unknown" and
        the wrap-up would still render, which is the kind of failure nobody
        notices until an AE asks why the quotes have no names.
        """
        service = self._require_service()
        return list(
            self._paginate(
                service.conferenceRecords().participants().list,
                "participants",
                parent=conference_record,
            )
        )

    # ── Space READS — resolution for the Calendar path ───────────────────────
    #
    # On the READ service on purpose. The calendar screen's primary job is to
    # bind a meeting to the right HubSpot company and hand the AE the calendar's
    # own Meet link; arming transcription is secondary and best-effort. So
    # LOOKING a space up must not depend on the write flag or the write scopes —
    # with ``CCS_MEET_ENABLED`` off, a row still has to be able to explain
    # itself ("Meet writes are disabled") rather than fail before it can look.
    # ``spaces.get`` is covered by ``meetings.space.readonly``, which is the
    # scope this module has had all along.

    def get_space(self, name: str) -> dict:
        """One Space resource. ``GET /v2/{name=spaces/*}``.

        ``name`` may be the canonical ``spaces/{space}``, or — per Google's
        reference for ``spaces.get`` — the ``spaces/{meetingCode}`` alias.

        ⚠️ That alias is DOCUMENTED and UNVERIFIED on this tenant: nothing in
        this repo has ever exercised it. So a 404 from a code-shaped name means
        "the code is wrong or expired, OR the space is in another tenant, OR the
        alias does not work here" — three causes, and this method cannot tell
        them apart. Do not read one 404 as proof about the alias.

        The Space resource carries ``name`` and ``meetingCode`` as separate
        fields, which is what makes resolve-then-patch-by-canonical-name work:
        whatever you looked it up by, the response tells you the real name.

        Raises :class:`MeetSpaceNotFound` on 404. A 403 (and anything else) is
        re-raised as the original ``HttpError`` — a refused read is not a
        statement about the space, and inventing a verdict for it would put a
        wrong reason on the AE's screen.
        """
        service = self._require_service()
        target = (name or "").strip()
        if not target or target.rstrip("/") == "spaces":
            raise ValueError(
                f"Not a Meet space name: {name!r}. Expected 'spaces/{{space}}' "
                "or 'spaces/{meetingCode}'."
            )
        try:
            return _with_meet_backoff(
                lambda: service.spaces().get(name=target).execute()
            )
        except HttpError as exc:
            classified = _classify_space_error(
                exc, operation="get", space_name=target, subject=self.user_email
            )
            if classified is not None:
                raise classified from exc
            raise

    def resolve_space_name(self, meeting_code: str) -> str:
        """A meeting code -> the canonical ``spaces/{space}`` name. One hop.

        The calendar gives us a Meet URI, so a code is all we ever have for a
        meeting Cubie did not create. ``spaces.patch`` does not document the
        ``spaces/{meetingCode}`` alias, so resolving first is what makes the
        write addressable at all.

        Raises ``ValueError`` on a blank or malformed code — before any HTTP —
        so a missing value can never become a request for the literal resource
        name ``spaces/``. Raises :class:`MeetSpaceNotFound` when the lookup 404s
        or comes back without a ``name``.
        """
        code = _valid_meeting_code(meeting_code)
        space = self.get_space(f"spaces/{code}") or {}
        canonical = str(space.get("name") or "").strip()
        if not canonical:
            raise MeetSpaceNotFound(
                f"Meet returned a space for code {code!r} with no 'name' field, "
                "so there is nothing to address a write to."
            )
        return canonical

    # ── Space writes (grant-gated) — Sales Call Capture ──────────────────────
    #
    # There are only three write methods on the whole Meet API: spaces.create,
    # spaces.patch and spaces.endActiveConference. Transcripts, recordings and
    # smart notes are read-only, and there is NO "start transcription now" call
    # — auto-generation via artifactConfig at space creation is the only
    # programmatic route, which is why "start the call from Cubie" exists at all.

    def _space_write_service(self):
        """Build (once) a Meet service scoped for space writes.

        Mirrors ``_build_service`` but with ``SPACE_WRITE_SCOPES``, kept separate
        and lazy so a missing grant can only fail space writes — never the
        attendance reads above. Follows ``rubik_core/tools/drive.py``'s
        Labels-scope pattern.

        Raises rather than returning ``None``: a space write that silently
        no-ops leaves an AE in a Meet with no transcript and no explanation,
        which is worse than a 500 the router can turn into a real message.
        """
        if not settings.CCS_MEET_ENABLED:
            raise MeetSpaceWriteDisabled(
                "Meet space writes are disabled by CCS_MEET_ENABLED=false, so "
                "no meeting space was created and no transcription was armed. "
                f"Once the {', '.join(SPACE_WRITE_SCOPES)} scopes ARE granted on "
                "the domain-wide-delegation client id, the blocker is this "
                f"{PRODUCT_NAME} switch and not a Workspace Admin grant: turn "
                "CCS_MEET_ENABLED on to enable space writes."
            )
        if self._space_service is not None:
            return self._space_service
        from rubik_core.tools._workspace_auth import google_workspace_configured

        if not google_workspace_configured():
            raise MeetSpaceWriteDisabled(
                "Workspace DWD credentials are not configured."
            )
        from rubik_core.tools._workspace_auth import (
            load_workspace_credentials,
            safe_refresh,
        )

        creds = load_workspace_credentials(SPACE_WRITE_SCOPES)
        if creds is None:
            raise MeetSpaceWriteDisabled(
                "Could not load Workspace credentials for Meet space writes."
            )
        delegated = creds.with_subject(self.user_email)
        safe_refresh(delegated)
        self._space_service = build(
            "meet", "v2", credentials=delegated, cache_discovery=False
        )
        return self._space_service

    def create_space(
        self,
        *,
        auto_transcribe: bool,
        auto_record: bool = False,
        auto_smart_notes: bool = True,
    ) -> dict:
        """Create a Meet space with auto-artifact generation pre-configured.

        ``POST /v2/spaces``. Returns the Space resource — ``name``
        (``spaces/{space}``), ``meetingUri`` (what the frontend opens),
        ``meetingCode`` and the echoed ``config``.

        ``auto_record`` defaults **off**: recording a sales call is a materially
        bigger consent question than transcribing it, and nothing in CCS reads a
        recording. The caller passes ``auto_transcribe=False`` for a session
        whose consent is ``suppressed``, which produces a normal space that
        generates no artifacts at all.

        Raises :class:`MeetSpaceWriteDisabled` when ``CCS_MEET_ENABLED`` is off.
        """
        service = self._space_write_service()
        body = {
            "config": {
                "artifactConfig": {
                    "transcriptionConfig": {
                        "autoTranscriptionGeneration": _on_off(auto_transcribe)
                    },
                    "recordingConfig": {
                        "autoRecordingGeneration": _on_off(auto_record)
                    },
                    "smartNotesConfig": {
                        "autoSmartNotesGeneration": _on_off(auto_smart_notes)
                    },
                }
            }
        }
        space = _with_meet_backoff(lambda: service.spaces().create(body=body).execute())
        logger.info(
            "Created Meet space %s for %s (transcribe=%s record=%s smart_notes=%s)",
            space.get("name"),
            self.user_email,
            auto_transcribe,
            auto_record,
            auto_smart_notes,
        )
        return space

    def arm_space_transcription(
        self, *, meeting_code: str | None = None, space_name: str | None = None
    ) -> dict:
        """Turn auto-transcription ON for a space Cubie did not create.

        This is the Calendar path's write. Resolve the name, READ the current
        ``artifactConfig``, then patch ONLY the transcription leaf by the
        canonical name that came back.

        ONLY THE TRANSCRIPTION LEAF IS SENT. On a Calendar-created space the
        recording and smart-notes settings are the HOST's, and this tenant
        returns smart notes ON by default. The patch mask names
        ``config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration``
        and nothing else, so those two are left exactly as the host had them —
        not re-sent, not reset. (The first version masked the parent
        ``config.artifactConfig`` and re-sent all three to survive the
        replacement; Meet rejects that mask outright with a 400.)

        Pass ``meeting_code`` (from the calendar's Meet URI) or ``space_name``.
        Only ONE ``spaces.get`` happens either way — this deliberately does not
        call :meth:`resolve_space_name`, which would double the round trip, since
        the same response carries both the canonical name and the config.

        Returns::

            {"space_name": "spaces/...", "meeting_code": "abc-defg-hij",
             "state": "armed" | "already_armed",
             "artifact_config_before": {...}, "artifact_config_after": {...}}

        ``already_armed`` performs NO write: transcription was on before we
        arrived (a host default, or a previous Cubie run, or a recurring series
        armed on an earlier occurrence — one space is reused for the whole
        series). ``artifact_config_before`` is kept as evidence of what the host
        actually had, so a later argument about a lost recording setting can be
        settled from the record.

        A successful patch is NOT proof that a transcript will exist. Meet has
        been observed to accept an artifact configuration and ignore it, so
        the echoed ``artifact_config_after`` is logged when it does not say ON,
        and the sweep must keep treating "no transcript arrived" as a normal
        outcome with a named reason.

        Raises: ``ValueError`` (neither argument, or a malformed code),
        :class:`MeetSpaceNotFound`, :class:`MeetSpaceWriteDisabled`,
        :class:`MeetSpaceNotPatchable`, :class:`MeetArtifactsUnsupported`.
        Anything else is the original ``HttpError``.
        """
        if not meeting_code and not space_name:
            raise ValueError(
                "arm_space_transcription needs meeting_code or space_name."
            )
        lookup = (
            str(space_name).strip()
            if space_name
            else f"spaces/{_valid_meeting_code(meeting_code)}"
        )
        space = self.get_space(lookup) or {}
        canonical = str(space.get("name") or "").strip()
        if not canonical:
            raise MeetSpaceNotFound(
                f"Meet returned a space for {lookup!r} with no 'name' field, so "
                "there is nothing to address the write to."
            )
        code = str(space.get("meetingCode") or (meeting_code or "")).strip() or None
        before = dict(((space.get("config") or {}).get("artifactConfig")) or {})

        if _artifact_flag(before, "transcriptionConfig", "autoTranscriptionGeneration"):
            logger.info(
                "Meet space %s (code=%s) is already set to auto-transcribe — "
                "no patch sent for %s",
                canonical,
                code,
                self.user_email,
            )
            return {
                "space_name": canonical,
                "meeting_code": code,
                "state": "already_armed",
                "artifact_config_before": before,
                "artifact_config_after": dict(before),
            }

        patched = self.patch_space_artifacts(canonical, auto_transcribe=True)
        after = dict(((patched or {}).get("config") or {}).get("artifactConfig") or {})
        if not after:
            # The patch response is documented to echo the Space, but if this
            # tenant returns a thin body, reporting the config we ASKED for beats
            # reporting nothing — flagged in the log so the two are never
            # confused when someone reads the stored verdict later. The leaf
            # mask left the other sub-messages alone, so "asked for" is the
            # config we read with transcription flipped on.
            logger.warning(
                "Meet spaces.patch for %s returned no config to echo; reporting "
                "the requested configuration as artifact_config_after",
                canonical,
            )
            after = {**before, **_artifact_config_body(auto_transcribe=True)}
        elif not _artifact_flag(
            after, "transcriptionConfig", "autoTranscriptionGeneration"
        ):
            # Meet accepted the config and ignored it. Say so here; the row
            # still reports "armed" because that is what we did, and the echoed
            # config is the evidence that it may not have taken.
            logger.warning(
                "Meet accepted the transcription patch for %s but echoed %r — "
                "a transcript may not be generated for this call",
                canonical,
                after,
            )
        logger.info(
            "Armed auto-transcription on Meet space %s (code=%s) for %s "
            "(recording and smart notes not touched)",
            canonical,
            code,
            self.user_email,
        )
        return {
            "space_name": canonical,
            "meeting_code": code,
            "state": "armed",
            "artifact_config_before": before,
            "artifact_config_after": after,
        }

    def patch_space_artifacts(
        self,
        space_name: str,
        *,
        auto_transcribe: bool,
        auto_record: bool | None = None,
        auto_smart_notes: bool | None = None,
    ) -> dict:
        """Set auto-artifact generation on an EXISTING space.

        ``PATCH /v2/{space.name=spaces/*}`` with ``updateMask`` passed
        explicitly. Meet's own note is that an unset mask "defaults to update all
        fields provided with values", which is a behaviour to depend on by
        accident rather than design.

        The mask is one LEAF path per flag passed (see
        :func:`_artifact_update_mask`), e.g.
        ``config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration``.
        ``auto_record`` / ``auto_smart_notes`` default to ``None``, which leaves
        them out of both the body and the mask, so the space keeps whatever it
        had. Meet rejects the parent mask ``config.artifactConfig`` with ``400
        Invalid update mask provided``; that 400 is unclassified here and
        surfaces as the original ``HttpError``.

        ``space_name`` should be the canonical ``spaces/{space}`` from a
        ``spaces.get`` / ``spaces.create`` response. The ``spaces/{meetingCode}``
        alias is documented on ``spaces.get`` and NOT on ``spaces.patch``, so a
        code parsed out of a ``hangoutLink`` may simply fail here — resolve it
        first with :meth:`resolve_space_name`.

        Failure modes, which are separate problems with separate owners:

        * :class:`MeetSpaceWriteDisabled` — ``CCS_MEET_ENABLED`` is off; raised
          before any credential request.
        * :class:`MeetArtifactsUnsupported` — Meet's answer named the feature or
          the licence. Tenant-wide; every other meeting will answer the same.
        * :class:`MeetSpaceNotPatchable` — 403/404 for this impersonated
          subject. Carries the subject and the space name.
        * anything else — the original ``HttpError``, unclassified on purpose.
        """
        service = self._space_write_service()
        artifact = _artifact_config_body(
            auto_transcribe=auto_transcribe,
            auto_record=auto_record,
            auto_smart_notes=auto_smart_notes,
        )
        body = {"config": {"artifactConfig": artifact}}
        mask = _artifact_update_mask(artifact)
        try:
            return _with_meet_backoff(
                lambda: (
                    service.spaces()
                    .patch(name=space_name, updateMask=mask, body=body)
                    .execute()
                )
            )
        except HttpError as exc:
            classified = _classify_space_error(
                exc,
                operation="patch",
                space_name=space_name,
                subject=self.user_email,
            )
            if classified is not None:
                raise classified from exc
            raise

    # ── Conference-record and transcript reads — Sales Call Capture ──────────

    def _require_service(self):
        """The read-scoped service, or a loud failure.

        The attendance methods above swallow errors and return ``[]`` on purpose
        — a failed ACL read falls back to the invite list. The CCS reads must
        NOT do that: a sweep that reports "no conference records" because the
        grant was revoked looks exactly like "nobody had any calls today", and
        would disable the whole feature silently. Mirrors
        ``rubik_core/tools/directory.py``'s raise-on-uninitialised-service.
        """
        if self.service is None:
            raise RuntimeError(
                f"Meet service not initialized for {self.user_email} "
                "(missing Workspace key or failed token refresh)."
            )
        return self.service

    def _paginate(self, request_fn, item_key: str, **kwargs) -> Iterator[dict]:
        """Yield every item across every page of a Meet list call."""

        def _fetch(**params):
            return request_fn(**params).execute()

        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            params = dict(kwargs, pageSize=_PAGE_SIZE)
            if page_token:
                params["pageToken"] = page_token
            resp = _with_meet_backoff(_fetch, **params)
            yield from resp.get(item_key) or []
            page_token = resp.get("nextPageToken")
            if not page_token:
                return
        logger.warning(
            "Meet %s pagination hit the %d-page cap (%s) — results truncated",
            item_key,
            _MAX_PAGES,
            kwargs,
        )

    def list_conference_records(
        self,
        *,
        space_name: str | None = None,
        start_after: datetime.datetime | str | None = None,
    ) -> list[dict]:
        """Conference records visible to ``user_email``, newest-API-order.

        ``GET /v2/conferenceRecords``, filtering on ``space.name`` and/or
        ``start_time``, fully paginated. Each record carries ``name``,
        ``space``, ``startTime``, ``endTime`` (unset while the conference is
        live) and ``expireTime``.

        With neither argument this lists every record the impersonated user can
        see — which is what the CCS sweep wants, because a conference record
        exists for **any** space the AE owns, ad-hoc meetings included, so "a
        call happened" is never invisible even when Cubie didn't create the space.

        Recurring meetings reuse ONE space, so filtering by ``space_name`` (or by
        meeting code, as ``list_signed_in_participants`` does) returns EVERY past
        occurrence — not one row. Use :meth:`_pick_record` /
        ``_CORRELATION_WINDOW_SECONDS`` to choose the occurrence that belongs to
        a given transcript or session; the CCS sweep hits exactly this.

        🔴 Records and their transcript entries are deleted **30 days** after the
        conference ends (``expireTime``). Anything not persisted on ingest is
        gone; a lookback longer than 30 days finds nothing to backfill.
        """
        service = self._require_service()
        clauses: list[str] = []
        if space_name:
            clauses.append(f'space.name = "{space_name}"')
        since = _format_rfc3339(start_after) if start_after else None
        if since:
            clauses.append(f'start_time >= "{since}"')
        elif start_after:
            # An unparseable watermark must not quietly widen the sweep to "all
            # time" — that is a 30-day scan of every user on every run.
            raise ValueError(
                f"Unparseable start_after for Meet filter: {start_after!r}"
            )
        kwargs: dict[str, Any] = {}
        if clauses:
            kwargs["filter"] = " AND ".join(clauses)
        return list(
            self._paginate(
                service.conferenceRecords().list, "conferenceRecords", **kwargs
            )
        )

    def list_transcripts(self, conference_record: str) -> list[dict]:
        """Transcripts for one conference record (``conferenceRecords/{id}``).

        ``GET /v2/{parent}/transcripts``, fully paginated. Each carries ``name``
        (the parent for :meth:`list_transcript_entries`), ``state``,
        ``startTime`` / ``endTime`` and ``docsDestination`` — the Google Doc,
        which survives per Drive/Vault policy even after the structured entries
        expire. A conference can have several: one per transcription session, so
        a host who toggles transcription mid-call produces more than one.
        """
        service = self._require_service()
        return list(
            self._paginate(
                service.conferenceRecords().transcripts().list,
                "transcripts",
                parent=conference_record,
            )
        )

    def list_transcript_entries(self, transcript_name: str) -> Iterator[dict]:
        """Stream every utterance of one transcript, in order.

        ``GET /v2/{parent}/entries``, fully paginated. Each entry carries
        ``name``, ``participant`` (a participant resource name, NOT an email —
        resolve it via the participants list), ``text`` (≤10K words),
        ``languageCode``, ``startTime`` and ``endTime``.

        Returns a lazy iterator: a long call is thousands of rows and the ingest
        writes them as it reads. The service check happens eagerly, so a broken
        client fails at the call rather than on first ``next()``; per-page HTTP
        errors still surface during iteration.
        """
        service = self._require_service()
        return self._paginate(
            service.conferenceRecords().transcripts().entries().list,
            "transcriptEntries",
            parent=transcript_name,
        )
