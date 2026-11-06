"""Which recording belongs to an L10 session, and who was actually in it.

This module answers the two questions the post-meeting summary email is built
on, and nothing else:

1. **Was this L10 recorded?** — find the transcript ``Asset`` that belongs to
   one ``eos_l10_sessions`` row (:func:`find_recording`). No recording, no
   email: the summary is grounded in what was *said*, so without a transcript
   there is nothing to send.
2. **Who was actually in the room?** — of the people on that transcript's ACL,
   which ones the transcript itself puts in the meeting
   (:func:`present_participants`).

**Why the join is fuzzy.** Nothing links an EOS L10 session to a transcript.
A session is created when a facilitator clicks Start in Cubie; a transcript is
created by Gemini / Fathom and swept in later by
``rubik_core/scripts/transcript_sync.py``, which keys it on the Drive
``file_id`` and stores calendar-derived attendance in ``meeting_participants``.
The L10 meeting *does* carry a ``google_event_id`` when it was imported from
Calendar — but the transcript side never records the event id it matched, so
there is no shared key to join on. What we have instead is the transcript's
FILENAME (which is the calendar event's title plus "- <date> - Notes by
Gemini") and its ingest time. So we match on title + time window, and require
attendance overlap when the meeting has a roster to check against. Every
threshold below is deliberately conservative: mailing a team someone else's
meeting notes is far worse than mailing nothing.

**Why the time window is asymmetric and hours wide.** ``Asset.created_at`` is
the app-wide stand-in for "when the meeting happened" (see
``rubik_core/graph/writers._transcript_metadata``) but it is really the INGEST
time — the transcript sync runs every 15 minutes with ``--days 1``, and Gemini
itself takes a while to publish notes. So the window opens shortly before the
session starts and closes well after it ends. It stays far short of a week, so
it can never reach the next occurrence of a weekly meeting.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ── Meeting-title recognition ───────────────────────────────────────────────
# The feature is scoped to L10s by NAME, per product decision: only a meeting
# whose title carries "L10" gets a summary email. Matched as a whole token with
# tolerance for the ways people type it ("L10", "L-10", "L 10"), so "HL105"
# and "Level 10" are NOT matches — the latter deliberately: the rule is the
# literal word, and widening it to prose would start mailing recaps for
# meetings nobody scoped in.
L10_TITLE_RE = re.compile(r"\bl[\s._-]?10\b", re.IGNORECASE)

# "Notes by Gemini" transcripts are named "<event title> - <date> - Notes by
# Gemini". Strip the boilerplate + trailing date so what's left is the calendar
# title we can compare to the L10 meeting's name.
GEMINI_SUFFIX_RE = re.compile(r"\s*[-–]\s*Notes by Gemini\s*$", re.IGNORECASE)
TRAILING_DATE_RE = re.compile(r"\s*[-–(]\s*\d{4}[/-]\d{1,2}[/-]\d{1,2}.*$")

# How far before a session's start, and after the end of its BOX, a
# transcript's ingest stamp may fall and still belong to it. LAG is generous
# because it absorbs Gemini's publish delay plus the sync's own cadence.
#
# ⚠️ The window is anchored on ``actual_start`` + the meeting's box, NEVER on
# ``actual_end``. An auto-closed session's ``actual_end`` is its last write,
# which drifts for as long as nobody concludes the meeting, so a session can
# span 141 h, 338 h or 676 h. An end-anchored window is therefore WEEKS wide
# and reaches straight into other occurrences of the same weekly meeting: a
# session can match the recording of the week before. ``actual_start`` is when
# somebody actually clicked Start, so start + box + LAG is bounded at ~38 h
# however long the row was left open.
RECORDING_LEAD = timedelta(hours=2)
RECORDING_LAG = timedelta(hours=36)

# A meeting with no ``duration_minutes`` gets the EOS-standard 90-minute box.
DEFAULT_BOX_MINUTES = 90

# Below this many characters a transcript cannot ground a recap, so the meeting
# counts as unrecorded. Google sometimes publishes an EMPTY notes doc: an L10
# "recording" of 3 bytes, a bare UTF-8 BOM and nothing else. A real 60-90
# minute L10 runs to tens of thousands of characters, so this
# floor only ever catches genuine emptiness.
MIN_TRANSCRIPT_CHARS = 500

# Minimum title similarity for a transcript to be considered this meeting's.
# 0.6 accepts "Systems L10" ↔ "Systems L10 Weekly" and rejects "Systems L10" ↔
# "Delivery L10" (which share only the "l10" token).
MIN_TITLE_SCORE = 0.6

_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def is_l10_title(name: str | None) -> bool:
    """True iff a meeting name carries the word "L10" (see :data:`L10_TITLE_RE`)."""
    return bool(name) and bool(L10_TITLE_RE.search(name or ""))


def title_hint_from_filename(name: str | None) -> str:
    """Best-effort calendar title from a transcript filename."""
    if not name:
        return ""
    base = name
    if base.lower().endswith(".txt"):
        base = base[:-4]
    base = GEMINI_SUFFIX_RE.sub("", base)
    base = TRAILING_DATE_RE.sub("", base)
    return base.strip()


def _tokens(text: str) -> list[str]:
    return [t for t in _NON_WORD_RE.sub(" ", (text or "").lower()).split() if t]


def _normalize(text: str) -> str:
    return " ".join(_tokens(text))


def title_score(meeting_name: str, transcript_title: str) -> float:
    """0..1 similarity between an L10's name and a transcript's title.

    Deliberately crude and explainable rather than clever: exact match after
    normalization, then substring containment, then token containment. A fuzzy
    edit-distance score would happily rate "Systems L10" and "Sales L10" as
    near-identical, which is precisely the mistake that mails one team another
    team's notes.
    """
    left, right = _normalize(meeting_name), _normalize(transcript_title)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.8
    lt, rt = set(_tokens(left)), set(_tokens(right))
    shared = lt & rt
    if not shared:
        return 0.0
    # Containment against the SMALLER side: a short meeting name fully present
    # in a long transcript title is a strong signal, and dividing by the union
    # (Jaccard) would punish it for the extra words.
    return min(0.79, len(shared) / min(len(lt), len(rt)))


def naive_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to naive UTC, the shape every stamp in this DB uses.

    ``eos_l10_sessions`` writes ``datetime.utcnow()`` (naive) while the
    transcript sync writes ``datetime.now(timezone.utc)`` (aware) — and which
    one comes back from the driver depends on the backend (Postgres strips the
    offset, SQLite preserves it). Comparing the two raises TypeError, so every
    stamp goes through here first.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Recording:
    """One transcript, matched to an L10 session."""

    doc_id: str
    title: str
    title_hint: str
    created_at: datetime
    owner_email: str
    source_path: str | None
    # The transcript's authoritative ACL (``meeting_participants``). Everyone
    # allowed to read this recording — a superset of who was actually in it.
    participant_emails: list[str]
    title_score: float
    attendance_overlap: int


def find_recording(
    db,
    *,
    meeting_name: str,
    session_start: datetime,
    box_minutes: int | None = None,
    expected_emails: set[str] | None = None,
    exclude_doc_ids: set[str] | None = None,
) -> Recording | None:
    """The transcript belonging to one L10 session, or ``None`` if unrecorded.

    ``expected_emails`` is the meeting's own roster (calendar invitees, or the
    team when there is no invite list), lower-cased. When it is non-empty it is
    a HARD gate: a candidate transcript whose participants don't intersect it
    is somebody else's meeting no matter how well its title scores. This is the
    check that makes a title-and-time match safe enough to mail.

    ``exclude_doc_ids`` are recordings already claimed by another session. A
    recording belongs to exactly one meeting occurrence, so a claimed one is
    skipped and the next-best candidate considered.

    Ranking, once every hard gate has passed: title score, then PROXIMITY to
    the session's start, then attendance overlap. Proximity outranks overlap
    because overlap is already a gate — two candidates that both clear it are
    equally plausible on attendance, and the tighter timestamp is then the
    better evidence. Ordering it the other way is what let a week-old recording
    with a fuller attendee list beat the right one.
    """
    from rubik_core.database.models import Asset, MeetingParticipant

    start = naive_utc(session_start)
    if start is None:
        return None
    box = timedelta(minutes=box_minutes or DEFAULT_BOX_MINUTES)
    window_open = start - RECORDING_LEAD
    window_close = start + box + RECORDING_LAG
    skip = {d for d in (exclude_doc_ids or set()) if d}

    # Narrow in SQL first — this runs every 15 minutes and the assets table
    # holds every DAM document, not just transcripts. The SQL bound is padded
    # well beyond the real window (and the exact window is re-checked in Python
    # below) so no candidate can be lost to a driver's timezone handling: this
    # column is naive on Postgres but the sync writes tz-aware values, and
    # SQLite preserves the offset it was given.
    sql_pad = timedelta(days=3)
    rows = (
        db.query(Asset)
        .filter(
            Asset.topic == "Meeting Transcripts",
            Asset.status == "approved",
            Asset.created_at.isnot(None),
            Asset.created_at >= window_open - sql_pad,
            Asset.created_at <= window_close + sql_pad,
        )
        .all()
    )

    expected = {e.lower() for e in (expected_emails or set()) if e}
    best: Recording | None = None
    for asset in rows:
        if asset.doc_id in skip:
            continue
        created = naive_utc(asset.created_at)
        if created is None or not (window_open <= created <= window_close):
            continue
        hint = title_hint_from_filename(asset.original_filename)
        score = title_score(meeting_name, hint)
        if score < MIN_TITLE_SCORE:
            continue

        participants = sorted(
            {
                (row[0] or "").lower()
                for row in db.query(MeetingParticipant.participant_email)
                .filter(MeetingParticipant.doc_id == asset.doc_id)
                .all()
                if row[0]
            }
        )
        overlap = len(expected & set(participants)) if expected else 0
        if expected and overlap == 0:
            logger.info(
                "[l10-summary] transcript %s (%r) matched %r on title but shares "
                "no attendee with the meeting — not this meeting's recording",
                asset.doc_id,
                hint,
                meeting_name,
            )
            continue

        candidate = Recording(
            doc_id=asset.doc_id,
            title=asset.original_filename or hint,
            title_hint=hint,
            created_at=created,
            owner_email=(asset.owner_email or "").lower(),
            source_path=asset.source_path,
            participant_emails=participants,
            title_score=score,
            attendance_overlap=overlap,
        )
        if best is None or _better(candidate, best, start + box):
            best = candidate
    return best


def _better(candidate: Recording, incumbent: Recording, anchor: datetime) -> bool:
    """Is ``candidate`` a better match than ``incumbent``?

    ``anchor`` is the END of the meeting's box, not its start: a recording is
    PUBLISHED after the meeting finishes, so measuring proximity from the start
    systematically favours anything ingested beforehand — which by definition
    cannot be this meeting's recording. (Anchoring on the start made a decoy
    ingested an hour BEFORE the meeting beat the real transcript ingested 20
    minutes after it.)
    """

    def key(rec: Recording) -> tuple:
        return (
            rec.title_score,
            -abs((rec.created_at - anchor).total_seconds()),
            rec.attendance_overlap,
        )

    return key(candidate) > key(incumbent)


# ── Who was actually in the recording ───────────────────────────────────────


def _name_variants(name: str) -> list[str]:
    """Normalized forms of a display name worth looking for in a transcript.

    Google Meet's Gemini notes and Fathom both label speakers "First Last", and
    an attendee roll-call sometimes renders "Last, First" — so both orderings
    are checked. Single-token names are dropped: a bare first name is far too
    common to treat as proof somebody attended.
    """
    tokens = _tokens(name)
    if len(tokens) < 2:
        return []
    forward = " ".join(tokens)
    reversed_ = " ".join([tokens[-1], *tokens[:-1]])
    return [forward] if reversed_ == forward else [forward, reversed_]


def present_participants(
    transcript_text: str,
    *,
    candidates: dict[str, str],
    owner_email: str = "",
) -> set[str]:
    """Of ``candidates`` (email → display name), who the transcript itself
    places in the meeting.

    A transcript's ``meeting_participants`` ACL is a UNION of several sources —
    emails scraped from the text, names matched in its header, and either the
    Meet conference's signed-in joiners or, when that can't be determined, the
    calendar INVITE LIST (which includes no-shows). That superset is the right
    answer for "who may read this", and the wrong answer for "who was here":
    invitees who never joined would get a recap of a meeting they missed. So
    presence is re-derived from the transcript body:

    * their email address appears in it, or
    * their full name appears in it as whole words (either name order), or
    * they OWN the recording — it exists in their Drive / under their Fathom
      account because they hosted the meeting, which is attendance by
      construction even if they never spoke and Gemini never named them.

    Returns lower-cased emails, a subset of ``candidates``.
    """
    text = _normalize(transcript_text)
    present: set[str] = set()
    raw_lower = (transcript_text or "").lower()
    owner = (owner_email or "").lower()

    for email, name in candidates.items():
        key = (email or "").lower()
        if not key:
            continue
        if key == owner:
            present.add(key)
            continue
        if key in raw_lower:
            present.add(key)
            continue
        for variant in _name_variants(name or ""):
            if re.search(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", text):
                present.add(key)
                break
    return present


# ── Reading the recording ───────────────────────────────────────────────────

# How much of a transcript we pull into a prompt. A 2-hour Meet transcript can
# exceed 200KB; 120KB comfortably covers a 90-minute L10 including the action
# items Gemini appends at the end, and bounds what one summary can cost.
MAX_TRANSCRIPT_BYTES = 120_000


def read_transcript_text(
    source_path: str | None,
    *,
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
    gcs_client=None,
) -> str:
    """Up to ``max_bytes`` of a transcript's text from GCS; "" if unavailable.

    Never raises — a transcript we can't read is treated the same as one that
    doesn't exist (no recording ⇒ no email), which is the safe direction.
    """
    uri = source_path or ""
    if not uri.startswith("gs://"):
        return ""
    from rubik_core.tenancy.storage import assert_own_uri

    try:
        assert_own_uri(uri)
    except ValueError:
        logger.warning("[l10-summary] refused transcript outside workspace")
        return ""
    path = uri[len("gs://") :]
    if "/" not in path:
        return ""
    bucket_name, blob_name = path.split("/", 1)
    try:
        client = gcs_client
        if client is None:
            from google.cloud import storage

            from rubik_core.config import settings

            client = storage.Client(project=settings.GCP_PROJECT_ID)
        blob = client.bucket(bucket_name).blob(blob_name)
        raw = blob.download_as_bytes(start=0, end=max_bytes - 1)
        # ``utf-8`` (not ``utf-8-sig``) leaves a leading BOM as U+FEFF, which is
        # NOT whitespace to ``str.strip()`` — so an empty Google notes doc, which
        # is literally just the 3-byte BOM, reads back as one "non-blank"
        # character and passes every emptiness check downstream. Strip it here so
        # every consumer sees "" for an empty document.
        return raw.decode("utf-8", errors="ignore").lstrip("\ufeff")
    except Exception as exc:  # noqa: BLE001 — unreadable == unrecorded
        logger.warning("[l10-summary] could not read transcript %s: %s", uri, exc)
        return ""
