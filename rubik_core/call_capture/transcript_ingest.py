"""Meet transcript -> Cubie: the three writes every captured call performs.

``ingest()`` is the only way a Meet transcript enters Cubie from the Sales Call
Capture path. It performs THREE writes, in this order, for every non-suppressed
session (contract C1):

1. **``call_transcript_entries`` rows** — the CCS-local citation index. This is
   what makes ``call_extraction_items.evidence_entry_id`` resolve to a real
   stored utterance, and it is the only copy that survives: Google deletes
   conference records, transcripts AND transcript entries 30 days after the
   conference ends. Nothing persisted here is recoverable afterwards.
2. **The DAM asset** — one ``assets`` row plus one UTF-8 blob in GCS, which is
   what a transcript *is* everywhere else in Cubie. Without it Chat and RAG
   cannot answer from the call at all; the entries table is an index, not a
   document store.
3. **``upsert_participants``** — MANDATORY, and the write people forget.
   ``AgentTools.search_past_meetings`` short-circuits on an empty
   ``list_doc_ids_for_user``, so a transcript with no participants row is
   invisible in Chat *even to its own host*. It is also the single choke point
   that mirrors attendance into the relationship graph.

A ``capture_consent = "suppressed"`` session performs **none** of the three and
``ingest()`` returns 0 immediately. That is a confidential call, and the fact of
it is still visible to the cockpit through its ``call_sessions`` row — only the
content is not.

**Why this module commits.** Steps 2 and 3 go through ``DAMRegistry`` and
``SQLMeetingParticipantsRepository``, both of which open and commit their OWN
session (that is how ``transcript_sync.py`` and ``backend/routers/fathom.py``
already call them), and step 2 then waits on a Vertex RAG import that polls for
up to twenty attempts. Holding this session's write transaction open across a
multi-second network call is the shape that exhausted dev Cloud SQL's three
connections once already, and on SQLite a second connection cannot write at all
while the first holds a write transaction. So the entry rows are committed
before step 2 begins. Callers must not assume they can roll this back.

**Why "has entries" is not "is ingested".** Because write 1 commits before
writes 2 and 3 begin, a run that timed out inside the Vertex import leaves a
session with a full citation index, no corpus document and no ACL row — and a
caller that gates recovery on the presence of entries will report that call as
``already_ingested`` forever while the AE never gets a wrap-up. So every run
records WHAT IT COMPLETED against the transcript resource name, in
``CallSession.attrs[INGEST_MARK_KEY]``; :func:`needs_ingest` and
:func:`is_complete` are the only correct recovery gate.

**The fourth thing ingest records: how much of the call it actually heard.**
Auto-transcription is a DEFAULT, not a lock — a host or co-host can stop it
mid-call from the Meet UI, and the whole Meet API has three write methods, none
of which can stop or start transcription on a running conference. So Cubie can
neither prevent nor detect that while it happens, and afterwards a
host-stopped transcript is indistinguishable from a complete one: it reports
ENDED, the sweep correctly ingests it, and the AE gets a wrap-up that looks
entirely normal built from part of the call. The two clocks do disagree,
though — the last utterance offset against the conference's own start/end — so
every ingest stores that comparison in ``CallSession.attrs[COVERAGE_KEY]``
(:func:`compute_coverage`), and the wrap-up says when the model never saw the
rest. It is metadata (durations only, no content), so it is NOT gated on
``can_read_transcript``: a supervisor may see that a call was partly captured
without seeing what was said.

Sequencing and replay: ``sequence`` is a 1-based per-session counter and it is
the handle the extraction prompt cites, so it must be stable across
passes. Already-stored ``external_id``s are skipped and new rows continue from
the highest stored sequence, which makes a re-read of a still-growing transcript
append rather than renumber. The insert itself is ON CONFLICT DO NOTHING against
``uq_call_transcript_entries_source_external``, so a replay costs one statement
and writes nothing.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from rubik_core.config import AppMode, settings
from rubik_core.database.call_capture import CallTranscriptEntry
from rubik_core.database.models import User
from rubik_core.eos.l10_recording import MIN_TRANSCRIPT_CHARS

logger = logging.getLogger(__name__)

#: ``TRANSCRIPT_SOURCES`` value for everything this module writes.
MEET_SOURCE = "meet"

#: The fallback capture path. Named here only because its entries usually carry
#: NO per-utterance timestamps, which coverage has to know about.
FATHOM_SOURCE = "fathom"

#: The one ``Asset.topic`` that participant-gated meeting transcripts use. Chat's
#: meeting search and the L10 recap both filter on this exact string.
TRANSCRIPT_TOPIC = "Meeting Transcripts"

#: Asset statuses that stop a re-ingest. ``deleted`` is a human revocation
#: tombstone, not a transient state — once someone removes a transcript, no
#: sweep may put it back.
TERMINAL_ASSET_STATUSES = ("approved", "deleted")

#: Bounded pagination for the participants read, used only when the Meet client
#: exposes no paginator of its own. A conference with 1,000 participants is not
#: a sales call.
_MAX_PARTICIPANT_PAGES = 10

#: ``CallSession.attrs`` key holding what ingest ACTUALLY COMPLETED, per
#: transcript resource name. See :func:`mark_outcome`.
INGEST_MARK_KEY = "transcript_ingest"

#: ``CallSession.attrs`` key holding the coverage verdict — how much of the
#: call the stored transcript accounts for. See :func:`compute_coverage` for the
#: shape and :func:`mark_outcome` for the write.
#:
#: In ``attrs`` rather than in its own column because ``attrs`` is where this
#: table already keeps derived per-session bookkeeping (``transcript_ingest``
#: above, ``client_elapsed_ms``, ``company_name``) and one banner on one wrap-up
#: needs nothing more. NOTE: if coverage ever becomes a REPORTING dimension —
#: "how many calls were truncated last month", per-AE trends, anything that
#: filters or aggregates on it — promote it to real columns. Filtering on a JSON
#: key is how a nightly report turns into a full scan.
COVERAGE_KEY = "transcript_coverage"

#: Coverage ``reason`` codes: short, machine-readable, and deliberately carrying
#: NO transcript content — coverage is durations, and it is shown to readers who
#: are not allowed to read what was said.
#:
#: The three "unknown" codes exist because each one is a way to compute a FALSE
#: truncation, and a banner that cries wolf teaches the AE to ignore the one
#: that is real.
#: These strings are a CONTRACT with ``backend/routers/calls.py``, which serves
#: ``reason`` out of a closed ``COVERAGE_REASONS`` set and degrades anything
#: outside it to ``"unknown"`` — the fence that stops this field from ever
#: carrying a sentence of the call to a supervisor who may not read the
#: transcript. Renaming one here without adding it there silently blanks the
#: label on the wrap-up.
COVERAGE_COVERED = "covered"  # the transcript runs to the end of the call
COVERAGE_TRUNCATED = "truncated"  # BOTH conditions met — say so on the wrap-up
COVERAGE_WITHIN_RATIO = "within_ratio"  # a real gap, but proportionally most is here
COVERAGE_SHORT_GAP = "short_gap"  # under the ratio, but the missing tail is minutes
COVERAGE_CLOCK_SKEW = "clock_skew"  # an utterance past the end of the call, clamped
COVERAGE_CALL_WINDOW_UNKNOWN = "no_call_duration"  # no started_at / ended_at
COVERAGE_NO_OFFSETS = "no_offsets"  # entries, but none carries an offset
COVERAGE_FATHOM_NO_OFFSETS = "no_offsets_fathom"  # the common Fathom case

#: Outcomes that mean "there is nothing left to do for this transcript".
#: ``below_min_chars`` and ``no_entries`` are deliberately in here: they are
#: decisions, not failures, and a retry would take the same decision forever.
#: ``revoked`` is the human tombstone from :data:`TERMINAL_ASSET_STATUSES`.
DONE_OUTCOMES: frozenset[str] = frozenset(
    {"ingested", "already_approved", "revoked", "below_min_chars", "no_entries"}
)

#: The one outcome that means the call is STRANDED: the entries are committed
#: but the DAM asset / RAG import did not complete, so the transcript is in the
#: citation index and invisible to Chat, RAG and the graph.
FAILED_OUTCOME = "asset_failed"

#: How many passes may spend a Vertex import on the same transcript before the
#: sweep stops paying for it. Vertex serialises RAG imports per corpus and backs
#: off, so a permanently-failing corpus would otherwise absorb one heavy import
#: per transcript every 15 minutes for the whole 24-hour lookback. Reaching the
#: cap does NOT make the transcript look ingested — it stays "not complete", and
#: the sweep keeps reporting it in its own failure bucket.
MAX_ASSET_ATTEMPTS = 5


def outcomes(call_session) -> dict[str, dict]:
    """``{transcript_resource_name: {"outcome", "attempts", "at"}}`` for a session.

    The record of what ingest COMPLETED, which is not the same question as
    "does this session have transcript entries". The three writes at the top of
    this module happen in order and the first one commits before the other two
    start, so "entries exist" is satisfied by a run that then failed to reach
    the corpus — a call whose wrap-up an AE never gets while the sweep reports
    ``already_ingested`` and a clean ``records_failed: 0``. Anything deciding
    whether to re-attempt must read this, never the entries table.
    """
    marks = (getattr(call_session, "attrs", None) or {}).get(INGEST_MARK_KEY)
    return marks if isinstance(marks, dict) else {}


def is_complete(call_session, transcript_name: str) -> bool:
    """Did this transcript's ingest finish everything it was ever going to do?"""
    mark = outcomes(call_session).get(transcript_name) or {}
    return mark.get("outcome") in DONE_OUTCOMES


def attempts(call_session, transcript_name: str) -> int:
    """How many passes have already tried to ingest this transcript."""
    mark = outcomes(call_session).get(transcript_name) or {}
    try:
        return int(mark.get("attempts") or 0)
    except (TypeError, ValueError):  # pragma: no cover — defensive on JSON
        return 0


def needs_ingest(call_session, transcript_name: str) -> bool:
    """Should this pass spend an ingest on this transcript?

    ``False`` for a finished transcript AND for one that has burned
    :data:`MAX_ASSET_ATTEMPTS` — those two are not the same state, and callers
    must not collapse them: the second is still ``not is_complete()`` and still
    belongs in a failure bucket every pass.
    """
    if is_complete(call_session, transcript_name):
        return False
    return attempts(call_session, transcript_name) < MAX_ASSET_ATTEMPTS


def mark_outcome(
    db: Session,
    call_session,
    transcript_name: str,
    outcome: str,
    coverage: dict | None = None,
) -> dict:
    """Record what this pass achieved for one transcript, and commit it.

    Committed rather than flushed because it is the only durable evidence that
    a stranded call is stranded: a process killed after the entries commit and
    before this write leaves the session looking ingested, which is exactly the
    failure this mark exists to make visible.

    ``coverage`` rides along on the same reassignment rather than through a
    second writer: it is computed from the entries this pass has just committed,
    so writing it with the outcome means the two can never disagree about which
    pass they describe, and one commit does both. ``None`` leaves any stored
    verdict untouched — which is what the ``no_entries`` path wants, since a
    call with no transcript at all is a different, already-handled state and
    must not also carry a coverage verdict competing with it.

    ``attrs`` is REASSIGNED, never mutated — SQLAlchemy does not track in-place
    changes to a JSON column and ``attrs["x"] = y`` emits no UPDATE.
    """
    marks = dict(outcomes(call_session))
    previous = marks.get(transcript_name) or {}
    marks[transcript_name] = {
        "outcome": outcome,
        "attempts": attempts(call_session, transcript_name) + 1,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if previous.get("outcome") == FAILED_OUTCOME and outcome in DONE_OUTCOMES:
        logger.info(
            "Call %s transcript %s recovered on attempt %s (%s)",
            call_session.id,
            transcript_name,
            marks[transcript_name]["attempts"],
            outcome,
        )
    call_session.attrs = {
        **(getattr(call_session, "attrs", None) or {}),
        INGEST_MARK_KEY: marks,
        **({COVERAGE_KEY: coverage} if coverage is not None else {}),
    }
    db.commit()
    return marks[transcript_name]


def _parse_ts(value) -> datetime | None:
    """RFC3339 -> datetime, via the transcript sync's own coercion.

    Lazy import: ``transcript_sync`` pulls in both RAG adapters at module load,
    and this module is imported by the sweep on every pass.
    """
    from rubik_core.scripts.transcript_sync import _to_datetime

    return _to_datetime(value)


def _to_utc(value: datetime | None) -> datetime | None:
    """Read a naive datetime as UTC; leave an aware one alone.

    Every stored timestamp in this repo is naive UTC (there is no
    ``timezone=True`` column anywhere) while Meet returns RFC3339 with an
    offset, so offset arithmetic mixes the two on every call.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _offset_ms(ts: datetime | None, base: datetime | None) -> int | None:
    """Milliseconds from the start of the call, or ``None`` when unknowable.

    A transcript entry's ``startTime`` / ``endTime`` are absolute Timestamps,
    NOT durations — the first design's ``_to_ms(e["startTime"])`` read them as
    offsets and would have stored an epoch-sized integer in a column documented
    as "milliseconds from the start of the call". The base is the session's
    ``started_at`` when the sweep has stamped it, and the earliest utterance
    otherwise (which shifts every offset by the silent lead-in — visible as a
    call whose first line is at 0ms, harmless for citation, and the reason the
    fallback is worth naming rather than hiding).

    Clamped at zero: a negative "milliseconds from the start of the call" is not
    a value any reader could use, and it would mean the stamped start is later
    than an utterance — a bad base, not a bad entry.
    """
    ts, base = _to_utc(ts), _to_utc(base)
    if ts is None or base is None:
        return None
    delta = int((ts - base).total_seconds() * 1000)
    return max(delta, 0)


def coverage(call_session) -> dict | None:
    """The stored coverage verdict for a session, or ``None`` if never computed.

    ``None`` is the honest answer for a suppressed call (which never ingests), a
    call with no transcript, and any session ingested before coverage existed.
    Readers must render that as *unknown*, never as "fully covered" — the whole
    point of the verdict is that a partial capture looks complete.
    """
    stored = (getattr(call_session, "attrs", None) or {}).get(COVERAGE_KEY)
    return stored if isinstance(stored, dict) else None


def _entry_offset_ms(entry) -> int | None:
    """How far into the call this one utterance proves the transcript reaches.

    ``end_offset_ms`` when it exists, ``start_offset_ms`` otherwise. The
    fallback never inflates coverage (a start is at or before its own end) and
    it removes one way of computing a false gap: an entry whose ``endTime`` Meet
    never stamped would otherwise contribute nothing at all.
    """
    end = getattr(entry, "end_offset_ms", None)
    if end is not None:
        return int(end)
    start = getattr(entry, "start_offset_ms", None)
    return int(start) if start is not None else None


def compute_coverage(entries, *, started_at, ended_at) -> dict:
    """How much of the call the stored transcript accounts for.

    ``entries`` is every stored utterance for the session, in ``sequence``
    order, from EVERY source — anything with ``source``, ``start_offset_ms`` and
    ``end_offset_ms``, so both ORM rows and a narrow column query work. The
    result is the contract the router and the wrap-up read::

        {"covered_ms": int, "call_ms": int|None, "ratio": float|None,
         "gap_ms": int|None, "truncated": bool, "reason": str}

    ``truncated`` is True only when BOTH conditions hold — ratio below
    ``CCS_COVERAGE_MIN_RATIO`` **and** gap above ``CCS_COVERAGE_MIN_GAP_SECONDS``
    — never either. A four-minute call that ends forty seconds early is not a
    truncated call, and every wolf cried here is an AE learning to ignore the
    banner that is real.

    The unknown cases are the ones worth spelling out, because each is a way to
    produce a false "truncated":

    * **Fathom entries carry no per-utterance timestamps.** Taken
      naively their coverage computes as zero and EVERY Fathom call is flagged —
      by far the likeliest false positive in this feature. An untimed entry
      could have been said anywhere in the call, so a single untimed Fathom
      entry makes the whole verdict unknowable, not small.
    * **A partly-timed Meet transcript** uses the furthest known offset, not the
      last entry — entries arrive in ``sequence`` order, so this is the last
      non-null one, and taking the max additionally survives an out-of-order
      tail.
    * **No ``started_at`` / ``ended_at``** (or a non-positive span) means there
      is no call length to compare against. Unknown is reported as unknown and
      is never truncated — the same rule the commitment clock follows.
    * **Clock skew** between Meet's offsets and our own stamps can put the last
      utterance past the end of the call. Clamp: the ratio caps at 1.0 and the
      gap never goes negative.
    """
    rows = list(entries)

    call_ms: int | None = None
    start_utc, end_utc = _to_utc(started_at), _to_utc(ended_at)
    if start_utc is not None and end_utc is not None:
        span = int((end_utc - start_utc).total_seconds() * 1000)
        # A non-positive span is a broken pair of stamps, not a zero-length
        # call; treating it as a length would divide by zero or invent a gap.
        call_ms = span if span > 0 else None

    def _verdict(covered_ms: int, reason: str, **rest) -> dict:
        return {
            "covered_ms": covered_ms,
            "call_ms": call_ms,
            "ratio": None,
            "gap_ms": None,
            "truncated": False,
            "reason": reason,
            **rest,
        }

    offsets = [_entry_offset_ms(row) for row in rows]
    if any(
        getattr(row, "source", None) == FATHOM_SOURCE and offset is None
        for row, offset in zip(rows, offsets)
    ):
        return _verdict(0, COVERAGE_FATHOM_NO_OFFSETS)

    known = [offset for offset in offsets if offset is not None]
    if not known:
        return _verdict(0, COVERAGE_NO_OFFSETS)

    covered_ms = max(max(known), 0)
    if call_ms is None:
        return _verdict(covered_ms, COVERAGE_CALL_WINDOW_UNKNOWN)

    if covered_ms > call_ms:
        return _verdict(
            call_ms, COVERAGE_CLOCK_SKEW, ratio=1.0, gap_ms=0, truncated=False
        )

    ratio = round(covered_ms / call_ms, 4)
    gap_ms = call_ms - covered_ms
    truncated = (
        ratio < settings.CCS_COVERAGE_MIN_RATIO
        and gap_ms > settings.CCS_COVERAGE_MIN_GAP_SECONDS * 1000
    )
    if truncated:
        reason = COVERAGE_TRUNCATED
    elif ratio < settings.CCS_COVERAGE_MIN_RATIO:
        # Under the ratio and over nothing else: the gap floor is what kept this
        # quiet, and naming that is how the floor gets tuned from real calls.
        reason = COVERAGE_SHORT_GAP
    elif gap_ms > settings.CCS_COVERAGE_MIN_GAP_SECONDS * 1000:
        # The other half of the AND: minutes are missing, but they are a small
        # share of a long call — a two-hour call that ends in silence.
        reason = COVERAGE_WITHIN_RATIO
    else:
        reason = COVERAGE_COVERED
    return _verdict(covered_ms, reason, ratio=ratio, gap_ms=gap_ms, truncated=truncated)


def _coverage_rows(db: Session, call_session_id: str) -> list:
    """The offset columns of every stored entry for a session, in call order.

    Every source, not just :data:`MEET_SOURCE`: coverage is a question about the
    CALL, and a session that also holds Fathom entries has to be judged on all
    of them (see :func:`compute_coverage` on why one untimed Fathom entry makes
    the verdict unknowable). Three columns rather than whole ORM rows because a
    600-line transcript's text is not needed to divide two integers.
    """
    return (
        db.query(
            CallTranscriptEntry.source,
            CallTranscriptEntry.start_offset_ms,
            CallTranscriptEntry.end_offset_ms,
        )
        .filter(CallTranscriptEntry.call_session_id == call_session_id)
        .order_by(CallTranscriptEntry.sequence)
        .all()
    )


def _participant_index(client, conference_record: str | None) -> dict[str, dict]:
    """Map ``conferenceRecords/*/participants/*`` -> the participant resource.

    A transcript entry carries ``participant``, a resource name, and nothing
    else about who spoke; identity only exists on the participants list, so this
    join is the whole reason ``speaker_name`` and ``speaker_email`` can be
    populated at all.

    ``GoogleMeetClient.list_signed_in_participants`` cannot be used here: it
    returns ``{"user_id", "display_name"}`` and DROPS the resource name, which
    is the join key. Until that module grows a public ``list_participants``,
    this prefers one if it appears, then borrows the client's own paginator
    (which carries the 429 backoff and the page cap), and only then falls back
    to a bounded loop of its own. Returns ``{}`` on any failure — a call whose
    speakers cannot be identified is still worth ingesting, and
    ``participant_key`` is stored raw precisely so a wrong or missing join can
    be re-resolved later.
    """
    if not conference_record:
        return {}
    rows: list[dict] = []
    try:
        lister = getattr(client, "list_participants", None)
        if callable(lister):
            rows = list(lister(conference_record))
        else:
            rows = _list_participants_fallback(client, conference_record)
    except Exception:
        logger.exception(
            "Meet participant read failed for %s — speakers will be unnamed",
            conference_record,
        )
        return {}
    return {r["name"]: r for r in rows if isinstance(r, dict) and r.get("name")}


def _list_participants_fallback(client, conference_record: str) -> list[dict]:
    """``conferenceRecords.participants.list``, fully paginated, raw resources."""
    service = getattr(client, "service", None)
    if service is None:
        raise RuntimeError(
            f"Meet service not initialized for {getattr(client, 'user_email', '?')}"
        )
    request_fn = service.conferenceRecords().participants().list
    paginate = getattr(client, "_paginate", None)
    if callable(paginate):
        return list(paginate(request_fn, "participants", parent=conference_record))

    rows: list[dict] = []
    page_token: str | None = None
    for _ in range(_MAX_PARTICIPANT_PAGES):
        params: dict = {"parent": conference_record, "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = request_fn(**params).execute()
        rows.extend(resp.get("participants") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return rows


def _speaker(
    participant: dict | None,
    all_app_users: list[User],
    cache: dict[str, str | None],
) -> tuple[str | None, str | None]:
    """``(display_name, company_email)`` for one Meet participant resource.

    The email comes from ``transcript_sync._resolve_joiner_emails`` — the same
    mapping that seeds every transcript ACL in Cubie — which matches Meet's
    ``users/{id}`` against ``User.id`` (the Google ``sub`` stored at login) and
    falls back to a case-insensitive display-name match. Resolving one joiner at
    a time keeps the per-entry mapping that its set return would collapse, and
    the cache means a 600-line transcript resolves each speaker once.

    An external participant correctly does not resolve: they have no Cubie
    account, so there is nothing to grant and no address to invent. This is why
    ``speaker_email`` is nullable and why NO directory scope is requested for
    it — ``rubik_core/tools/directory.py`` is also the only Workspace client
    that raises from ``__init__``.
    """
    if not participant:
        return None, None
    signed_in = participant.get("signedinUser") or {}
    anonymous = participant.get("anonymousUser") or {}
    phone = participant.get("phoneUser") or {}
    display = (
        signed_in.get("displayName")
        or anonymous.get("displayName")
        or phone.get("displayName")
    )
    user_id = signed_in.get("user")
    if not user_id:
        return display, None
    if user_id not in cache:
        from rubik_core.scripts.transcript_sync import _resolve_joiner_emails

        resolved = _resolve_joiner_emails(
            [{"user_id": user_id, "display_name": display}], all_app_users
        )
        cache[user_id] = next(iter(sorted(resolved)), None)
    return display, cache[user_id]


def _stored_sequences(db: Session, call_session_id: str) -> dict[str, int]:
    """``{external_id: sequence}`` already stored for this session's Meet entries.

    Drives both halves of replay safety: which entries to skip, and where the
    sequence counter resumes so a re-read of a still-growing transcript appends
    instead of renumbering rows the extractor may already have cited.
    """
    rows = (
        db.query(CallTranscriptEntry.external_id, CallTranscriptEntry.sequence)
        .filter(
            CallTranscriptEntry.call_session_id == call_session_id,
            CallTranscriptEntry.source == MEET_SOURCE,
        )
        .all()
    )
    return {external_id: seq for external_id, seq in rows if external_id}


def _insert_ignore(db: Session, rows: list[dict]) -> int:
    """``INSERT ... ON CONFLICT DO NOTHING`` on both dialects Cubie runs on.

    Postgres in dev/prod, SQLite in CI and in every test that builds its schema
    with ``create_all()``. The conflict target is left implicit: the only unique
    keys on the table are the PK (uuid4 hex, generated here) and
    ``(call_session_id, source, external_id)``, so a bare DO NOTHING cannot
    swallow anything a named target would have caught.

    Column defaults and ``@validates`` hooks do NOT run on a Core insert, which
    is why every caller-supplied value below is already normalised —
    ``speaker_email`` in particular, because the ``lower_check`` CHECK on that
    column would reject a mixed-case address written this way.
    """
    if not rows:
        return 0
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        # No ON CONFLICT support to assume: fall back to the ORM. The pre-read
        # in ``ingest`` has already removed every known duplicate, so this only
        # loses the concurrent-sweep race, and loudly.
        for row in rows:
            db.add(CallTranscriptEntry(**row))
        db.flush()
        return len(rows)

    result = db.execute(
        _insert(CallTranscriptEntry).values(rows).on_conflict_do_nothing()
    )
    written = result.rowcount
    return len(rows) if written is None or written < 0 else written


def _transcript_text(call_session, entries: list[CallTranscriptEntry]) -> str:
    """The human-readable transcript that becomes the DAM asset / RAG document.

    Built from the STORED entries rather than the API response so the document
    and the citation index can never disagree about what was said. A short
    header gives Chat something to date and attribute the call by; the body is
    plain ``Speaker: text`` lines, without the ``[sequence]`` prefixes, because
    those exist for the extraction prompt and would only be noise in a retrieved
    chunk.
    """
    started = call_session.started_at or call_session.created_at
    header = [
        "Sales call transcript",
        f"AE: {call_session.ae_email}",
    ]
    if started:
        header.append(f"Date: {started:%Y-%m-%d %H:%M} UTC")
    if call_session.conference_record:
        header.append(f"Conference: {call_session.conference_record}")
    speakers = sorted({e.speaker_name for e in entries if e.speaker_name})
    if speakers:
        header.append(f"Speakers: {', '.join(speakers)}")

    body = [
        f"{e.speaker_name or 'Unknown speaker'}: {e.text}"
        for e in entries
        if (e.text or "").strip()
    ]
    return "\n".join(header) + "\n\n" + "\n".join(body) + "\n"


def _acl_emails(call_session, entries: list[CallTranscriptEntry]) -> list[str]:
    """Who may read this transcript: the AE plus every resolved speaker.

    Deliberately narrow. A sales call is not a team meeting, and the ACL is the
    only thing standing between one AE's client call and the rest of the
    company's Chat queries. External participants never resolve to an address,
    so they are absent by construction. Placeholder / reserved-domain addresses
    are dropped here as well as inside ``upsert_participants``, because the same
    list is written to ``Asset.allowed_emails``, which has no such filter.
    """
    from rubik_core.database.repository import is_placeholder_email

    emails = {(call_session.ae_email or "").strip().lower()}
    emails.update((e.speaker_email or "").strip().lower() for e in entries)
    return sorted(e for e in emails if e and "@" in e and not is_placeholder_email(e))


def _register_and_ingest_asset(
    call_session,
    *,
    doc_id: str,
    text: str,
    allowed_emails: list[str],
    filename: str,
    meeting_id: str,
) -> str:
    """Writes 2 and 3: the DAM asset (+ RAG ingest) and the participant ACL.

    Returns the OUTCOME, not a bare bool: ``ingested``, ``already_approved``,
    ``revoked`` (a human tombstone) or :data:`FAILED_OUTCOME`. A bool cannot
    tell "already in the corpus" from "the import failed" — both are "did not
    ingest this time" — and collapsing them is what would either strand a call
    forever or re-import an approved transcript on every pass. The asset row is
    registered BEFORE the upload with ``source_path`` already pointing at
    ``{doc_id}.txt``, so the temp file has to carry exactly that name:
    ``VertexRagAdapter.upload_to_gcs`` derives the blob name from
    ``local_path.name``, and any other name records a ``source_path`` for a blob
    that does not exist — after which ``read_transcript_text`` returns ``""``
    forever, silently.

    ``target_corpus_id`` is the meetings corpus, not the default one:
    transcripts live in ``company_meetings_corpus`` (SOC 2 H-15), and both the
    ingest and any later delete must name it or the vectors are written to, or
    orphaned in, the wrong place.
    """
    from rubik_core.dam.registry import AssetMetadata, DAMRegistry
    from rubik_core.database.repository import SQLMeetingParticipantsRepository
    from rubik_core.scripts.transcript_sync import (
        _build_processor,
        _resolve_meetings_corpus_id,
    )

    dam = DAMRegistry()
    existing = dam.get_asset(doc_id)
    if existing and existing.status in TERMINAL_ASSET_STATUSES:
        # ``approved`` = already in the corpus; ``deleted`` = a human revoked it.
        # Both are terminal. Note the consequence for a transcript that grew
        # after an earlier pass approved it: the extra utterances are in the
        # citation index but not in the corpus. The sweep only calls ingest when
        # a session has NO entries, so this is the replay case, not the norm.
        logger.info(
            "Transcript asset %s already %s — leaving the corpus alone",
            doc_id,
            existing.status,
        )
        return "already_approved" if existing.status == "approved" else "revoked"

    from rubik_core.tenancy.storage import local_data_dir, object_name

    if settings.APP_MODE == AppMode.GCP:
        source_uri = (
            f"gs://{settings.GCS_BUCKET_NAME}/{object_name('rag', f'{doc_id}.txt')}"
        )
    else:
        stored = local_data_dir() / "rag" / f"{doc_id}.txt"
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_text(text)
        source_uri = stored.as_uri()
    now = datetime.utcnow()

    with tempfile.TemporaryDirectory() as tmp_dir:
        # EXACTLY {doc_id}.txt — see the docstring. Do not add a prefix.
        tmp_path = Path(tmp_dir) / f"{doc_id}.txt"
        tmp_path.write_text(text)

        dam.register_asset(
            AssetMetadata(
                doc_id=doc_id,
                # The metadata field is ``owner``; the ORM column is
                # ``owner_email``. They are not the same name and the mismatch
                # is silent.
                owner=call_session.ae_email,
                topic=TRANSCRIPT_TOPIC,
                # No department: transcripts are participant-gated, never
                # department-scoped, and a department here would widen the ACL
                # to everyone in Sales.
                department=None,
                status="pending",
                is_company_wide=False,
                source_path=source_uri,
                allowed_emails=allowed_emails,
                original_filename=filename,
                created_at=now,
                updated_at=now,
            )
        )

        # ── ACCESS BOUNDARY ─────────────────────────────────────────────────
        # Authoritative participant ACL, and the write without which this
        # transcript is invisible in Chat even to the AE who was on the call.
        # REPLACE semantics: the full final set, every time.
        SQLMeetingParticipantsRepository().upsert_participants(
            doc_id, meeting_id=meeting_id, emails=allowed_emails
        )
        # ────────────────────────────────────────────────────────────────────

        res = _build_processor().process_file(
            tmp_path,
            manual_metadata={
                "owner": call_session.ae_email,
                "topic": TRANSCRIPT_TOPIC,
                "status": "approved",
                "is_company_wide": False,
                # The vector-store metadata filter reads this pipe-delimited
                # cache; the table above is the source of truth.
                "allowed_emails": "|" + "|".join(allowed_emails) + "|",
                "original_filename": filename,
            },
            target_corpus_id=_resolve_meetings_corpus_id(),
        )

    result = res.get("result")
    if not result or not result.success:
        dam.update_asset(doc_id, {"status": "failed"})
        logger.error(
            "Transcript ingest failed for call session %s (doc %s): %s",
            call_session.id,
            doc_id,
            result.error if result else "unknown error",
        )
        # ``failed`` is NOT in TERMINAL_ASSET_STATUSES, so the next pass
        # re-registers and re-imports this same doc_id. That is the recovery,
        # and it only happens because the caller records the outcome.
        return FAILED_OUTCOME
    dam.update_asset(doc_id, {"status": "approved"})
    # Meeting routines: queues rows only, never raises, no-op when off.
    from rubik_core.meeting_skills.routines import on_transcript_ready

    on_transcript_ready(doc_id)
    return "ingested"


def ingest(db: Session, call_session, transcript_name: str, client) -> int:
    """Persist one Meet transcript against ``call_session``. Returns rows written.

    ``client`` is a ``GoogleMeetClient`` for the AE (or any object with the same
    three read methods, which is how tests supply fixtures). ``transcript_name``
    is a ``conferenceRecords/*/transcripts/*`` resource name — a conference can
    have several when the host toggles transcription mid-call, and each is
    ingested against the same session.

    The return value counts ``call_transcript_entries`` rows only. Zero is a
    legitimate, common outcome: a suppressed call, a replay, or a transcript
    with nothing quotable in it. Writes 2 and 3 are skipped when the assembled
    text is shorter than ``MIN_TRANSCRIPT_CHARS`` — see the content gate below.
    It is therefore NOT a success signal: whether this transcript finished is
    :func:`is_complete`, recorded by :func:`mark_outcome` on every path.
    """
    if (call_session.capture_consent or "granted") == "suppressed":
        # None of the three writes. Not an error, and not a state to recover
        # from: the AE marked this call confidential on the start screen.
        logger.info(
            "Call session %s is suppressed — no transcript ingest", call_session.id
        )
        return 0
    if not transcript_name:
        raise ValueError("transcript_name is required")

    all_app_users = db.query(User).filter(User.is_platform_support.is_(False)).all()
    participants = _participant_index(client, call_session.conference_record)
    stored = _stored_sequences(db, call_session.id)
    next_seq = max(stored.values(), default=0) + 1

    raw_entries = list(client.list_transcript_entries(transcript_name))

    # Offsets are relative to the call, so a base is needed before the first row
    # is built. ``started_at`` when the sweep has stamped it; the earliest
    # utterance otherwise (see ``_offset_ms``).
    base = call_session.started_at
    if base is None:
        starts = [
            _parse_ts(r.get("startTime")) for r in raw_entries if r.get("startTime")
        ]
        starts = [s for s in starts if s]
        base = min(starts) if starts else None

    rows: list[dict] = []
    email_cache: dict[str, str | None] = {}
    replayed = empty = 0
    for position, raw in enumerate(raw_entries, start=1):
        text = (raw.get("text") or "").strip()
        if not text:
            # ``text`` is NOT NULL and an empty utterance can never be cited.
            empty += 1
            continue
        # Meet always supplies ``name``. The synthetic fallback exists because
        # SQL treats NULLs as distinct, so a NULL ``external_id`` would defeat
        # the UNIQUE constraint and a replay would duplicate the whole
        # transcript.
        external_id = raw.get("name") or f"{transcript_name}/entries/pos-{position}"
        if external_id in stored:
            replayed += 1
            continue
        participant_key = raw.get("participant")
        speaker_name, speaker_email = _speaker(
            participants.get(participant_key), all_app_users, email_cache
        )
        rows.append(
            {
                "id": uuid.uuid4().hex,
                "call_session_id": call_session.id,
                "source": MEET_SOURCE,
                "external_id": external_id,
                "participant_key": participant_key,
                "speaker_name": speaker_name,
                # Folded here because a Core insert bypasses @validates and the
                # column carries a lower-case CHECK.
                "speaker_email": speaker_email.lower() if speaker_email else None,
                "text": text,
                "language_code": raw.get("languageCode") or "en-US",
                "start_offset_ms": _offset_ms(_parse_ts(raw.get("startTime")), base),
                "end_offset_ms": _offset_ms(_parse_ts(raw.get("endTime")), base),
                "sequence": next_seq,
                "created_at": datetime.utcnow(),
            }
        )
        stored[external_id] = next_seq
        next_seq += 1

    written = _insert_ignore(db, rows)
    # Committed before writes 2 and 3 — see the module docstring on why this
    # service commits rather than leaving it to the caller.
    db.commit()
    logger.info(
        "Call %s transcript %s: %d entries written, %d replayed, %d empty",
        call_session.id,
        transcript_name,
        written,
        replayed,
        empty,
    )

    entries = (
        db.query(CallTranscriptEntry)
        .filter(
            CallTranscriptEntry.call_session_id == call_session.id,
            CallTranscriptEntry.source == MEET_SOURCE,
        )
        .order_by(CallTranscriptEntry.sequence)
        .all()
    )
    if not entries:
        # A settled transcript with nothing quotable in it. Done, not failed —
        # re-reading it every 15 minutes would find the same nothing. NO
        # coverage verdict here on purpose: this is the ``no_transcript`` state,
        # already handled and already visible, and a second "0% covered" banner
        # next to it would only compete with it.
        mark_outcome(db, call_session, transcript_name, "no_entries")
        return written

    # ── DID WE HEAR THE WHOLE CALL? ─────────────────────────────────────────
    # Computed from what is now committed, and written with the outcome below so
    # the two always describe the same pass. A host can stop transcription
    # mid-call and nothing in the Meet API reports that, so this comparison of
    # the last utterance against the conference's own clock is the only signal
    # there is — see the module docstring.
    call_coverage = compute_coverage(
        _coverage_rows(db, call_session.id),
        started_at=call_session.started_at,
        ended_at=call_session.ended_at,
    )
    if call_coverage["truncated"]:
        logger.warning(
            "Call %s transcript %s covers %.0f%% of the call — %d minute(s) of "
            "it carry no transcript (transcription was likely stopped mid-call)",
            call_session.id,
            transcript_name,
            (call_coverage["ratio"] or 0) * 100,
            round((call_coverage["gap_ms"] or 0) / 60000),
        )

    text = _transcript_text(call_session, entries)
    # ── CONTENT GATE ────────────────────────────────────────────────────────
    # The BOM strip is defensive here (this text is assembled, not exported)
    # but it stays because a bare 3-byte UTF-8 BOM is TRUTHY and survives
    # ``.strip()``, which is how five of dev's sixteen Meet transcripts were
    # ingested as empty shells, marked approved, and skipped forever after. The
    # 500-character floor is the real gate: a 40-second call has nothing worth
    # putting in the corpus, and an asset with no content is worse than no
    # asset because the terminal-status check above then blocks the retry.
    text = text.lstrip("﻿")
    if len(text.strip()) < MIN_TRANSCRIPT_CHARS:
        logger.info(
            "Call %s transcript is only %d chars — indexed for citation, not "
            "ingested into the corpus",
            call_session.id,
            len(text.strip()),
        )
        # A decision, not a failure: recorded as done so the sweep stops asking.
        mark_outcome(
            db, call_session, transcript_name, "below_min_chars", call_coverage
        )
        return written

    doc_id = hashlib.sha256(transcript_name.encode()).hexdigest()
    started = call_session.started_at or call_session.created_at or datetime.utcnow()
    filename = f"Sales call - {started:%Y-%m-%d %H%M} - {call_session.ae_email}.txt"
    try:
        outcome = _register_and_ingest_asset(
            call_session,
            doc_id=doc_id,
            text=text,
            allowed_emails=_acl_emails(call_session, entries),
            filename=filename,
            # The "meeting" identity for the ACL row. The conference record is
            # the stable one — several transcripts of one conference share it,
            # which is what makes the graph attendance edge point at the call
            # rather than at a transcription session.
            meeting_id=call_session.conference_record or transcript_name,
        )
    except Exception:  # noqa: BLE001 — recorded as stranded, then reported
        # Vertex serialises RAG imports per corpus and backs off, so a timeout
        # here is the expected shape of a bad afternoon. Swallowed rather than
        # re-raised on purpose: the entries ARE committed, so the extractor and
        # the AE's wrap-up can still run off them, and the sweep reports the
        # call in its own failure bucket and re-attempts the corpus next pass.
        # Re-raising would cost the AE the wrap-up as well as the corpus.
        outcome = FAILED_OUTCOME
        logger.exception(
            "Transcript asset write raised for call session %s (doc %s) — "
            "entries are stored, corpus/ACL are NOT; will re-attempt",
            call_session.id,
            doc_id,
        )
    mark_outcome(db, call_session, transcript_name, outcome, call_coverage)
    return written
