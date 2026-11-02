"""Sales Call Capture (CCS) tables — sessions, transcript entries, extractions, writes.

Persistence for call capture. The business logic that fills these rows lives
in ``rubik_core/call_capture/``; this module is columns and constraints only,
in the mainstream Cubie style that ``rubik_core/database/smart_teams.py``
establishes: ``String`` PKs holding uuid4 hex generated at the call site,
Python-side ``datetime.utcnow`` defaults, ``JSON`` payloads, and ``String`` +
a module-level constant tuple + a portable ``CheckConstraint`` instead of a
native enum. The ``sa.Uuid`` PKs and ``AuditMixin`` under
``rubik_core/database/eos/`` are an EOS-local exception mirroring an upstream
schema; CCS has no upstream to mirror and deliberately does not copy them.

``sa.Enum``, ``JSONB``, ``DateTime(timezone=True)`` and ``postgresql_where``
are all absent on purpose. CI runs ``alembic upgrade head`` against
``sqlite:///./test.db`` and every test builds its schema with
``Base.metadata.create_all()`` on SQLite, so a Postgres-only type or a partial
index expressed the Postgres-only way does not merely fail in CI — it fails
for every developer running the suite. Partial uniqueness is expressed with
the portable True-or-NULL projection trick instead (see ``commitments.py``,
where CCS's sibling module needs two of them).

**These tables are a citation index, not a transcript store.** A transcript in
Cubie is one ``assets`` row plus one UTF-8 blob in GCS, and every existing
consumer reads it whole. ``call_transcript_entries`` exists next to that asset,
not instead of it: it is the per-utterance index the evidence rule needs so
``call_extraction_items.evidence_entry_id`` can resolve a quote to a real
stored utterance. Ingest therefore performs THREE writes for every
non-suppressed session — these rows, the DAM asset, and
``upsert_participants`` — in that order. Skipping the third leaves the
transcript invisible in Chat even to its own host, because
``search_past_meetings`` short-circuits on an empty ``list_doc_ids_for_user``.
A session with ``capture_consent = "suppressed"`` performs none of the three.

**Email case.** ``users.email`` is persisted verbatim from the Google ID token
and is NOT reliably lower-cased — ``backend/auth.py`` folds case only for the
domain comparison, never for storage. Every identity-bearing column here is
folded by a ``@validates`` hook and pinned by a ``lower_check`` CHECK for the
writers that bypass instance attributes (``bulk_insert_mappings``, a Core
``insert()``, a seed script, a psql session). There is deliberately NO foreign
key to ``users.email`` anywhere in this module: offboarding DELETES the users
row, and "no users row" is the load-bearing departed signal that the handoff
feature reads. A call that happened does not stop having happened when its AE
leaves.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship, validates

from rubik_core.database.constraints import in_check, lower_check
from rubik_core.database.models import Base
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT

# ── Column vocabularies, enforced in the database ─────────────────────────────
# Every one of these columns is filtered on by a read that matters: the sweep
# finds work by ``status``, the wrap-up screen groups by ``kind``, the approval
# flow advances ``status``. A garbage value does not fail loudly, it silently
# removes a row from the only query that would have surfaced it — a misspelled
# "transcript-ready" leaves a session no sweep can see and no AE is ever told
# about. The CHECK constraints below are what make these tuples true for the
# writers the ORM does not mediate.

# "suppressed" is a confidential call: no entries, no asset, no participants,
# no extraction. It is a first-class state, not an error.
CAPTURE_CONSENTS: tuple[str, ...] = ("granted", "suppressed")

# "no_transcript" is NOT a failure. A call can legitimately end
# with nothing to transcribe — Meet transcription was never enabled, the space
# was joined directly, the call was 40 seconds. Conflating it with "failed"
# would fill the ops alert with normal behaviour and teach everyone to ignore
# the bucket.
SESSION_STATUSES: tuple[str, ...] = (
    "scheduled",
    "in_progress",
    "ended",
    "transcript_ready",
    "extracted",
    "wrapped",
    "no_transcript",
    "failed",
)

# "fathom" is the fallback that has to actually work: it is the entire plan if
# Meet transcription turns out to be unavailable on this Workspace edition.
# See ``CallExtractionItem.evidence_entry_id`` for the one place the citation
# rule relaxes for it.
TRANSCRIPT_SOURCES: tuple[str, ...] = ("meet", "fathom", "manual")

# The closed set of things the extractor may propose. "stage_signal" values are
# a closed enum of their own, shared with the stage-lag detector's
# SIGNAL_TO_STAGE map — if the prompt is allowed to emit free text there, every
# lookup misses, the detector produces zero exceptions, and nothing errors.
ITEM_KINDS: tuple[str, ...] = (
    "vendor_field",
    "lead",
    "task",
    "sow_request",
    "stage_signal",
    "contract_expiry",
)

# The proposal lifecycle. "superseded" is what a re-extraction does to a
# still-pending proposal; "write_failed" is terminal for the attempt but leaves
# the proposal legible, so the AE sees why nothing reached HubSpot.
ITEM_STATUSES: tuple[str, ...] = (
    "proposed",
    "accepted",
    "rejected",
    "superseded",
    "written",
    "write_failed",
)


class CallSession(Base):
    """One sales call Cubie knows about, from before it starts to after wrap-up.

    The row is created either by ``POST /api/calls/start`` or by the polling
    sweep finding a conference record with no matching session, which is the
    ``origin="adhoc"`` path. That second path is why "there was a call" is never
    invisible: a conference record exists for any space the AE owns.

    ``POST /api/calls/start`` covers two of the three origins, and which one is
    decided by whether the request carries a ``calendar_event_id``. With one
    (``origin="calendar"``, the entry point AEs actually use) Cubie creates no
    space: it resolves the space the calendar already made and arms
    transcription on that. Without one (``origin=CALL_ORIGIN_ASSISTANT``) it creates a space
    of its own. Note what is still true and what is not: creating the space is
    NOT the only way to enable transcription — ``spaces.patch`` arms a space
    Cubie did not create — but there is still no API to start transcription on a
    conference already in progress, and a client-hosted space lives in another
    tenant and cannot be patched at all. So a ``calendar`` session's arming is
    BEST-EFFORT and its verdict is recorded in ``attrs["capture_arming"]`` with
    a reason. The company binding, not the arming, is the reason this row
    exists early: ``hubspot_company_id`` resolved before the call is what lets
    the wrap-up write to the right CRM record without asking.

    ``capture_consent`` is the kill switch for a confidential call and it gates
    all three ingest writes described in the module docstring, not just the
    RAG-visible one. ``status`` is the state the sweep drives; ``wrap_opened_at``
    exists so the "wrap-up in under 90 seconds" claim can be measured rather
    than asserted, and ``nothing_here`` records the honest outcome where the AE
    reviewed the wrap-up and there was genuinely nothing to file — which must be
    distinguishable from a wrap-up nobody opened.
    """

    __tablename__ = "call_sessions"

    id = Column(String, primary_key=True)  # uuid4 hex
    # The AE, keyed the way the rest of Cubie keys people: the email, folded,
    # with no FK. See the module docstring on offboarding.
    ae_email = Column(String, nullable=False, index=True)
    # Google's opaque user id for the AE, needed to impersonate them through
    # domain-wide delegation when reading their Meet artifacts. Not an identity
    # column in the email sense, so it is not folded.
    ae_google_user_id = Column(String, nullable=True)

    # Bound BEFORE the call where Cubie started it — that binding is the whole
    # reason for launching from Cubie rather than from Meet.
    #
    # ``hubspot_company_id`` is NULLABLE, and it is the ad-hoc path that makes
    # it so: the sweep detects a call from a conference record before anything
    # has told it which company was on the other end. Requiring a company here
    # would mean either dropping ad-hoc calls (defeating the "a call is never
    # invisible" guarantee) or inventing a company id, which is worse.
    hubspot_company_id = Column(String, nullable=True, index=True)
    hubspot_contact_id = Column(String, nullable=True)
    hubspot_deal_id = Column(String, nullable=True, index=True)
    # NOTE: there is deliberately no ``hubspot_owner_id`` column here or on
    # ``commitments``. Mapping a Cubie user to their HubSpot owner id is a runtime
    # lookup over the owner directory with a TTL cache, because a stored copy goes stale
    # silently — and the failure it goes stale into is "this AE produces zero signals,
    # with no error", which is exactly the failure nobody notices.

    # "spaces/{space}"; unique (see __table_args__).
    meet_space_name = Column(String, nullable=True)
    meet_meeting_code = Column(String, nullable=True, index=True)
    meet_uri = Column(String, nullable=True)
    # "conferenceRecords/{id}". UNIQUE because it is the sweep's natural key:
    # the poller sees the same conference record on every pass until the session
    # is terminal, and this constraint is what makes re-running the sweep free
    # rather than a source of duplicate sessions.
    conference_record = Column(String, nullable=True)  # unique (see __table_args__)

    capture_consent = Column(
        String, nullable=False, default="granted"
    )  # granted | suppressed
    status = Column(
        String, nullable=False, default="scheduled", index=True
    )  # see SESSION_STATUSES
    # A cross-instance claim for billable transcript analysis. The sweep and
    # the AE retry endpoint atomically claim this row before model I/O. A dead
    # worker's claim expires, while a repeat manual click obeys its own cooldown.
    extraction_claimed_until = Column(DateTime, nullable=True)
    extraction_claim_id = Column(String(32), nullable=True)
    extraction_manual_retry_after = Column(DateTime, nullable=True)
    # Number of committed transcript entries the last successful extraction
    # read. A newer entry means the report is stale even if an ingest crashed
    # before it could move the session back to transcript_ready.
    last_extracted_entry_count = Column(Integer, nullable=True)
    # How this session came to exist: cubie | adhoc | calendar — all three are
    # live. ``calendar`` is the entry point the AE actually uses: they pick one
    # of today's meetings off ``GET /api/calls/scheduled``, so the session is
    # bound to a Google Calendar event and Cubie arms the space the calendar
    # already made rather than creating one. ``cubie`` is the create-a-space
    # path, ``adhoc`` is the sweep discovering a conference record with no
    # session. Left as a plain String with no CHECK because, unlike ``status``,
    # nothing reads it to decide whether a row is actionable — it is provenance
    # for a human looking at why a session has no Meet space.
    origin = Column(String, nullable=False, default=CALL_ORIGIN_ASSISTANT)

    # The Google Calendar event this session came from — ``event["id"]`` from a
    # ``singleEvents=True`` listing, so a recurring series contributes one id
    # PER OCCURRENCE. Never ``recurringEventId``: that is the SERIES id, and
    # keying on it would make the weekly client call a single session forever,
    # collapsing every occurrence's wrap-up into the first one's.
    #
    # **Why a real column and not an ``attrs`` key.** ``attrs`` is exactly where
    # this used to be described (see its comment below) and it cannot work:
    # nothing in this repo queries INSIDE a JSON column — no ``json_extract``,
    # no ``.astext``, anywhere — because CI runs ``alembic upgrade head`` and
    # the whole suite against SQLite, where JSON path support differs from
    # Postgres. This value is *found by*, not merely *recorded*: the calendar
    # screen looks up "do I already have a session for this event?" on every
    # render, and ``POST /api/calls/start`` needs the lookup to be a real
    # indexed read so a double-click cannot mint two sessions for one meeting.
    # A found-or-created-by key must therefore be a column.
    calendar_event_id = Column(String, nullable=True, index=True)

    # Fathom's id for the recording this session was captured from — the
    # ``origin="fathom"`` path, and the link on a Meet-captured session whose
    # Fathom recording arrived later (``call_capture/fathom_attach``).
    # A column for the same reason ``calendar_event_id`` is: the Fathom sync
    # re-lists the same recordings on every pass and finds the session BY this
    # value. UNIQUE so two syncs racing on one recording are arbitrated by the
    # database; NULL for every non-Fathom session, and NULLs are distinct.
    fathom_recording_id = Column(String, nullable=True, unique=True, index=True)

    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    wrapped_at = Column(DateTime, nullable=True)
    # When the AE first opened the wrap-up screen — the numerator of the
    # 90-second metric the spec commits to.
    wrap_opened_at = Column(DateTime, nullable=True)
    # The AE reviewed the wrap-up and there was nothing worth filing. Distinct
    # from a wrap-up that was never opened, which is what ``wrapped_at`` NULL
    # means.
    nothing_here = Column(Boolean, nullable=False, default=False)

    # Free-form provenance the sweep wants to keep without a migration per
    # field: the Meet artifact names, the arming verdict under
    # ``attrs["capture_arming"]`` (which is how the sweep tells never-armed from
    # armed-but-empty), the reason a session went to ``no_transcript``. The
    # calendar event id is NOT here — see ``calendar_event_id`` above for why
    # anything found-or-created by has to be a column.
    #
    # **Reassign, never mutate.** SQLAlchemy does not track in-place mutation of
    # a plain ``JSON`` dict, so ``session.attrs["k"] = v`` emits no UPDATE and
    # the write vanishes with no error. Always
    # ``session.attrs = {**(session.attrs or {}), "k": v}``.
    attrs = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    entries = relationship(
        "CallTranscriptEntry",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    extractions = relationship(
        "CallExtraction",
        back_populates="session",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # One session per AE per calendar occurrence. This is what makes the
        # calendar screen's Start button idempotent: the AE double-clicks, or
        # two tabs both post, and the second insert is rejected by the database
        # rather than producing a second session that splits the transcript and
        # the wrap-up between two rows. ``POST /api/calls/start`` catches the
        # IntegrityError and returns the existing session.
        #
        # Composite with ``ae_email`` rather than unique on the event id alone
        # because two people from the company are routinely on the same client call, and
        # each of them owns their own capture: whoever starts it from Cubie is
        # the AE the wrap-up belongs to.
        #
        # **Portable across SQLite and Postgres despite the NULLs**, which is
        # what makes this addable to a table that already has rows: both engines
        # treat NULL as DISTINCT in a UNIQUE, so every pre-existing
        # ``origin=CALL_ORIGIN_ASSISTANT`` / ``"adhoc"`` session (all of which have a NULL
        # ``calendar_event_id``, and always will) coexists freely — the
        # constraint only ever binds rows that actually name an event. No
        # True-or-NULL projection column is needed here, unlike the partial
        # uniqueness in ``commitments.py``, because the thing being excluded is
        # already the NULL itself.
        UniqueConstraint(
            "ae_email",
            "calendar_event_id",
            name="uq_call_sessions_ae_calendar_event",
        ),
        UniqueConstraint("meet_space_name", name="uq_call_sessions_meet_space"),
        UniqueConstraint("conference_record", name="uq_call_sessions_conf_record"),
        # The AE's own list, and the sweep's "sessions of mine still in flight".
        Index("ix_call_sessions_ae_status", "ae_email", "status"),
        # Reads fold with ``func.lower()`` per the module docstring; on Postgres
        # that expression cannot use the plain column index, so without this the
        # AE's session list degrades to a sequential scan as the table grows.
        Index("ix_call_sessions_ae_email_lower", func.lower(ae_email)),
        CheckConstraint(lower_check("ae_email"), name="ck_call_sessions_email_lower"),
        CheckConstraint(
            in_check("capture_consent", CAPTURE_CONSENTS),
            name="ck_call_sessions_consent",
        ),
        CheckConstraint(
            in_check("status", SESSION_STATUSES), name="ck_call_sessions_status"
        ),
    )

    @validates("ae_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the identity key to lower case on every write.

        ``users.email`` is stored verbatim from the Google token, so a session
        written with any other casing never matches the AE's own list and the
        call silently belongs to nobody. Reads still use ``func.lower()``.
        """
        if value is None:
            return None
        return value.strip().lower()


class CallTranscriptEntry(Base):
    """One utterance from one call — the citation index, not the transcript.

    The transcript itself is a DAM asset plus a GCS blob (module docstring).
    These rows exist so an extracted proposal can point at the exact utterance
    it came from and a human can read that quote before approving a write to
    HubSpot.

    **Why the PK is a uuid hex and the prompt never sees it.** The spec asked
    for a ``BigInteger`` autoincrement PK precisely so the extractor could cite
    a short integer, because entries are injected into the prompt as ``[8842]``
    prefixes. Two things are true at once: this repo's PKs are uuid4-hex
    ``String`` everywhere outside ``eos_*``, and a 32-character hex is both
    token-expensive across hundreds of transcript lines and easy for a model to
    corrupt into something that looks plausible. So the column keeps the repo's
    convention and the PROMPT carries the per-session integer ``sequence``
    instead (``[12] Jordan: …``); the model cites the sequence, and the validator
    translates sequence → id before persisting
    ``CallExtractionItem.evidence_entry_id``. The spec's actual guarantee — a
    citation that resolves to a real stored utterance — is preserved, and a
    cited sequence that was never supplied drops the item, because models
    hallucinate identifiers.

    ``speaker_email`` is usually NULL and that is expected, not a bug. Meet
    returns ``signedinUser.user``, a People API resource name, and resolving it
    to an address needs a directory scope none of the Meet scopes grants. Match
    on ``speaker_name`` until that scope exists; do not build a reader that
    assumes this column is populated.
    """

    __tablename__ = "call_transcript_entries"

    id = Column(String, primary_key=True)  # uuid4 hex — see the class docstring
    call_session_id = Column(
        String,
        ForeignKey("call_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source = Column(String, nullable=False)  # see TRANSCRIPT_SOURCES
    # The provider's own id for this utterance — a Meet entry resource name.
    # NULL for sources that have none, which is why the uniqueness constraint
    # below cannot be the whole replay story.
    external_id = Column(String, nullable=True)
    participant_key = Column(String, nullable=True)  # Meet participant resource
    speaker_name = Column(String, nullable=True)
    speaker_email = Column(String, nullable=True)  # see the class docstring

    text = Column(Text, nullable=False)
    language_code = Column(String, nullable=True, default="en-US")
    # Milliseconds from the start of the call. ``Integer`` rather than
    # ``BigInteger``: 2^31 ms is 24 days, so a 32-bit offset cannot overflow on
    # anything anyone would call a sales call, and ``BigInteger`` appears
    # nowhere else in the Cubie model files.
    start_offset_ms = Column(Integer, nullable=True)
    end_offset_ms = Column(Integer, nullable=True)
    # Per-session 1-based ordinal, and the handle the extraction prompt cites.
    # NOT NULL because an entry with no sequence can never be cited, which
    # silently makes it invisible to the evidence rule.
    sequence = Column(Integer, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    session = relationship("CallSession", back_populates="entries")

    __table_args__ = (
        # Replays are free: the sweep re-reads the same Meet transcript on every
        # pass until the session is terminal, and this is what makes the second
        # read a no-op instead of a duplicated transcript. Caveat worth knowing
        # before trusting it: SQL treats NULLs as distinct, so this only dedupes
        # sources that actually supply an ``external_id`` (Meet does). A Fathom
        # or manual re-ingest must delete the session's entries first, or mint a
        # deterministic synthetic external_id.
        UniqueConstraint(
            "call_session_id",
            "source",
            "external_id",
            name="uq_call_transcript_entries_source_external",
        ),
        # The prompt build and the sequence → id translation both read the whole
        # session in sequence order; this is that read.
        Index("ix_call_transcript_entries_session_seq", "call_session_id", "sequence"),
        CheckConstraint(
            in_check("source", TRANSCRIPT_SOURCES),
            name="ck_call_transcript_entries_source",
        ),
        CheckConstraint(
            lower_check("speaker_email"),
            name="ck_call_transcript_entries_email_lower",
        ),
    )

    @validates("speaker_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the speaker address when there is one — see ``CallSession``."""
        if value is None:
            return None
        return value.strip().lower()


class CallExtraction(Base):
    """One run of the extractor over one call's transcript.

    A row per run rather than a mutable "current extraction", because the
    reproducibility question is asked after the fact: an AE disputes a proposal
    weeks later and the only useful answer names the model, the prompt version
    and the schema version that produced it. ``raw_output`` keeps the model's
    unedited JSON alongside the parsed items for the same reason — when a
    validator drops an item, the dropped item is still legible here.

    ``prompt_version`` / ``schema_version`` are constants in the extraction
    package, bumped by hand when the prompt or the output contract changes.
    They are NOT NULL because an extraction whose provenance is unknown cannot
    be compared against any other extraction, which makes tuning guesswork.
    """

    __tablename__ = "call_extractions"

    id = Column(String, primary_key=True)  # uuid4 hex
    call_session_id = Column(
        String,
        ForeignKey("call_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Pinned model id, e.g. "gemini-2.5-pro". Never a "-latest" alias: the alias
    # hides which family answered, and comparing two runs across a silent family
    # change is worse than having no comparison.
    model = Column(String, nullable=False)
    prompt_version = Column(String, nullable=False)
    schema_version = Column(String, nullable=False)
    raw_output = Column(JSON, nullable=False)
    latency_ms = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    session = relationship("CallSession", back_populates="extractions")
    items = relationship(
        "CallExtractionItem",
        back_populates="extraction",
        cascade="all, delete-orphan",
    )


class CallExtractionItem(Base):
    """One proposed HubSpot change, with the quote that justifies it.

    Nothing here is ever applied automatically. A row reaches HubSpot only when
    its AE approves that specific item, and the write then goes through
    ``rubik_core/retrieval/hubspot_write.py``, which re-enforces the pipeline,
    owner and property gates independently of whatever produced the row. This
    table is the third of four layers — prompt, Python validators, human
    approval, write-layer re-gate — and no layer is allowed to be the last.

    ``current_value`` is read LIVE from HubSpot at proposal time, not copied
    from anything the transcript claimed, so the diff the AE approves is
    grounded in fetched state. ``is_overwrite`` is what drives the ⚠ change UI:
    blind-overwriting a non-null HubSpot value is the failure mode the feature
    forbids, and this column is how the UI can refuse to hide it.

    ``needs_new_option`` holds a value that was clearly heard but is not a valid
    option on the target property. Storing it beats dropping it: it is the
    backlog of vocabulary the CRM is missing.
    """

    __tablename__ = "call_extraction_items"

    id = Column(String, primary_key=True)  # uuid4 hex
    extraction_id = Column(
        String,
        ForeignKey("call_extractions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized from the extraction so "everything proposed for this call"
    # is one indexed read instead of a join — that is the wrap-up screen's only
    # query, and it runs while an AE is waiting. Deliberately NOT given a
    # ``relationship`` back to ``CallSession``: a child with two delete-orphan
    # parents is ambiguous about who owns its lifecycle, and the extraction is
    # the real parent. The FK still cascades so a purged session leaves nothing
    # behind.
    call_session_id = Column(
        String,
        ForeignKey("call_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    kind = Column(String, nullable=False)  # see ITEM_KINDS
    target_object = Column(String, nullable=True)  # companies | leads | tasks | deals
    target_id = Column(String, nullable=True)  # HubSpot record id; NULL for creates
    target_property = Column(String, nullable=True)  # e.g. "industry"

    proposed_value = Column(Text, nullable=True)
    current_value = Column(Text, nullable=True)  # read live at proposal time
    payload = Column(JSON, nullable=True)  # create-shaped items (lead, task)

    # NOT NULL, and that is the point. The citation rule — every proposed write
    # carries the words that justify it — is enforced by the database so no
    # future code path can bypass it. A validator can be edited; a NOT NULL
    # column has to be migrated, in public, with a reason.
    evidence_text = Column(Text, nullable=False)
    # NULLABLE, and nullable for exactly one reason: Fathom transcripts have no
    # per-utterance identifiers. The validator therefore drops an
    # item only when a NON-NULL citation fails to resolve, and keeps a null
    # citation when the transcript source is ``fathom``. A validator that
    # rejects nulls unconditionally silently empties the entire Fathom
    # fallback — the fallback that exists precisely for the case where Meet
    # transcription is unavailable — and produces an empty wrap-up with no
    # error.
    #
    # SET NULL rather than CASCADE on delete: a confidentiality revocation that
    # purges a transcript must not also erase the record of what a human
    # approved. ``evidence_text`` is NOT NULL and survives, which is the half of
    # the citation that a person can actually read. Note this makes "NULL means
    # Fathom" a WRITE-time rule the validator enforces, not a stored invariant.
    evidence_entry_id = Column(
        String,
        ForeignKey("call_transcript_entries.id", ondelete="SET NULL"),
        nullable=True,
    )
    needs_new_option = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)

    is_overwrite = Column(Boolean, nullable=False, default=False)
    status = Column(
        String, nullable=False, default="proposed", index=True
    )  # see ITEM_STATUSES
    decided_by = Column(String, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    # What the AE changed it to before approving. Kept separately from
    # ``proposed_value`` so the model's accuracy stays measurable: an edited
    # approval is a partial miss, and overwriting the proposal would make every
    # miss look like a hit.
    edited_value = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    extraction = relationship("CallExtraction", back_populates="items")

    __table_args__ = (
        # The wrap-up screen: this call's still-undecided proposals.
        Index("ix_call_extraction_items_session_status", "call_session_id", "status"),
        CheckConstraint(
            in_check("kind", ITEM_KINDS), name="ck_call_extraction_items_kind"
        ),
        CheckConstraint(
            in_check("status", ITEM_STATUSES), name="ck_call_extraction_items_status"
        ),
        CheckConstraint(
            lower_check("decided_by"), name="ck_call_extraction_items_decided_lower"
        ),
    )

    @validates("decided_by")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the approver's address — see ``CallSession``."""
        if value is None:
            return None
        return value.strip().lower()


class CallHubspotWrite(Base):
    """The ledger of every HubSpot mutation this feature attempted.

    **``idempotency_key`` is what makes "approve twice writes once" true.**
    HubSpot has no idempotency keys of its own — the sanctioned writer probes,
    then creates, and reports which happened — so there is nothing on the far
    side to deduplicate a double submit. This UNIQUE column is the only
    mechanism: the caller derives a deterministic key from the item and the
    intended mutation, inserts this row, and lets the constraint reject the
    second attempt. No double submit ever writes twice, and that is a database
    guarantee rather than a UI one because the double submit that matters is
    the one where the AE's first click appeared to do nothing.

    Both foreign keys are SET NULL on delete, unlike the rest of this module.
    A row here records a mutation that really happened in a system Cubie does
    not own; purging the local call must not destroy the evidence that we wrote
    to a client's CRM record. ``hubspot_object_id`` keeps the write findable
    afterwards even with no local context left.
    """

    __tablename__ = "call_hubspot_writes"

    id = Column(String, primary_key=True)  # uuid4 hex
    extraction_item_id = Column(
        String,
        ForeignKey("call_extraction_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    call_session_id = Column(
        String,
        ForeignKey("call_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    idempotency_key = Column(String, nullable=False)  # unique (see __table_args__)
    method = Column(String, nullable=False)  # POST | PATCH
    path = Column(String, nullable=False)  # v3 path, e.g. /crm/v3/objects/tasks
    request_body = Column(JSON, nullable=True)
    response_status = Column(Integer, nullable=True)
    # Kept whole because the useful part of a HubSpot failure is usually a
    # nested validation message, and summarizing it at write time throws away
    # the only copy. Never rendered to a user verbatim.
    response_body = Column(JSON, nullable=True)
    hubspot_object_id = Column(String, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    # Stamped only once HubSpot has confirmed. A row with NULL here and a
    # non-null ``response_status`` is a failed attempt, which is exactly what
    # the retry sweep looks for.
    succeeded_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_call_hubspot_writes_idempotency"),
    )


class CallEventSubscription(Base):
    """Workspace Events push subscriptions — **UNUSED in v1, by decision.**

    v1 detects calls by POLLING ``conferenceRecords.list`` on Cloud Scheduler,
    not by push. The reasons are worth restating here,
    because this table is the thing a future reader will find and assume is
    load-bearing:

    - There are zero Pub/Sub push receivers in this repo. Push would mean
      writing a service-account OIDC verifier from scratch, with no precedent
      to copy, while every existing scheduled endpoint authenticates on a
      shared-secret header.
    - ``rubik_core/scripts/transcript_sync.py`` already sweeps Meet
      transcripts every 15 minutes. Polling extends an existing slot instead of
      adding a parallel pipeline.
    - Push buys latency, and latency is the one thing this workflow does not
      need: the wrap-up is a next-morning task, so seconds versus 15 minutes
      costs nothing real.
    - Most importantly it deletes the most fragile moving part in the design.
      A subscription has a 7-day TTL, and a single missed renewal job means
      Google deletes the subscription PERMANENTLY — a silent, unrecoverable
      loss of detection for that AE.

    The table ships written and unwired as the documented Phase-2 upgrade path,
    behind ``CCS_EVENT_PUSH_ENABLED`` (default false), so that turning push on
    later is a wiring change rather than a migration. ``expire_time`` is
    indexed because the renewal sweep's only query is "subscriptions expiring
    inside the window"; if this table is ever activated, that sweep failing
    quietly is the failure to alarm on.
    """

    __tablename__ = "call_event_subscriptions"

    id = Column(String, primary_key=True)  # uuid4 hex
    # One live subscription per Google user — the constraint that stops a
    # renewal race from leaving two subscriptions delivering duplicate events.
    google_user_id = Column(String, nullable=False)  # unique (see __table_args__)
    ae_email = Column(String, nullable=False, index=True)
    subscription_name = Column(String, nullable=False)  # "subscriptions/{id}"
    event_types = Column(JSON, nullable=False, default=list)  # list[str]
    expire_time = Column(DateTime, nullable=False, index=True)
    # active | expired | reactivating | deleted — provenance for a human
    # debugging a gap in detection, not a value any read filters on.
    state = Column(String, nullable=False, default="active")
    last_renewed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "google_user_id", name="uq_call_event_subscriptions_google_user"
        ),
        CheckConstraint(
            lower_check("ae_email"), name="ck_call_event_subscriptions_email_lower"
        ),
    )

    @validates("ae_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the identity key to lower case — see ``CallSession``."""
        if value is None:
            return None
        return value.strip().lower()
