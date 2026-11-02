"""Call Scripts — the authoring API that keeps scripts DATA and not code.

The HTTP half of the call checklists. ``rubik_core/database/call_scripts.py``
is the columns, ``rubik_core/call_capture/scripts.py`` is the engine, and this
module is the six endpoints the delivery supervisor and the product owners use
to put a product's checklist in front of an AE.

**This router is the feature's acceptance criterion**: scripts are not
hard-coded. A product owner chooses a product, enters the minimum information
a call has to surface, and serves themselves.

That is not a convenience. A script system only an engineer can update is
accurate for one quarter and wrong forever after: every SOW template change
becomes an engineering ticket, which is precisely the pattern this feature
exists to break. So the tables ship EMPTY — nothing here or in the migration
seeds company content — and every product key, question and fact key in the system
arrives through these endpoints from a human who owns the product.

Four rules shape the whole file, and each of them is here because breaking it
fails *quietly*:

1. **Editing an APPROVED script forks a new version; it never mutates one.**
   ``call_session_scripts`` points a finished call at the exact script it ran
   under, and ``coverage_for`` recomputes the gap list from that script's items
   every time a wrap-up is opened. Mutating an approved script in place would
   silently rewrite the history of every wrap-up citing it — turning "the AE
   skipped a required fact" into "the AE covered everything", or the reverse,
   months later, with nothing in the record to show it changed. Editing a
   DRAFT edits in place: nothing has run against it.

2. **``POST /{id}/approve`` is the only thing that makes a script live, and it
   stamps ``approved_by`` from the authenticated user — never from the body.**
   Scripts are *machine-drafted, human-approved*: an AI-generated script must
   not ship as authoritative. Scripts are
   mined from the transcript corpus by a batch job, so ``source="mined"`` rows
   arrive written by a machine with ``is_active`` already true. ``approved_by``
   is the one column that can evidence a named human read the checklist, and an
   endpoint that accepted it as input would let a caller forge that signature.

3. **Nothing is ever hard-deleted.** ``call_session_scripts.script_id`` carries
   no ``ondelete``, on purpose: a script a call has already run against must not
   vanish out from under the wrap-up that cites it. ``DELETE`` retires a script
   by clearing ``is_active``, which removes it from ``active_script_for`` — so
   it stops reaching AEs — and leaves every past call intact and explicable.

4. **Reads show LIVE scripts only, unless the caller may author.** A draft in
   front of an AE is rule 2 defeated by a different door: the panel renders,
   the questions look plausible, and the AE has no way to tell a model's guess
   from the company's own checklist.

**Gates.** Every endpoint carries ``Depends(require_ccs)`` (403 — WHO) and
in-body ``_require_ccs_enabled()`` (404 — WHETHER), in that order, exactly like
``backend/routers/calls.py``: someone outside the audience gets the same 403
whether the feature is on or off and cannot use the response to discover that
an unreleased surface exists. The flag check is IMPORTED from ``calls.py``
rather than copied so there is one definition of "is Call Capture on" and one
404 message — ``backend/tests/test_sales_kaizen_access.py`` asserts on it.

The authoring gate is a THIRD, narrower check on the four mutating endpoints,
and it deliberately reuses an existing predicate rather than inventing a role:
``auth.user_can_access_cockpit_supervisor`` is the supervisor's gate — the
delivery team, who deliver the SOWs these checklists exist to make buildable,
plus admins. It is applied as a predicate rather than by declaring
``require_cockpit_supervisor`` because that dependency also enforces
``COCKPIT_ENABLED``, and script authoring is a Call Capture surface: coupling
it to the cockpit's flag would 404 the editor whenever the cockpit is off,
which is a rollout footgun and not a permission. The supervisor roster ships
with admins only (Call Capture's ships open to everyone), so authoring starts
with admins and widens when an admin adds the delivery team to the Nudge
Worklist row in Admin → Feature Access;
``backend/tests/test_call_scripts_router.py`` pins both rosters.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.audit import record_audit
from backend.auth import require_ccs, user_can_access_cockpit_supervisor

# One definition of "is Call Capture switched on", and one 404 message — see
# the module docstring. `calls.py` owns it because it is that router's flag.
from backend.routers.calls import _require_ccs_enabled
from rubik_core.call_capture.scripts import active_script_for
from rubik_core.database.call_scripts import CallScript, ScriptItem
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scripts", tags=["call-scripts"])


# ── Constants ────────────────────────────────────────────────────────────────

# One product's checklist. A typical product's checklist is five facts; a
# "checklist" of fifty is a form, and an AE reading a form during a client call
# reads none of it. The cap is a shape statement, enforced by pydantic so an
# oversized body is rejected before any row is touched.
_MAX_ITEMS = 50

# One page of the editor. Versions accumulate slowly — a script forks on an
# approved-edit, not on every keystroke — so this is generous for the whole
# catalogue and still bounds the response.
_MAX_SCRIPTS = 200

# Shape of a join key authored through this router. Stricter than the engine's
# `_PRODUCT_KEY_RE`, which has to tolerate what a language model emits: keys
# TYPED BY A HUMAN have no reason to carry spaces, and a `product_key` of
# "onboarding review" would never match the folded, slug-shaped keys the rest of
# the feature joins on. Rejecting it at the door is the only place the mistake
# is visible — everywhere downstream it silently renders an empty panel or
# reports a discussed fact as missing.
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# "object.property", or a bare property. Deliberately permissive about the
# object half: `coverage_for` discards it (HubSpot's object names are written
# both ways — `company` and `companies` — and matching on them would report a
# covered fact as missing), so validating it here would refuse authoring that
# works perfectly well.
_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")

# Fields whose change is CONTENT — the reason rule 1 exists. Any of these on an
# approved script forks a new version. `is_active` is deliberately not among
# them: the model layer calls it "the AUTHOR'S on/off switch", it changes
# nothing a past call was measured against, and forking on a retire/restore
# would produce an unapproved copy that cannot be live and therefore cannot
# restore anything.
_CONTENT_FIELDS = frozenset({"name", "hubspot_product_id", "notes", "items"})

# Names where the roster lives rather than a team, so it stays true whichever
# team an admin adds. The client keys on the opening words (api/scripts.ts).
_AUTHOR_DENIED = (
    "Authoring call scripts is limited to the team that owns them — the "
    "delivery team your admin has added to the Nudge Worklist in Feature Access."
)


# ── Schemas ──────────────────────────────────────────────────────────────────


class ScriptItemIn(BaseModel):
    """One fact the call has to surface, and the question that surfaces it.

    ``prompt_text`` is what the AE reads; ``fact_key`` is what the extractor
    and ``coverage_for`` match on. Two fields, not one, because they change
    independently — a supervisor rewording a question must not silently retarget the
    extractor, and renaming a fact must not change what the AE is asked.

    No ``sequence``: the order of this list IS the order the panel renders, and
    a client-supplied sequence is one more way for two items to collide or for
    a gap to appear in a checklist a human is reading top to bottom.

    ``required`` defaults to TRUE, matching the column, and the asymmetry is the
    point: a wrongly-required fact shows a gap the AE dismisses in a second,
    while a wrongly-optional one never shows a gap at all — which is the exact
    failure the checklist was added to prevent.
    """

    fact_key: str = Field(min_length=1, max_length=64)
    prompt_text: str = Field(min_length=1, max_length=1000)
    hubspot_target: str | None = Field(default=None, max_length=128)
    required: bool = True
    help_text: str | None = Field(default=None, max_length=1000)


class ScriptCreate(BaseModel):
    """A new DRAFT. ``source`` is not accepted — see :func:`create_script`."""

    product_key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    hubspot_product_id: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=4000)
    items: list[ScriptItemIn] = Field(default_factory=list, max_length=_MAX_ITEMS)


class ScriptUpdate(BaseModel):
    """An edit. Every field optional; OMITTED and NULL are different.

    ``model_fields_set`` is what separates them, and it has to be: ``notes:
    None`` is "clear the note" while an absent ``notes`` is "leave it alone",
    and collapsing the two would let a UI that posts a partial form wipe fields
    the author never opened. Same for ``items`` — omitted keeps the current
    checklist, ``[]`` empties it (legal on a draft that is still being built,
    and refused at approval).
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    hubspot_product_id: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=4000)
    is_active: bool | None = None
    items: list[ScriptItemIn] | None = Field(default=None, max_length=_MAX_ITEMS)


# ── Gates and small helpers ──────────────────────────────────────────────────


def _require_author(user: UserProfile) -> str:
    """403 unless the caller may author scripts; returns their folded address.

    Reuses ``user_can_access_cockpit_supervisor`` — the supervisor's existing gate —
    as a predicate rather than declaring ``require_cockpit_supervisor``, which
    would drag ``COCKPIT_ENABLED`` onto a Call Capture surface. See the module
    docstring.

    Returns the address that will be stamped into ``approved_by`` and into the
    audit trail, folded here because ``users.email`` is stored verbatim from the
    Google ID token and an unfolded signature does not match its owner on a
    later read — the script then looks unsigned.
    """
    if not user_can_access_cockpit_supervisor(user):
        raise HTTPException(status_code=403, detail=_AUTHOR_DENIED)
    email = _fold(getattr(user, "email", None))
    if not email:
        raise HTTPException(status_code=403, detail="no authenticated identity")
    return email


def _fold(value: str | None) -> str:
    """Mirror of ``call_capture/scripts.py::_fold`` — strip and lower, nothing else."""
    return (value or "").strip().lower()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _key(value: str | None, *, field: str) -> str:
    """Fold and shape-check a join key, or 400 with the value that failed.

    The 400 names the offending string on purpose. A key rejected here is a
    typo an author can fix in five seconds; the same key ACCEPTED is a section
    that never renders or a fact reported as not discussed when it was
    discussed, and neither of those ever mentions the key.
    """
    key = _fold(value)
    if not _KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field} must be a lowercase slug — letters, digits, "
                f"underscores and hyphens, no spaces (got {value!r})."
            ),
        )
    return key


def _target(value: str | None) -> str | None:
    """Fold a ``"object.property"`` HubSpot target, or ``None``.

    NULL is a complete answer, not a gap: some facts feed the SOW rather than
    the CRM — hours per session, a rate — and there may be no HubSpot
    property that holds them. "Surface it in the wrap-up, write it nowhere" is
    what a missing target means.
    """
    target = _fold(value)
    if not target:
        return None
    if not _TARGET_RE.match(target):
        raise HTTPException(
            status_code=400,
            detail=(
                "hubspot_target must look like 'object.property' or 'property' "
                f"(got {value!r})."
            ),
        )
    return target


def _clean_items(items: list[ScriptItemIn]) -> list[dict]:
    """Validate and number a checklist, or 400 naming the duplicate.

    The ``UNIQUE(script_id, fact_key)`` constraint would catch a repeated fact
    key as an IntegrityError, but a 500-shaped failure tells the author
    nothing. It also matters more than a tidy message: two rows sharing a fact
    key make coverage ambiguous — the same extracted value satisfies one and
    leaves the other reported as not discussed, on the same screen.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        fact_key = _key(item.fact_key, field="fact_key")
        if fact_key in seen:
            raise HTTPException(
                status_code=400,
                detail=f"fact_key {fact_key!r} appears twice in this script.",
            )
        seen.add(fact_key)
        prompt = (item.prompt_text or "").strip()
        if not prompt:
            raise HTTPException(
                status_code=400,
                detail=f"{fact_key!r} needs a question the AE can read out loud.",
            )
        help_text = (item.help_text or "").strip() or None
        out.append(
            {
                "sequence": index,
                "fact_key": fact_key,
                "prompt_text": prompt,
                "hubspot_target": _target(item.hubspot_target),
                "required": bool(item.required),
                "help_text": help_text,
            }
        )
    return out


def _get_script(db: Session, script_id: str) -> CallScript:
    row = (
        db.execute(select(CallScript).where(CallScript.id == (script_id or "").strip()))
        .scalars()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such call script.")
    return row


def _next_version(db: Session, product_key: str) -> int:
    """``max(version) + 1`` for this product. Append-only, never reused.

    Racy by construction — two authors forking the same script at the same
    moment both read the same maximum — which is why every writer that uses
    this number commits inside :func:`_commit_or_conflict`. The UNIQUE
    constraint is the actual arbiter; this is the fast path.
    """
    current = db.execute(
        select(func.max(CallScript.version)).where(
            CallScript.product_key == product_key
        )
    ).scalar()
    return int(current or 0) + 1


def _version_conflict(db: Session) -> HTTPException:
    """Roll back a lost version race and build the 409 the author can act on.

    ``uq_call_scripts_product_version`` is what stops two rows competing to be
    the same version of a product — ``active_script_for`` picks by newest
    version, so a duplicate would make "the live script" a coin toss. A retry
    reads a new maximum and succeeds.
    """
    db.rollback()
    logger.warning("[ccs] call script version race", exc_info=True)
    return HTTPException(
        status_code=409,
        detail=(
            "Someone else saved a new version of this script a moment ago. "
            "Reload and apply your change again."
        ),
    )


def _flush_or_conflict(db: Session) -> None:
    """Flush a new ``call_scripts`` row. The constraint fires HERE, not at commit.

    Worth its own helper rather than folding into the commit: the INSERT that
    loses the race is emitted by this flush — the script row has to exist before
    its items can reference it — so a handler that only guarded the commit would
    turn a routine collision into a 500.
    """
    try:
        db.flush()
    except IntegrityError:
        raise _version_conflict(db) from None


def _commit_or_conflict(db: Session) -> None:
    """Commit, turning a lost version race into a 409. See :func:`_version_conflict`."""
    try:
        db.commit()
    except IntegrityError:
        raise _version_conflict(db) from None


def _replace_items(db: Session, script: CallScript, items: list[dict]) -> None:
    """Rewrite a script's checklist as a set.

    Deleted and flushed BEFORE the inserts, in two separate flushes.
    SQLAlchemy's unit of work emits a mapper's INSERTs before its DELETEs within
    one flush, so reusing a fact key that already exists on this script —
    reordering two items, the most ordinary edit there is — would trip
    ``UNIQUE(script_id, fact_key)`` against rows that are about to disappear.

    The collection is expired either side because the new rows are added by
    ``script_id`` rather than through the relationship; without it ``_script_out``
    would serialize the items this function just deleted.
    """
    for old in list(script.items or []):
        db.delete(old)
    db.flush()
    db.expire(script, ["items"])
    for item in items:
        db.add(ScriptItem(id=uuid.uuid4().hex, script_id=script.id, **item))
    db.flush()
    db.expire(script, ["items"])


def _item_out(item: ScriptItem) -> dict:
    return {
        "item_id": item.id,
        "sequence": item.sequence,
        "fact_key": item.fact_key,
        "prompt_text": item.prompt_text,
        "hubspot_target": item.hubspot_target,
        "required": bool(item.required),
        "help_text": item.help_text,
    }


def _script_out(script: CallScript, *, is_live: bool, items: bool = True) -> dict:
    """One script, flat snake_case, with the two states kept apart.

    ``is_active`` is the author's switch and ``approved_by`` is a human's
    signature; ``is_live`` is the derived answer to the only question an AE's
    screen asks — is this the newest approved, active version of this product's
    script. Collapsing them in the response would push the reader back to
    guessing which column means "live", which is the confusion the model layer
    exists to prevent.
    """
    rows = sorted(script.items or [], key=lambda i: i.sequence or 0)
    out = {
        "script_id": script.id,
        "product_key": script.product_key,
        "name": script.name,
        "version": script.version,
        "hubspot_product_id": script.hubspot_product_id,
        "is_active": bool(script.is_active),
        "is_live": is_live,
        "approved_by": script.approved_by,
        "approved_at": _iso(script.approved_at),
        "source": script.source,
        "notes": script.notes,
        "item_count": len(rows),
        "required_item_count": sum(1 for i in rows if i.required),
        "created_at": _iso(script.created_at),
        "updated_at": _iso(script.updated_at),
    }
    if items:
        out["items"] = [_item_out(i) for i in rows]
    return out


def _live_ids(db: Session) -> set[str]:
    """Ids of the scripts an AE would actually be shown, one per product.

    The same rule ``active_script_for`` applies — approved, active, newest
    version wins — resolved for the whole catalogue in one read so the list
    view and the panel cannot disagree about which version is live. They are
    two different code paths reading the same three columns, and the editor
    saying "live" about a version the panel does not render is a lie the author
    has no way to check.
    """
    rows = db.execute(
        select(CallScript.id, CallScript.product_key, CallScript.version).where(
            CallScript.is_active.is_(True),
            CallScript.approved_by.isnot(None),
        )
    ).all()
    best: dict[str, tuple[int, str]] = {}
    for script_id, product_key, version in rows:
        current = best.get(product_key)
        if current is None or (version or 0) >= current[0]:
            best[product_key] = ((version or 0), script_id)
    return {script_id for _version, script_id in best.values()}


def _audit(
    db: Session, *, actor: str, action: str, script: CallScript, **details
) -> None:
    """One audit row per authoring action. Call AFTER the commit.

    ``record_audit`` commits its own row, so calling it mid-transaction would
    commit a half-finished edit. Scripts are governance content — they decide
    what the company asks a client and what the wrap-up calls a gap — so who changed
    which version, and who signed it, is worth keeping even though nothing else
    in the feature reads it.
    """
    record_audit(
        db,
        actor_email=actor,
        action=action,
        target=script.id,
        details={
            "product_key": script.product_key,
            "version": script.version,
            **details,
        },
    )


# ── GET /scripts ─────────────────────────────────────────────────────────────


@router.get("")
def list_scripts(
    product_key: str | None = Query(default=None),
    include_drafts: bool = Query(default=False),
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """The script catalogue, with item counts.

    Defaults to LIVE scripts only — approved, active, newest version per
    product — because that is what an AE may see. ``include_drafts=true``
    returns every version of every script, unapproved and retired included, and
    requires the authoring gate: a draft in front of an AE is a model's guess
    wearing the company's checklist, which is exactly what human approval
    exists to refuse.

    ``can_author`` is returned so the frontend can hide the editor rather than
    render a control that 403s. It is a mirror of the gate, never a substitute
    for it — every mutating endpoint re-checks.
    """
    _require_ccs_enabled()
    can_author = user_can_access_cockpit_supervisor(user)
    if include_drafts and not can_author:
        raise HTTPException(status_code=403, detail=_AUTHOR_DENIED)

    stmt = select(CallScript)
    if product_key:
        stmt = stmt.where(CallScript.product_key == _fold(product_key))
    if not include_drafts:
        stmt = stmt.where(
            CallScript.is_active.is_(True), CallScript.approved_by.isnot(None)
        )
    rows = (
        db.execute(
            stmt.order_by(
                CallScript.product_key.asc(), CallScript.version.desc()
            ).limit(_MAX_SCRIPTS)
        )
        .scalars()
        .all()
    )
    live = _live_ids(db)
    if not include_drafts:
        # The SQL filter bounds the read; this is what makes the default view
        # ONE ROW PER PRODUCT. A superseded-but-still-approved v1 is active by
        # design — retiring it is what makes a bad v2 undoable — so without this
        # an AE would see "Onboarding Review" twice, with two different checklists
        # and no way to tell which one the panel is about to render.
        rows = [row for row in rows if row.id in live]
    return {
        "scripts": [
            _script_out(row, is_live=row.id in live, items=False) for row in rows
        ],
        "can_author": can_author,
    }


# ── GET /scripts/{product_key} ───────────────────────────────────────────────


@router.get("/id/{script_id}")
def get_script_by_id(
    script_id: str,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """One script by id, INCLUDING its questions, whatever its approval state.

    The editor needs this and nothing else provides it: the list omits ``items``
    and the by-product read returns only the live approved version. Without it
    an author who saves a draft on Monday opens it on Tuesday to a version whose
    questions cannot be shown — on the one screen whose entire purpose is that
    the supervisor maintains checklists without an engineer.

    Declared BEFORE ``/{product_key}`` so the literal segment wins, per the
    repo's route-order rule. The two cannot actually collide — ``/id/x`` is two
    segments and ``/{product_key}`` matches one — but relying on that is relying
    on an accident.

    Author-gated, unlike the two reads above: an unapproved draft is exactly
    what must never reach an AE (an AI-generated script never ships as
    authoritative), and a mined draft is unreviewed machine output.
    """
    _require_ccs_enabled()
    _require_author(user)
    script = db.get(CallScript, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="No such call script.")
    live = active_script_for(db, script.product_key)
    return _script_out(script, is_live=live is not None and live.id == script.id)


@router.get("/{product_key}")
def get_active_script(
    product_key: str,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """The live script for one product — the checklist an AE would be shown.

    Resolved through the engine's ``active_script_for`` rather than a query of
    its own, so this endpoint and the calendar panel cannot drift about what
    "live" means. A product with nothing but drafts 404s, which is the honest
    answer: there is no approved checklist for it yet.
    """
    _require_ccs_enabled()
    script = active_script_for(db, product_key)
    if script is None:
        raise HTTPException(
            status_code=404,
            detail=f"No approved call script for {_fold(product_key)!r}.",
        )
    return _script_out(script, is_live=True)


# ── POST /scripts ────────────────────────────────────────────────────────────


@router.post("", status_code=201)
def create_script(
    body: ScriptCreate,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Create a DRAFT. This is the form the supervisor fills in.

    The row lands unapproved whatever else is set — ``approved_by`` is NULL and
    only ``POST /{id}/approve`` can change that — so nothing created here
    reaches an AE until a named human signs it.

    ``source`` is fixed to ``"manual"`` and is not accepted from the body. The
    other two sources are real but neither comes through this door:
    ``"mined"`` belongs to the batch job that drafts from the transcript corpus,
    and letting a caller claim it would let machine-drafted content enter the
    table labelled as though a human had brought it — provenance is what an
    approver reads when deciding whether to sign, so a forgeable label is worse
    than none.

    A product that already has scripts gets the next version, not a rejection:
    starting a fresh version from scratch is a legitimate way to author, and
    ``active_script_for`` keeps showing the newest APPROVED one until this draft
    is signed.
    """
    _require_ccs_enabled()
    actor = _require_author(user)

    product_key = _key(body.product_key, field="product_key")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A script needs a name.")
    items = _clean_items(body.items)

    script = CallScript(
        id=uuid.uuid4().hex,
        product_key=product_key,
        name=name,
        version=_next_version(db, product_key),
        hubspot_product_id=(body.hubspot_product_id or "").strip() or None,
        is_active=True,
        source="manual",
        notes=(body.notes or "").strip() or None,
    )
    db.add(script)
    _flush_or_conflict(db)
    for item in items:
        db.add(ScriptItem(id=uuid.uuid4().hex, script_id=script.id, **item))
    _commit_or_conflict(db)
    db.refresh(script)

    _audit(db, actor=actor, action="CALL_SCRIPT_CREATE", script=script)
    logger.info(
        "[ccs] %s drafted call script %s v%s (%s items)",
        actor,
        script.product_key,
        script.version,
        len(items),
    )
    return _script_out(script, is_live=False)


# ── PUT /scripts/{id} ────────────────────────────────────────────────────────


@router.put("/{script_id}")
def update_script(
    script_id: str,
    body: ScriptUpdate,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Edit a draft in place; editing an APPROVED script FORKS a new version.

    Rule 1 of this module, and the reason it is not merely tidy: a call that is
    already running against a script recomputes its gap list from that script's
    items every time its wrap-up is opened. Change an approved script under it
    and the record of what the AE covered changes with it, silently, months
    later. So an approved script is copied to ``version + 1`` with
    ``approved_by`` cleared, the original stays live and untouched, and the fork
    reaches AEs only when somebody signs it.

    ``is_active`` alone is the exception, and it is exactly the exception the
    model layer describes: that column is "the AUTHOR'S on/off switch", it
    changes nothing a past call was measured against, and forking on a
    retire/restore would produce an unapproved copy — which cannot be live, and
    therefore could never restore anything. So a body carrying only
    ``is_active`` toggles the row it names, approved or not.

    A fork's ``source`` becomes ``"manual"`` regardless of the parent's: a human
    just hand-edited this checklist, and a fork of a mined draft that reports
    ``"mined"`` overstates the machine's part in what the next approver is being
    asked to sign.
    """
    _require_ccs_enabled()
    actor = _require_author(user)
    script = _get_script(db, script_id)

    provided = body.model_fields_set
    content = provided & _CONTENT_FIELDS
    if not provided:
        raise HTTPException(status_code=400, detail="Nothing to change.")

    items = _clean_items(body.items) if body.items is not None else None
    name = (body.name or "").strip() if "name" in provided else None
    if "name" in provided and not name:
        raise HTTPException(status_code=400, detail="A script needs a name.")

    # ── The retire/restore toggle: in place, on any script ──────────────────
    if script.approved_by is not None and not content:
        if body.is_active is None:
            # ``{"is_active": null}`` is not a retire instruction. Coercing it
            # would silently switch a live script off on a UI that posts its
            # whole form with an unset field.
            raise HTTPException(status_code=400, detail="Nothing to change.")
        script.is_active = bool(body.is_active)
        _commit_or_conflict(db)
        db.refresh(script)
        _audit(
            db,
            actor=actor,
            action="CALL_SCRIPT_UPDATE",
            script=script,
            is_active=script.is_active,
        )
        return _script_out(script, is_live=script.id in _live_ids(db))

    # ── A draft edits in place: nothing has ever run against it ─────────────
    if script.approved_by is None:
        if "name" in provided:
            script.name = name
        if "hubspot_product_id" in provided:
            script.hubspot_product_id = (body.hubspot_product_id or "").strip() or None
        if "notes" in provided:
            script.notes = (body.notes or "").strip() or None
        if "is_active" in provided and body.is_active is not None:
            script.is_active = bool(body.is_active)
        if items is not None:
            _replace_items(db, script, items)
        _commit_or_conflict(db)
        db.refresh(script)
        _audit(
            db,
            actor=actor,
            action="CALL_SCRIPT_UPDATE",
            script=script,
            forked=False,
        )
        logger.info("[ccs] %s edited draft script %s", actor, script.id)
        return _script_out(script, is_live=False)

    # ── An approved script forks ────────────────────────────────────────────
    parent_items = [
        {
            "sequence": item.sequence,
            "fact_key": item.fact_key,
            "prompt_text": item.prompt_text,
            "hubspot_target": item.hubspot_target,
            "required": bool(item.required),
            "help_text": item.help_text,
        }
        for item in sorted(script.items or [], key=lambda i: i.sequence or 0)
    ]
    fork = CallScript(
        id=uuid.uuid4().hex,
        product_key=script.product_key,
        name=name if "name" in provided else script.name,
        version=_next_version(db, script.product_key),
        hubspot_product_id=(
            (body.hubspot_product_id or "").strip() or None
            if "hubspot_product_id" in provided
            else script.hubspot_product_id
        ),
        is_active=True if body.is_active is None else bool(body.is_active),
        # NULL on purpose — a fork is a draft until somebody signs it.
        approved_by=None,
        approved_at=None,
        source="manual",
        notes=(
            (body.notes or "").strip() or None if "notes" in provided else script.notes
        ),
    )
    db.add(fork)
    _flush_or_conflict(db)
    for item in items if items is not None else parent_items:
        db.add(ScriptItem(id=uuid.uuid4().hex, script_id=fork.id, **item))
    _commit_or_conflict(db)
    db.refresh(fork)

    _audit(
        db,
        actor=actor,
        action="CALL_SCRIPT_FORK",
        script=fork,
        forked_from=script.id,
        forked_from_version=script.version,
    )
    logger.info(
        "[ccs] %s forked approved script %s v%s into v%s",
        actor,
        script.product_key,
        script.version,
        fork.version,
    )
    out = _script_out(fork, is_live=False)
    out["forked"] = True
    out["forked_from_script_id"] = script.id
    return out


# ── POST /scripts/{id}/approve ───────────────────────────────────────────────


@router.post("/{script_id}/approve")
def approve_script(
    script_id: str,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Sign a script. The ONLY thing that makes one live.

    Takes NO body: ``approved_by`` is stamped from the authenticated caller,
    never read from the request. A signature a caller can supply is not a
    signature, and this column is the only evidence that each script was
    approved by a named human and none was generated-and-shipped.

    Two refusals, both of them the failure this feature exists to prevent:

    * **An empty checklist.** A script with no items renders an empty panel
      section and reports every fact as covered, because there are none — a
      wrap-up that looks clean over a call that surfaced nothing, which is the
      original problem with a checklist bolted on top.
    * **A retired script.** ``is_active`` false means the author switched this
      version off; signing it would produce an approved-but-invisible row and
      leave the approver believing they had just published something.

    Idempotent, and it never re-signs: a script that is already approved comes
    back with its ORIGINAL approver and timestamp and ``already_approved``. The
    signature records who read the checklist and when — overwriting it on a
    second click would quietly reassign accountability for content that is
    already live.
    """
    _require_ccs_enabled()
    actor = _require_author(user)
    script = _get_script(db, script_id)

    if script.approved_by:
        out = _script_out(script, is_live=script.id in _live_ids(db))
        out["already_approved"] = True
        return out
    if not (script.items or []):
        raise HTTPException(
            status_code=400,
            detail=(
                "This script has no items. An empty checklist reports every "
                "call as complete — add the facts the call has to surface first."
            ),
        )
    if not script.is_active:
        raise HTTPException(
            status_code=409,
            detail=(
                "This version is retired. Reactivate it before approving, or "
                "approve the version that replaced it."
            ),
        )

    script.approved_by = actor  # folded again by the model's @validates hook
    script.approved_at = datetime.utcnow()
    _commit_or_conflict(db)
    db.refresh(script)

    _audit(
        db,
        actor=actor,
        action="CALL_SCRIPT_APPROVE",
        script=script,
        item_count=len(script.items or []),
    )
    logger.info(
        "[ccs] %s approved call script %s v%s",
        actor,
        script.product_key,
        script.version,
    )
    out = _script_out(script, is_live=script.id in _live_ids(db))
    out["already_approved"] = False
    return out


# ── DELETE /scripts/{id} ─────────────────────────────────────────────────────


@router.delete("/{script_id}")
def deactivate_script(
    script_id: str,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Retire a script by clearing ``is_active``. Nothing is ever hard-deleted.

    ``call_session_scripts.script_id`` carries no ``ondelete`` precisely so this
    endpoint cannot become a way to destroy the record of what a call ran
    against: the wrap-up recomputes its gap list from the script's items, and a
    deleted script turns a finished, explicable call into an unexplainable one.
    Clearing ``is_active`` removes the row from ``active_script_for``, so it
    stops reaching AEs immediately, and leaves every past call intact.

    Applies to approved scripts too — the company stops selling a product and its
    checklist has to be retirable — and it is reversible through
    ``PUT {"is_active": true}``, which is why that one field edits an approved
    script in place instead of forking.

    Idempotent: retiring a retired script is a no-op with a 200, not a 404. The
    author's intent is already satisfied.
    """
    _require_ccs_enabled()
    actor = _require_author(user)
    script = _get_script(db, script_id)

    if not script.is_active:
        out = _script_out(script, is_live=False)
        out["already_inactive"] = True
        return out

    script.is_active = False
    _commit_or_conflict(db)
    db.refresh(script)

    _audit(
        db,
        actor=actor,
        action="CALL_SCRIPT_DEACTIVATE",
        script=script,
        was_approved=bool(script.approved_by),
    )
    logger.info(
        "[ccs] %s retired call script %s v%s", actor, script.product_key, script.version
    )
    out = _script_out(script, is_live=False)
    out["already_inactive"] = False
    return out
