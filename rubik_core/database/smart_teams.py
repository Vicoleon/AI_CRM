"""Smart Teams tables — work style profiles, the interview, skills, and guides.

Persistence for `docs/smart_teams/plan.md`. The business logic that fills these
rows lives in ``rubik_core/smart_teams/``; this module is columns and
constraints only, in the mainstream Cubie style (String PKs holding uuid4 hex,
Python-side ``datetime.utcnow`` defaults, ``JSON`` payloads, ``String`` +
pipe-delimited comment instead of a native enum). The UUID PKs and
``AuditMixin`` under ``rubik_core/database/eos/`` are an EOS-local exception
that mirrors an upstream schema — Smart Teams has no upstream to mirror and
deliberately does not copy them.

**The privacy contract, which the column layout exists to serve.**
Interview transcripts and numeric scores are visible ONLY to the person they
describe and to support admins. That is why the raw material and the publishable
prose are separate tables: ``st_interviews`` (answers), ``st_uploads`` (a
report someone chose to hand us) and the per-framework payloads on
``st_profiles`` are private by construction, while ``st_documents`` holds the
only thing that ever becomes company-visible — and only after its subject sets
``approved_at`` and flips ``published``. Unpublishing is a first-class state,
not a delete: ``published`` goes back to false and ``unpublished_at`` records
when, so an admin can still see that someone completed the interview without
regaining access to their guide. ``rag_ingested`` tracks the single RAG write
this feature is allowed to make — the approved guide, never a score, never a
transcript.

**Advisory only.** Nothing in these tables may be wired to performance review,
promotion, or compensation. Personality instruments used for employment
decisions are a legal exposure; collaboration and team-staffing guidance is
not. Any future reader that joins these rows to a review or comp surface is a
bug, not a feature.

**The versioning contract.** A person's profile is append-only: a retake, or a
merged official upload, writes a NEW ``st_profiles`` row with
``version = max(version) + 1`` for that person rather than mutating the old
one. Old versions are kept because a published guide cites the exact version it
was generated from — losing them would make every guide unauditable and every
pair/team guide cache un-invalidatable. Two constraints hold the invariant:
``uq_st_profile_user_version`` makes a version number unusable twice, and
``uq_st_profile_one_current`` makes "exactly one current row per person"
enforceable in both SQLite and Postgres (see ``SmartTeamsProfile.is_current``).

**Email case.** ``users.email`` is persisted verbatim from the Google ID token
and is NOT reliably lower-cased — ``backend/auth.py`` folds case only for the
domain comparison, never for storage. Every ``user_email`` here is therefore
folded to lower case by a ``@validates`` hook, exactly as
``rubik_core/database/eos/people.py`` does for the EOS identity bridge, and for
the same reason: that hazard silently strips an EOS person of every ownership
grant. Normalizing in the ORM rather than at each call site makes the
invariant hold for write paths not yet written.
There is deliberately NO foreign key to ``users.email`` on any column here —
the two casings would not join. Reads must still fold with ``func.lower()`` so a row written around the
ORM (a seed script, a psql session) cannot re-break it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import validates

from rubik_core.database.constraints import in_check, lower_check
from rubik_core.database.models import Base

# The six framework layers a profile carries, per plan.md "Instrument design".
# Order is the canonical presentation order and the order of the provenance map.
PROFILE_FRAMEWORKS: tuple[str, ...] = (
    "disc",
    "big_five",
    "type16",
    "strengths",
    "motivators",
    "enneagram",
)

# Where a framework's payload came from. "merged" means an official upload was
# reconciled with interview answers rather than replacing them outright.
PROVENANCE_SOURCES: tuple[str, ...] = ("interview", "official_upload", "merged")

# ── Column vocabularies, enforced in the database ─────────────────────────────
# These were "String + a pipe-delimited comment" in the first cut, which is the
# mainstream Cubie style and is fine for a column only ORM writers ever touch.
# These columns are not that: a seed script, a psql fixup, or an ST-3 bulk
# update can write any string at all, and a garbage status here does not fail
# loudly — it silently removes a row from the read that matters, because every
# read filters on exactly these values. A misspelled "complete" leaves an
# interview that no resume query can find and no completion sweep can see. The
# CHECK constraints below are what make the comment true.
PROFILE_STATUSES: tuple[str, ...] = ("draft", "current", "superseded")
PROFILE_ORIGINS: tuple[str, ...] = ("interview", "official_upload", "admin_seed")
INTERVIEW_STATUSES: tuple[str, ...] = ("in_progress", "completed", "abandoned")
UPLOAD_REVIEW_STATUSES: tuple[str, ...] = (
    "pending",
    "confirmed",
    "rejected",
    "merged",
)
SKILL_SOURCES: tuple[str, ...] = ("declared", "inferred", "manager")
SKILL_LEVELS: tuple[str, ...] = ("familiar", "working", "strong", "expert")
DOCUMENT_KINDS: tuple[str, ...] = ("personal_guide", "pair_guide", "team_guide")
DOCUMENT_STATUSES: tuple[str, ...] = ("pending", "ready", "failed")


def default_provenance() -> dict[str, str]:
    """Every framework starts attributed to the in-house interview.

    The interview is the default source for everyone (plan.md Decision 1: only
    the ET has official DISC results, and the system must not assume they
    exist), so seeding the full map is more honest than leaving it empty and
    letting each reader invent its own fallback. An upload that merges in later
    rewrites just the keys it actually covers.
    """
    return {name: "interview" for name in PROFILE_FRAMEWORKS}


def subject_key_for(kind: str, subject_emails: Sequence[str]) -> str:
    """The subject half of a document's cache key, without the versions.

    Split out because "find every guide for these people, any version" is a
    real read — it is what lets the UI say "you have an older guide, regenerate
    it?" instead of pretending none was ever written — and that caller has no
    versions to supply. It used to reach ``document_cache_keys`` with an empty
    version map and discard the second return value, which only worked while a
    missing version silently became 0. Asking for what you actually want is
    cheaper than teaching every caller which half of a tuple to ignore.
    """
    emails = [(email or "").strip().lower() for email in subject_emails]
    # A team guide is a set (the same five people in any order want one
    # report); a pair guide is ordered, because "X provides / Y provides" reads
    # differently reversed. Kept identical to document_cache_keys by calling
    # this from there, so the two can never drift.
    if kind == "team_guide":
        emails = sorted(emails)
    return "|".join(emails)


def document_cache_keys(
    kind: str,
    subject_emails: Sequence[str],
    profile_versions: Mapping[str, int],
) -> tuple[str, str]:
    """Canonicalize a guide's subjects and the profile versions behind it.

    Returns ``(subject_key, profile_version_key)`` — the pair that makes
    ``st_documents`` a cache. Both are built here, together, from one ordering,
    because a uniqueness constraint over two independently-canonicalized
    strings is not a constraint at all: two writers that disagree about
    ordering or case would each happily insert "the same" document.

    Ordering follows what the document actually is. A **pair guide** is
    order-sensitive — the pair format reads "X provides / Y provides",
    so the pair asked for in the other order is a different document and must
    not be served from cache. A **team guide** is a set: the same five people
    requested in any order want the same report, so sorting is what makes the
    cache hit. A personal guide has one subject and is unaffected either way.

    ``profile_version_key`` is what makes staleness a single indexed string
    comparison rather than a JSON diff: recompute it from each subject's
    CURRENT profile version and look the document up by
    ``(kind, subject_key, profile_version_key)``. A miss means somebody's
    profile has advanced since the document was written, so it must be
    regenerated — no portable SQL-side JSON comparison required.
    """
    subject_key = subject_key_for(kind, subject_emails)
    emails = subject_key.split("|") if subject_key else []
    # Fold the CALLER's map too. Subject emails arrive folded above, but
    # ``profile_versions`` is keyed by whatever the caller holds, and what
    # callers hold is ``users.email`` verbatim from the Google token — mixed
    # case is the expected input, not an exotic one. Looking a folded address
    # up in an unfolded map silently missed every time, and the old
    # ``.get(e, 0)`` turned that miss into version 0 for everybody: the cache
    # key came out byte-identical after a retake, so a stale published guide
    # kept being served, and stayed RAG-ingested, describing what someone used
    # to say about themselves.
    versions = {
        (key or "").strip().lower(): value for key, value in profile_versions.items()
    }
    # Indexed, not ``.get`` — a subject the caller forgot to supply is a bug in
    # the caller, and fabricating version 0 for them produces a plausible key
    # that collides with a real one. Fail where the mistake was made.
    missing = [email for email in emails if email not in versions]
    if missing:
        raise KeyError(
            f"document_cache_keys: no profile version supplied for {missing!r} "
            f"(kind={kind!r}); a staleness key built without every subject's "
            "version cannot detect staleness"
        )
    version_key = "|".join(f"{email}:{int(versions[email])}" for email in emails)
    return subject_key, version_key


class SmartTeamsProfile(Base):
    """One version of one person's work style profile.

    Append-only: a retake or a merged official upload inserts a new row with
    the next ``version`` rather than editing this one, because published guides
    cite the version they were generated from (see the module docstring's
    versioning contract).

    The six framework payloads are ``JSON`` for the same reason
    ``AttendeeProfile`` keeps its research payloads there: the scoring contract
    is still moving with the item banks and rubrics, and pinning every facet
    score to a column would mean a migration per tuning pass. What must stay
    queryable — which version is live, how far along it is, and where each
    layer came from — are columns. ``type16`` is a Big Five derivation labeled
    "(derived)" everywhere it surfaces; it is not, and must never be presented
    as, an MBTI result.

    ``is_current`` holds ``True`` or ``NULL`` and NOTHING ELSE, which is what
    makes ``uq_st_profile_one_current`` mean "at most one current row per
    person": SQL uniqueness treats NULLs as distinct, so any number of
    superseded rows coexist while a second live row for the same person is
    rejected by the database rather than by whichever writer remembered to
    check. Storing ``False`` instead of ``NULL`` would silently invert that
    into "at most one SUPERSEDED row per person" and start rejecting legitimate
    retakes, so the setter below coerces falsy values to ``NULL``. ``status``
    is the semantic field a reader should render; ``is_current`` is the
    enforced projection of ``status == "current"``.
    """

    __tablename__ = "st_profiles"

    id = Column(String, primary_key=True)  # uuid4 hex
    user_email = Column(String, nullable=False, index=True)
    # Monotonic per person, starting at 1. The writer takes max(version) + 1;
    # uq_st_profile_user_version is what makes a reused number impossible when
    # two writes race (a retake finishing while an upload merges).
    version = Column(Integer, nullable=False, default=1)
    status = Column(
        String, nullable=False, default="draft"
    )  # draft | current | superseded
    # True or NULL only — never False. See the class docstring.
    is_current = Column(Boolean, nullable=True)

    # ── the private layer: raw scored payloads, person + support admins only ──
    disc = Column(JSON, nullable=True)
    big_five = Column(JSON, nullable=True)
    type16 = Column(JSON, nullable=True)  # derived from big_five, labeled so
    strengths = Column(JSON, nullable=True)
    motivators = Column(JSON, nullable=True)
    enneagram = Column(JSON, nullable=True)

    # {framework: "interview" | "official_upload" | "merged"} — one entry per
    # PROFILE_FRAMEWORKS name, so a guide can say where each layer came from.
    provenance = Column(JSON, nullable=False, default=default_provenance)

    # How this version came to exist: "interview" | "official_upload" |
    # "admin_seed". The row it came FROM points here rather than the other way
    # round (``st_interviews.profile_id``, ``st_uploads.merged_profile_id``):
    # profiles are the parent of the lineage, and pointing both ways would make
    # st_profiles and st_uploads mutually dependent, which neither
    # ``create_all()`` nor a migration can order.
    origin = Column(String, nullable=False, default="interview")

    scored_at = Column(DateTime, nullable=True)
    superseded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        # A version number is spent once per person. Append-only history is
        # worthless if two rows can claim to be version 3.
        UniqueConstraint("user_email", "version", name="uq_st_profile_user_version"),
        # "Exactly one current version per person", enforced by the NULL rule
        # described in the class docstring.
        UniqueConstraint("user_email", "is_current", name="uq_st_profile_one_current"),
        # The NULL rule, made real. Without this the invariant above is only as
        # good as the ``@validates`` hook, and the obvious way to retire a
        # profile — ``update(SmartTeamsProfile).values(is_current=False)`` on
        # the previous row, which is exactly how ST-3 will write it — bypasses
        # instance attributes entirely. A stored False inverts the unique
        # constraint into "one SUPERSEDED row per person", so the FIRST retake
        # succeeds and the SECOND raises IntegrityError, 500ing somebody who
        # just spent 25 minutes answering questions about themselves.
        CheckConstraint(
            "is_current IS NULL OR is_current",
            name="ck_st_profile_is_current_true_or_null",
        ),
        CheckConstraint(lower_check("user_email"), name="ck_st_profile_email_lower"),
        CheckConstraint(
            in_check("status", PROFILE_STATUSES), name="ck_st_profile_status"
        ),
        CheckConstraint(
            in_check("origin", PROFILE_ORIGINS), name="ck_st_profile_origin"
        ),
        Index("ix_st_profiles_user_status", "user_email", "status"),
        # The module docstring mandates reading through ``func.lower()``. On
        # Postgres that expression cannot use the plain column index, so every
        # profile read would degrade to a sequential scan as the table grows —
        # the index has to match the expression the callers actually use.
        Index("ix_st_profiles_email_lower", func.lower(user_email)),
    )

    @validates("user_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the identity key to lower case on every write.

        ``users.email`` is stored verbatim from the Google token, so a row
        written with any other casing never matches a lookup and the person
        silently has no profile — the failure mode described in the module
        docstring. Reads still use ``func.lower()``.
        """
        if value is None:
            return None
        return value.strip().lower()

    @validates("is_current")
    def _only_true_or_null(self, _key: str, value: object) -> bool | None:
        """Collapse anything falsy to ``NULL`` so the unique constraint holds.

        ``uq_st_profile_one_current`` relies on superseded rows carrying NULL.
        A writer that sets ``is_current=False`` on the row it just retired
        would turn the constraint into "one superseded version per person" and
        break the next retake, so the value is normalized here rather than
        trusted to every caller.
        """
        return True if value else None


class SmartTeamsInterview(Base):
    """One person's Work Style Interview session — resumable by design.

    The interview is ~25 minutes of quick-fire scale banks plus conversational
    segments, which is far longer than anyone completes in one sitting, so the
    session IS the state: ``section_cursor`` and ``sections_completed`` say
    where to resume, and ``answers`` accumulates the scale responses as they
    are submitted rather than at the end. A row is progress, not a submission.

    ``answers`` is ``{item_id: value}`` and is raw private material — it is
    scored into ``st_profiles`` and never rendered to anyone but its author and
    support admins.

    ``adk_session_id`` points at the ADK session backing the conversational
    segments (persisted in Cloud SQL, so a resume on a different Cloud Run
    instance still finds the conversation). Only the session id is stored: the
    app name is constant for this feature and the ADK user id is
    ``user_email``, already on the row.
    """

    __tablename__ = "st_interviews"

    id = Column(String, primary_key=True)  # uuid4 hex
    user_email = Column(String, nullable=False, index=True)
    status = Column(
        String, nullable=False, default="in_progress"
    )  # in_progress | completed | abandoned

    # Section key the person is on, e.g. "disc_forced_choice". The section
    # catalog belongs to rubik_core/smart_teams/, not to this column.
    section_cursor = Column(String, nullable=True)
    sections_completed = Column(JSON, nullable=False, default=list)  # list[str]
    answers = Column(JSON, nullable=False, default=dict)  # {item_id: value}

    adk_session_id = Column(String, nullable=True)

    # The profile version this interview was scored into, once it completes.
    # SET NULL rather than CASCADE: the record that somebody sat through the
    # interview is worth keeping even if a profile version is ever purged.
    profile_id = Column(
        String, ForeignKey("st_profiles.id", ondelete="SET NULL"), nullable=True
    )

    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Bumped on every save so an abandoned session can be nudged (plan.md
    # rollout: weekly Slack reminder to incomplete users) without guessing.
    last_activity_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        # The resume lookup: newest in_progress row for this person.
        Index("ix_st_interviews_user_status", "user_email", "status"),
        Index("ix_st_interviews_email_lower", func.lower(user_email)),
        CheckConstraint(lower_check("user_email"), name="ck_st_interview_email_lower"),
        CheckConstraint(
            in_check("status", INTERVIEW_STATUSES), name="ck_st_interview_status"
        ),
    )

    @validates("user_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the identity key to lower case — see ``SmartTeamsProfile``."""
        if value is None:
            return None
        return value.strip().lower()


class SmartTeamsUpload(Base):
    """An official assessment report the person chose to hand us.

    Parsing a report someone uploads about themselves is the one place Smart
    Teams takes data it did not author: those are their own results, supplied
    with their consent, for their own profile. It is still self-given data —
    nothing here is scraped, and no report about a colleague may be uploaded on
    their behalf.

    Two-step by construction. ``framework`` and ``parsed`` are what the Gemini
    parse *proposed*; ``review_status`` is the person's verdict on it. A row
    only reaches a profile after they confirm the mapping, at which point a new
    ``st_profiles`` version is written with that framework's provenance flipped
    to ``official_upload`` (or ``merged``) and ``merged_profile_id`` set here,
    so the audit trail runs both ways.

    ``doc_id`` is the Cubie asset id for the stored file; the file itself never
    lands in a column.
    """

    __tablename__ = "st_uploads"

    id = Column(String, primary_key=True)  # uuid4 hex
    user_email = Column(String, nullable=False, index=True)

    doc_id = Column(String, nullable=True)  # Cubie asset id for the stored file
    gcs_uri = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)

    # Detected by the parse, confirmed by the person. One of
    # PROFILE_FRAMEWORKS, or NULL when the parse could not tell.
    framework = Column(String, nullable=True)
    parsed = Column(JSON, nullable=True)  # proposed payload for that framework
    parse_error = Column(String, nullable=True)
    review_status = Column(
        String, nullable=False, default="pending"
    )  # pending | confirmed | rejected | merged

    # The profile version that absorbed this upload, once merged.
    merged_profile_id = Column(
        String, ForeignKey("st_profiles.id", ondelete="SET NULL"), nullable=True
    )

    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    parsed_at = Column(DateTime, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_st_uploads_user_review", "user_email", "review_status"),
        Index("ix_st_uploads_email_lower", func.lower(user_email)),
        CheckConstraint(lower_check("user_email"), name="ck_st_upload_email_lower"),
        CheckConstraint(
            in_check("review_status", UPLOAD_REVIEW_STATUSES),
            name="ck_st_upload_review_status",
        ),
        # NULL is the honest value when the parse could not tell which
        # instrument the PDF came from; anything non-NULL must name a real
        # framework, or the merge would write a payload under a key no reader
        # looks at and the upload would silently do nothing.
        CheckConstraint(
            in_check("framework", PROFILE_FRAMEWORKS, nullable=True),
            name="ck_st_upload_framework",
        ),
    )

    @validates("user_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the identity key to lower case — see ``SmartTeamsProfile``."""
        if value is None:
            return None
        return value.strip().lower()


class SmartTeamsSkill(Base):
    """One entry in the curated skill vocabulary the Team Builder matches on.

    A closed, curated list rather than free text: "get me someone who knows
    data privacy" only works if the same competence is not stored eleven ways.
    ``aliases`` is what lets the inference sweep and the chat parser map the
    phrasing people actually use onto the canonical entry.

    ``skill_key`` is a curated slug (``"data_privacy"``), used as the PK the way
    ``EosIngestState.kind`` is — the vocabulary is small, hand-maintained, and
    read by key everywhere, so a surrogate id would only add a join.

    Retirement is ``is_active = False``, never a delete: people's claimed
    skills point here, and dropping the row would either orphan or silently
    erase their history. ``uq_st_person_skill``'s foreign key uses RESTRICT to
    make that explicit at the database level.
    """

    __tablename__ = "st_skills_taxonomy"

    skill_key = Column(String, primary_key=True)  # curated slug, e.g. data_privacy
    name = Column(String, nullable=False)
    # Grouping for the picker and for coverage scoring, e.g. "Compliance".
    cluster = Column(String, nullable=True, index=True)
    aliases = Column(JSON, nullable=False, default=list)  # list[str]
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class SmartTeamsPersonSkill(Base):
    """One person's claim on one curated skill, and how much we trust it.

    ``confirmed`` is the load-bearing column and the reason this table exists
    separately from the taxonomy. The inference sweep reads internal signals
    only — meetings attended, docs authored — and produces
    SUGGESTIONS. An inferred row starts ``confirmed = False`` and stays there
    until its subject says yes, and the Team Builder must filter on
    ``confirmed.is_(True)``: staffing a project off an unconfirmed guess means
    telling a project lead that somebody has expertise nobody ever claimed.
    ``ix_st_person_skills_user_confirmed`` exists so that filter is cheap
    enough that nobody is tempted to skip it.

    Declared and manager-supplied rows are written ``confirmed = True`` — the
    act of declaring a skill IS the confirmation — which keeps the read side a
    single boolean predicate rather than a source-dependent expression. Reading
    "confirmed skills" as ``source != "inferred"`` would be wrong the moment a
    person confirms an inference.

    One row per (person, skill): an inference for a skill the person already
    declared updates that row's evidence instead of stacking a second card.
    """

    __tablename__ = "st_person_skills"

    id = Column(String, primary_key=True)  # uuid4 hex
    user_email = Column(String, nullable=False, index=True)
    skill_key = Column(
        String,
        ForeignKey("st_skills_taxonomy.skill_key", ondelete="RESTRICT"),
        nullable=False,
    )

    level = Column(String, nullable=True)  # familiar | working | strong | expert
    source = Column(
        String, nullable=False, default="declared"
    )  # declared | inferred | manager
    # False until the subject says yes. Inferred rows start here; declared and
    # manager rows are inserted already True. NEVER treat False as fact.
    confirmed = Column(Boolean, nullable=False, default=False)
    confirmed_at = Column(DateTime, nullable=True)

    # What the inference was standing on, so a confirm/deny prompt can show its
    # work: a doc_id or meeting id, plus the fuller citation set.
    evidence_ref = Column(String, nullable=True)
    evidence = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_email", "skill_key", name="uq_st_person_skill"),
        # The Team Builder's only skill read: this person's CONFIRMED skills.
        Index("ix_st_person_skills_user_confirmed", "user_email", "confirmed"),
        Index("ix_st_person_skills_email_lower", func.lower(user_email)),
        CheckConstraint(
            lower_check("user_email"), name="ck_st_person_skill_email_lower"
        ),
        CheckConstraint(
            in_check("source", SKILL_SOURCES), name="ck_st_person_skill_source"
        ),
        CheckConstraint(
            in_check("level", SKILL_LEVELS, nullable=True),
            name="ck_st_person_skill_level",
        ),
    )

    @validates("user_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the identity key to lower case — see ``SmartTeamsProfile``."""
        if value is None:
            return None
        return value.strip().lower()


class SmartTeamsDocument(Base):
    """A generated guide: "How to work with X", a pair guide, or a team report.

    The ONLY Smart Teams row that ever becomes company-visible, and only when
    its subject has approved it. ``approved_at`` records the consent and
    ``published`` gates the read; unpublishing clears ``published`` and stamps
    ``unpublished_at`` rather than deleting, so an admin dashboard can still
    report that the interview was completed without recovering access to the
    prose. ``rag_ingested`` marks the single RAG write this feature is
    permitted — the approved guide text, tagged for the companywide corpus.
    Scores and transcripts are never ingested, so nothing in the chat corpus
    can leak the private layer.

    **A cache, keyed on who it is about and what it was generated from.**
    ``subject_key`` and ``profile_version_key`` both come from
    ``document_cache_keys`` (see its docstring for why they are built
    together), and ``uq_st_document_cache`` makes the triple
    ``(kind, subject_key, profile_version_key)`` the cache identity. Staleness
    therefore needs no background invalidation pass: recompute the version key
    from each subject's current profile and look it up. If someone has retaken
    the interview since, the lookup misses and the guide is regenerated — which
    is exactly what we want, because a pair guide written against version 2 of
    someone's profile is describing a person who has since told us something
    different. ``profile_versions`` keeps the same information in readable form
    for rendering "generated from Sam v2, Jordan v1".

    ``subject_emails`` is ordered and holds N entries: one for a personal
    guide, two for a pair guide, and as many as the team has. No foreign key to
    ``st_profiles`` for that reason — a team guide stands on N profile versions
    and a single-column FK could only ever name one of them.
    """

    __tablename__ = "st_documents"

    id = Column(String, primary_key=True)  # uuid4 hex
    kind = Column(String, nullable=False)  # personal_guide | pair_guide | team_guide

    subject_emails = Column(JSON, nullable=False, default=list)  # ordered list[str]
    subject_key = Column(String, nullable=False)  # document_cache_keys()[0]
    profile_versions = Column(JSON, nullable=False, default=dict)  # {email: version}
    profile_version_key = Column(String, nullable=False)  # document_cache_keys()[1]

    title = Column(String, nullable=True)
    # The editable draft body. The person may revise this before approving;
    # the exported Word/PDF is rendered from it, not the other way round.
    content_md = Column(Text, nullable=True)
    doc_id = Column(String, nullable=True)  # Cubie asset id of the exported doc
    gcs_uri = Column(String, nullable=True)

    # Generation runs on cubie-worker, so a row exists before its prose does.
    status = Column(String, nullable=False, default="pending")  # pending|ready|failed
    error = Column(String, nullable=True)

    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String, nullable=True)
    published = Column(Boolean, nullable=False, default=False)
    unpublished_at = Column(DateTime, nullable=True)
    rag_ingested = Column(Boolean, nullable=False, default=False)
    rag_ingested_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "kind",
            "subject_key",
            "profile_version_key",
            name="uq_st_document_cache",
        ),
        # Find every generation for these subjects regardless of version — the
        # read behind "you have an older guide, regenerate?".
        Index("ix_st_documents_kind_subject", "kind", "subject_key"),
        CheckConstraint(in_check("kind", DOCUMENT_KINDS), name="ck_st_document_kind"),
        CheckConstraint(
            in_check("status", DOCUMENT_STATUSES), name="ck_st_document_status"
        ),
        CheckConstraint(
            lower_check("subject_key"), name="ck_st_document_subject_key_lower"
        ),
        # Publication is consent, so it may not precede the approval that
        # granted it. Without this, an ST-6 bug that flips ``published`` while
        # leaving ``approved_at`` NULL would make somebody's guide company-
        # visible without them ever having agreed to it — the one failure in
        # this feature that cannot be walked back after the fact.
        CheckConstraint(
            "NOT published OR approved_at IS NOT NULL",
            name="ck_st_document_published_needs_approval",
        ),
    )

    @validates("subject_key", "approved_by")
    def _normalize_identity(self, _key: str, value: str | None) -> str | None:
        """Fold the identity-bearing strings — see ``SmartTeamsProfile``.

        This was the one email-bearing table with no fold at all, which meant
        ``subject_key`` could disagree with the folded key
        ``document_cache_keys`` produces and turn the cache lookup into a
        permanent miss — a fresh guide generated on every request.
        """
        if value is None:
            return None
        return value.strip().lower()

    @validates("subject_emails")
    def _normalize_subject_emails(self, _key: str, value: object) -> object:
        """Fold the ordered subject list, preserving order.

        Order is load-bearing for a pair guide (the "X provides / Y provides"
        format reads differently reversed), so this folds in place rather than
        sorting.
        """
        if not isinstance(value, (list, tuple)):
            return value
        return [
            (item or "").strip().lower() if isinstance(item, str) else item
            for item in value
        ]
