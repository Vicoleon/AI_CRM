"""Call Scripts — the per-product checklist a discovery call has to surface.

Persistence for the call checklists ("call scripts"). The business logic that
fills and reads these rows lives in ``rubik_core/call_capture/scripts.py``; this
module is columns and constraints only, in the same style as its sibling
``rubik_core/database/call_capture.py``: legacy ``Column()`` declarations,
``String`` PKs holding uuid4 hex generated at the call site, Python-side
``datetime.utcnow`` defaults, ``JSON`` payloads, and ``String`` + a module-level
constant tuple + a portable ``CheckConstraint`` instead of a native enum.
``mapped_column``/``Mapped[]`` has zero hits repo-wide and is not introduced
here.

There is no ``TimestampMixin`` in this repo — the EOS-local ``AuditMixin`` is
the only mixin that exists, and it is a deliberate exception mirroring an
upstream schema. Timestamps are spelled out instead as the explicit
``created_at``/``updated_at`` pair every other Cubie table carries, so nothing
new has to be maintained.

``sa.Enum``, ``JSONB``, ``DateTime(timezone=True)``, ``sa.Uuid`` and
``postgresql_where`` are all absent on purpose. CI runs
``alembic upgrade head`` against SQLite and every test builds its schema with
``Base.metadata.create_all()`` on SQLite, so a Postgres-only type does not
merely fail in CI — it fails for every developer running the suite.

**Why a script exists at all**, because the whole feature rests on it: if
something is not asked or not covered on the call, no automation afterwards can
recover it. A perfect extractor over an incomplete conversation still produces
an incomplete HubSpot record, and the AE cannot tell, because the wrap-up looks
clean. A script is the list of FACTS a call must surface for a buildable SOW —
not a pitch and not a talk track. For a consulting engagement the shape is: is
an initial assessment needed, how many sessions, how many hours each, what rate,
what start date.

**Scripts are DATA, NOT CODE, and that is the acceptance criterion the feature
lives or dies on**: a sales lead adds a product script through a form — no
deploy, no engineering ticket. Nothing in this module or in any migration seeds
company content. The tables ship EMPTY; the script content is the sales leads'
to author and approve. A script system only an engineer can update is accurate
for one quarter and wrong forever after.

**Email case.** ``users.email`` is persisted verbatim from the Google ID token
and is NOT reliably lower-cased — ``backend/auth.py`` folds case only for the
domain comparison, never for storage. Every identity-bearing column here
(``CallScript.approved_by``, ``CompanyProductDefault.ae_email``) is folded by a
``@validates`` hook and pinned by a ``lower()`` CHECK for the writers that
bypass instance attributes (``bulk_insert_mappings``, a Core ``insert()``, a
seed script, a psql session). There is deliberately NO foreign key to
``users.email``: offboarding DELETES the users row, and "no users row" is the
load-bearing departed signal the handoff feature reads. A script approved by
someone who has since left is still an approved script, and a call that
happened against it still happened.

**Key columns are folded too, for the same class of reason.** ``product_key``
and ``fact_key`` are join keys across three subsystems — the panel groups
sections by ``product_key``, the extraction prompt is built from ``fact_key``
values, and the wrap-up matches what came back against them. A casing mismatch
there does not raise: it silently reports a fact as *not discussed* when it was
discussed, which is precisely the false alarm that teaches an AE to ignore the
gaps block.
"""

from __future__ import annotations

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
from sqlalchemy.orm import relationship, validates

from rubik_core.database.constraints import in_check, lower_check
from rubik_core.database.models import Base

# ── Column vocabularies, enforced in the database ─────────────────────────────

#: Where a script came from. ``mined`` is drafted by the machine from the
#: historic transcript corpus, ``manual`` is typed into the editor by a sales
#: lead, and ``imported`` is lifted from existing SOWs or cheat sheets. This is
#: provenance a human reads when deciding whether to approve — see
#: ``CallScript.approved_by`` for why ``mined`` in particular must never be
#: shown to an AE unsigned.
SCRIPT_SOURCES: tuple[str, ...] = ("mined", "manual", "imported")


class CallScript(Base):
    """One product's checklist, at one version — the unit a sales lead approves.

    Identified by ``product_key`` (``implementation``,
    ``onboarding_review``, …), which is the system's own stable handle for the
    product and is what every other surface joins on:
    ``call_session_scripts.product_key`` denormalizes it, the panel groups its
    sections by it, and the wrap-up's ``unscripted_products`` compares against
    it. ``hubspot_product_id`` is the join to the company's HubSpot Products
    and is NULLABLE on purpose — a complete Products set is a hard gate for
    *mining accurate scripts*, not for *storing a script*, and requiring the id
    here would mean a sales lead cannot enter the first script until a HubSpot
    project they do not control has finished.

    **Versioning is append-only.** Editing an approved script forks a new row
    with ``version = max(version) + 1`` for that ``product_key`` rather than
    mutating the approved one, and ``uq_call_scripts_product_version`` makes a
    version number unusable twice. Old versions are kept because
    ``call_session_scripts`` points a finished call at the exact script it ran
    under; mutating an approved script in place would silently rewrite the
    history of every wrap-up that cited it, turning "the AE skipped a required
    fact" into "the AE covered everything" (or the reverse) months after the
    fact, with nothing in the record to show it changed.

    **Why ``approved_by`` — not ``is_active`` — is what makes a script live.**
    This is the non-obvious one, and the rule is: machine-drafted,
    human-approved — an AI-generated script never ships as authoritative.
    Scripts are mined from the transcript corpus by a batch
    job, so ``source="mined"`` rows arrive in this table written by a machine,
    already switched on as far as any is-this-row-usable flag is concerned. The
    two columns answer different questions and collapsing them loses the one
    that matters:

    * ``is_active`` is the AUTHOR'S on/off switch — "this version is the
      current draft for this product, not a retired one". A mining job sets it
      freely; it costs nothing and means nothing about correctness.
    * ``approved_by`` is a HUMAN'S SIGNATURE — a named person read this
      checklist and says the company asks these questions. It cannot be set by a batch
      job, and it is what ``active_script_for`` requires before a script
      reaches an AE's screen.

    A reader that gates on ``is_active`` alone shows AEs a model's guess as if
    it were the company's own checklist, and does it invisibly: the panel
    renders, the questions look plausible, and the first sign of trouble is an
    SOW built on a fact nobody at the company ever agreed to ask for. Every
    script is approved by a named human, none is generated-and-shipped, and this
    column is the only thing that can evidence it. ``approved_at`` records
    when; the two are set and cleared together (a fork resets both to NULL).
    """

    __tablename__ = "call_scripts"

    id = Column(String, primary_key=True)  # uuid4 hex

    # the system's stable handle for the product. Folded and slug-shaped by the
    # ``@validates`` hook below — see the module docstring on why key casing is
    # an identity concern here and not a cosmetic one.
    product_key = Column(String, nullable=False, index=True)
    # What a human sees: "Implementation". Free text, never joined on.
    name = Column(String, nullable=False)
    # 1-based, monotonic per product_key. See the class docstring on forking.
    version = Column(Integer, nullable=False, default=1)

    # The join to the company's HubSpot Products. Nullable: the Products set
    # may be incomplete and this table must not be blocked on it.
    hubspot_product_id = Column(String, nullable=True)

    # The author's on/off switch, NOT the liveness gate. See the class
    # docstring — a script is not live until ``approved_by`` is set.
    is_active = Column(Boolean, nullable=False, default=True)

    # The human signature. NULL means "draft", whatever ``is_active`` says.
    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime, nullable=True)

    # see SCRIPT_SOURCES — provenance, and the reason approval is not optional
    source = Column(String, nullable=False, default="manual")
    # Why this script looks the way it does: which SOW template it was derived
    # from, what the approver changed. Read by a human, never parsed.
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    items = relationship(
        "ScriptItem",
        back_populates="script",
        cascade="all, delete-orphan",
        order_by="ScriptItem.sequence",
    )

    __table_args__ = (
        # Append-only versioning, enforced rather than intended: a fork that
        # reuses a version number would leave two rows competing to be "the"
        # v2 of a product, and ``active_script_for`` picks by newest version.
        UniqueConstraint(
            "product_key", "version", name="uq_call_scripts_product_version"
        ),
        # ``active_script_for``'s only query: newest approved active version for
        # one product. Composite so the filter and the ordering are one read.
        Index("ix_call_scripts_product_active", "product_key", "is_active", "version"),
        CheckConstraint(
            in_check("source", SCRIPT_SOURCES), name="ck_call_scripts_source"
        ),
        # NULL passes a CHECK (the comparison is UNKNOWN, not FALSE), so this
        # constrains signed rows without making approval mandatory.
        CheckConstraint(
            lower_check("approved_by"), name="ck_call_scripts_approved_by_lower"
        ),
        CheckConstraint(
            lower_check("product_key"), name="ck_call_scripts_product_key_lower"
        ),
    )

    @validates("approved_by")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the approver's address on every write.

        ``users.email`` is stored verbatim from the Google token, so an
        approval written with any other casing does not match the approver on a
        later read and the script looks unsigned. Reads still use
        ``func.lower()``.
        """
        if value is None:
            return None
        return value.strip().lower()

    @validates("product_key")
    def _normalize_product_key(self, _key: str, value: str | None) -> str | None:
        """Fold the product handle — it is a join key, not a label.

        ``Onboarding_Review`` and ``onboarding_review`` are the same product to every
        human and two different products to every query in this feature: the
        panel would render two sections, and the wrap-up would report the
        second as an unscripted product discussed on the call. Neither raises.
        """
        if value is None:
            return None
        return value.strip().lower()


class ScriptItem(Base):
    """One fact the call has to surface, and the question that surfaces it.

    ``prompt_text`` is what the AE reads ("How many hours per session?");
    ``fact_key`` (``hours_per_session``) is what the extractor looks for and what
    ``coverage_for`` matches the extraction against. They are separate columns
    because they change independently: a sales lead rewording a question must not
    silently retarget the extractor, and renaming a fact must not change what
    the AE is asked.

    ``hubspot_target`` is ``"object.property"`` and is NULLABLE by design: some
    facts feed the SOW rather than the CRM — hours per session and a rate are
    what a sales lead needs to build the document, and there may be no
    HubSpot property that holds them. A NULL here means "surface it in the
    wrap-up, write it nowhere", which is a complete answer, not a gap.

    There is no ``updated_at``: items are rewritten as a set when a script is
    edited, and the owning ``CallScript`` carries the timestamp that matters.
    This mirrors ``CallTranscriptEntry``, which is created-and-never-updated
    for the same reason.
    """

    __tablename__ = "call_script_items"

    id = Column(String, primary_key=True)  # uuid4 hex
    script_id = Column(
        String,
        ForeignKey("call_scripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Display order in the panel. 1-based; gaps are legal (an item removed from
    # a draft does not force a renumber).
    sequence = Column(Integer, nullable=False)

    # What the AE should ask, in their words.
    prompt_text = Column(Text, nullable=False)
    # What the extractor looks for. Folded — see the module docstring.
    fact_key = Column(String, nullable=False)
    # "object.property", e.g. "deal.hours_per_session". NULL = SOW-only fact.
    hubspot_target = Column(String, nullable=True)

    # Drives the "✗ not discussed" surface in the wrap-up. Defaults to TRUE
    # deliberately: an item somebody added to a checklist and never classified
    # is a fact they wanted, and the failure of a wrongly-optional item is
    # silent (the gap never appears) while the failure of a wrongly-required
    # one is visible and one edit away.
    required = Column(Boolean, nullable=False, default=True)
    # Context for the AE — why this matters, what a good answer sounds like.
    help_text = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    script = relationship("CallScript", back_populates="items")

    __table_args__ = (
        # One row per fact per script. Two rows sharing a ``fact_key`` would
        # make coverage ambiguous — the same extracted value would satisfy one
        # and leave the other reported as not discussed on the same screen.
        UniqueConstraint("script_id", "fact_key", name="uq_call_script_items_fact"),
        # The panel and the prompt build both read a whole script in sequence
        # order; this is that read.
        Index("ix_call_script_items_script_seq", "script_id", "sequence"),
        CheckConstraint(
            lower_check("fact_key"), name="ck_call_script_items_fact_key_lower"
        ),
    )

    @validates("fact_key")
    def _normalize_fact_key(self, _key: str, value: str | None) -> str | None:
        """Fold the extractor's handle for this fact.

        ``coverage_for`` is a pure match on ``fact_key``. A case mismatch there
        reports a fact as *not discussed* when it was discussed — a false alarm
        in the one place the feature is asking an AE to trust it.
        """
        if value is None:
            return None
        return value.strip().lower()


class CallSessionScript(Base):
    """Which scripts one call is running against — N per call, not one.

    **Why a join table rather than a ``call_sessions.call_script_id`` column.**
    A single nullable column on ``call_sessions`` encodes exactly one product
    per call, and one product per call contradicts the product rule that
    lead↔deal is 1:1: a call that surfaces three products should produce
    **three leads**, not one lead with three interests. AEs often do not split
    them, which is why an AE failing to split leads into multiple products is
    the failure this whole workstream exists to fix.

    With one column the three-product call is not merely awkward, it is
    unrepresentable: the panel can show one checklist, coverage collapses into
    a single merged percentage that is wrong for all three products, and the
    wrap-up has no way to say *"the support plan was discussed but this call
    is only scripted for implementation — split it into its own lead?"*
    That sentence is the feature. It converts the exact manual step AEs skip
    into one click, and it needs a call to be able to carry N scripts and to
    know which products those N are. Hence a row per (session, script).

    ``product_key`` is denormalized from the script on purpose. It is what the
    panel groups sections by and what ``unscripted_products`` diffs the
    extraction against, and both of those run per render; joining out to
    ``call_scripts`` for a value that cannot change for a given script id would
    buy nothing. It also keeps the record legible if a script is ever renamed.

    Rows are written by ``POST /api/calls/start`` from the products the AE
    picked on the calendar row. Nothing writes them automatically: an AE who
    picks no product gets no sections, and the wrap-up then reports every
    product discussed as unscripted, which is the honest answer.
    """

    __tablename__ = "call_session_scripts"

    id = Column(String, primary_key=True)  # uuid4 hex
    call_session_id = Column(
        String,
        ForeignKey("call_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # No ``ondelete`` — the default RESTRICT/NO ACTION is wanted here. A script
    # a call has already run against must not be deletable out from under the
    # wrap-up that cites it; the editor deactivates drafts and forks approved
    # versions instead of hard-deleting either.
    script_id = Column(String, ForeignKey("call_scripts.id"), nullable=False)
    # Denormalized from the script — see the class docstring.
    product_key = Column(String, nullable=False)

    attached_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # The attach is idempotent: an AE double-clicks Start, or two tabs both
        # post, and the second attach is rejected by the database rather than
        # producing a duplicated section in the panel.
        UniqueConstraint(
            "call_session_id",
            "script_id",
            name="uq_call_session_scripts_session_script",
        ),
        # And one SECTION per product, which the constraint above does not give
        # on its own: attaching v1 and then v2 of the same product's script are
        # two different ``script_id`` values, and the panel would render
        # "Onboarding Review" twice with two different coverage verdicts on the
        # same screen. Writers should find-or-create per (session, product_key)
        # rather than blind-insert.
        UniqueConstraint(
            "call_session_id",
            "product_key",
            name="uq_call_session_scripts_session_product",
        ),
    )

    @validates("product_key")
    def _normalize_product_key(self, _key: str, value: str | None) -> str | None:
        """Fold, so this copy cannot drift from ``CallScript.product_key``."""
        if value is None:
            return None
        return value.strip().lower()


class CompanyProductDefault(Base):
    """What this AE picked for this client last time — the self-service feel.

    The AE chooses the products on the calendar row before the call. There is
    no HubSpot Leads READ anywhere in this repo to infer it from — leads are
    write-only via ``_write_leads``, and that path is gated off pending a
    permission the workspace still has to grant — so reading the products off
    the lead is not buildable today. Remembering the pick per (AE, company)
    is the cheap substitute and it is most of the value: the weekly call with
    the same client defaults to the same products every week, and the AE only
    ever touches the control when something changes.

    Keyed by ``hubspot_company_id`` rather than by calendar event or domain
    because the company binding is what the calendar screen already resolves
    for every row, and it survives a renamed meeting, a new contact and a
    changed email domain.

    Per AE, not per company: two AEs sell different things into the same
    account, and one AE's pick is not evidence about the other's call.

    ``product_keys`` is a JSON list and is only ever read WHOLE — the row is
    found by ``ae_email`` + ``hubspot_company_id``, both real columns. Nothing
    in this repo queries inside a JSON column (no ``json_extract``, no
    ``.astext``, anywhere), because CI runs against SQLite where JSON path
    support diverges from Postgres. A future "which companies want an
    onboarding review?" read needs its own table, not a JSON predicate.

    **Reassign, never mutate.** SQLAlchemy does not track in-place mutation of
    a plain ``JSON`` list, so ``row.product_keys.append(key)`` emits no UPDATE
    and the write vanishes with no error. Always
    ``row.product_keys = [*(row.product_keys or []), key]``.
    """

    __tablename__ = "call_company_product_defaults"

    id = Column(String, primary_key=True)  # uuid4 hex
    ae_email = Column(String, nullable=False, index=True)
    hubspot_company_id = Column(String, nullable=False)
    # list[str] of product_key. Written by ``remember_products``.
    product_keys = Column(JSON, nullable=False, default=list)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        # One remembered pick per AE per client — this is an upsert target, and
        # without the constraint a second Start on the same client writes a
        # second row and ``remembered_products`` returns whichever the database
        # happened to order first.
        UniqueConstraint(
            "ae_email",
            "hubspot_company_id",
            name="uq_call_company_product_defaults_ae_company",
        ),
        CheckConstraint(
            lower_check("ae_email"),
            name="ck_call_company_product_defaults_email_lower",
        ),
        # Reads fold with ``func.lower()`` per the module docstring; on Postgres
        # that expression cannot use the plain column index, so without this the
        # lookup degrades to a sequential scan as history accumulates.
        Index("ix_call_company_product_defaults_email_lower", func.lower(ae_email)),
    )

    @validates("ae_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the identity key on every write.

        An un-folded row is not merely untidy here: the uniqueness invariant is
        keyed on ``ae_email``, so a second casing makes the same human two
        different people and the AE's remembered pick silently stops being
        found. Reads still use ``func.lower()``.
        """
        if value is None:
            return None
        return value.strip().lower()
