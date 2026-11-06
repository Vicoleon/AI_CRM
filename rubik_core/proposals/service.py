"""Proposal Generator — the three steps, and the model calls behind them.

Sync functions, called from the router behind ``run_in_threadpool`` (the
model calls block for tens of seconds and must not hold the event loop).
Nothing here talks to the client or sends anything anywhere; the AE downloads
the document and sends it themselves.

``@attribute_to(COST_FEATURE)`` sits on :func:`_llm_json`, the function that
actually calls the model provider, for the reason ``call_capture/extraction.py`` spells
out: ``current_feature`` is a contextvar and does not survive the hop into a
worker thread, so a tag set on the router would report this spend as
"Unattributed".
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import (
    ProviderRequestError,
    generate_text_on_route,
)
from rubik_core.brand import PRODUCT_NAME
from rubik_core.call_capture.provenance import (
    session_source_visible,
    visible_session_ids,
)
from rubik_core.company.profile import get_company_profile
from rubik_core.cost.usage import attribute_to
from rubik_core.database.call_capture import CallSession, CallTranscriptEntry
from rubik_core.database.models import User
from rubik_core.database.proposals import Proposal, ProposalVersion
from rubik_core.models import get_genai_client, safe_json
from rubik_core.proposals import COST_FEATURE, prompts, templates
from rubik_core.proposals.brief import (
    MAX_PRICING_LINES,
    normalize_brief,
    normalize_content,
    normalize_template,
)
from rubik_core.sales.model_config import model_for
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.crm_visibility import visible_source_ids

logger = logging.getLogger(__name__)

#: Same cap as CRM Sync's ``TRANSCRIPT_MAX_BYTES``: comfortably inside the PRO
#: context window with the system prompt and the HubSpot block beside it. A
#: discovery call is far shorter; the cap exists for the all-day workshop.
TRANSCRIPT_MAX_CHARS = 150_000
MAX_INSTRUCTION_CHARS = 2_000
_PROVENANCE_KEY = "_crm_provenance"


class ProposalError(RuntimeError):
    """Base for the failures the router turns into a 4xx with a sentence."""


class NoTranscript(ProposalError):
    """The call has no transcript entries — there is nothing to read."""


class BriefNotConfirmed(ProposalError):
    """Generate was asked for before the AE confirmed the brief."""


class NothingToRevise(ProposalError):
    """Revise was asked for before any version existed."""


class ModelFailed(ProposalError):
    """The model call failed or returned something unparseable."""


class SelectedProviderFailed(ModelFailed):
    """The workspace's selected provider needs admin attention."""


class RecordingNotFound(ProposalError):
    """No Fathom recording with that id is in Cubie or in Fathom's recent listing."""


class NotYourCall(ProposalError):
    """The recording exists, but its host is somebody else."""


class CouldNotAttach(ProposalError):
    """Fathom has the recording, but it could not become a captured call."""


class SourceAccessChanged(ProposalError):
    """Stored CRM facts cannot be proved inside this workspace anymore."""


def _ids_from_columns(
    row: Proposal | CallSession,
) -> tuple[set[str], set[str], set[str]]:
    return (
        {str(row.hubspot_company_id)} if row.hubspot_company_id else set(),
        {str(row.hubspot_contact_id)} if row.hubspot_contact_id else set(),
        {str(row.hubspot_deal_id)} if row.hubspot_deal_id else set(),
    )


def _sources_visible(sources: tuple[set[str], set[str], set[str]]) -> bool:
    return visible_source_ids(*sources) == sources


def session_sources_visible(session: CallSession) -> bool:
    """Current live proof before using a captured call's stored CRM identity."""
    return session_source_visible(session)


def _source_marker(session: CallSession) -> dict:
    sources = _ids_from_columns(session)
    if not session_sources_visible(session):
        raise SourceAccessChanged("This call's CRM sources are unavailable.")
    return {
        "schema_version": 1,
        "workspace": current_workspace(),
        "company_id": next(iter(sources[0]), None),
        "contact_id": next(iter(sources[1]), None),
        "deal_ids": sorted(sources[2]),
        "unprovable": False,
    }


def _proposal_sources(
    proposal: Proposal,
) -> tuple[set[str], set[str], set[str]] | None:
    """Trust only the server stamp and exact stored source IDs, never brief prose."""
    source = proposal.brief_source
    marker = source.get(_PROVENANCE_KEY) if isinstance(source, dict) else None
    if (
        not isinstance(marker, dict)
        or type(marker.get("schema_version")) is not int
        or marker["schema_version"] != 1
        or marker.get("unprovable") is not False
        or marker.get("workspace") != current_workspace()
    ):
        return None
    sources = _ids_from_columns(proposal)
    company_id = marker.get("company_id")
    contact_id = marker.get("contact_id")
    deals = marker.get("deal_ids")
    if (
        company_id != next(iter(sources[0]), None)
        or contact_id != next(iter(sources[1]), None)
        or not isinstance(deals, list)
        or deals != sorted(sources[2])
        or any(not isinstance(value, str) or not value for value in deals)
    ):
        return None
    return sources


def proposal_visible(proposal: Proposal) -> bool:
    """A legacy/unproved or revoked proposal may not be read or re-prompted."""
    sources = _proposal_sources(proposal)
    return sources is not None and _sources_visible(sources)


def visible_proposals(proposals: list[Proposal]) -> list[Proposal]:
    """Batch live proof for list rows without publishing a revoked summary."""
    marked = [(row, _proposal_sources(row)) for row in proposals]
    scopes = [scope for _, scope in marked if scope is not None]
    combined = tuple(set().union(*(scope[i] for scope in scopes)) for i in range(3))
    visible = visible_source_ids(*combined)
    return [
        row
        for row, scope in marked
        if scope is not None and all(scope[i] <= visible[i] for i in range(3))
    ]


# ── Model ────────────────────────────────────────────────────────────────────


def _model_id() -> str:
    """PRO by default: a proposal is read by a prospect's decision-maker and a
    wrong sentence costs more than the tokens do at this volume. Never a
    ``-latest`` alias (see ``config.py``).

    Admin-overridable at runtime in Admin → Sales Models; falls back to
    ``PROPOSALS_MODEL`` and then the PRO tier when nothing is stored. A
    workspace-owned AI route, when selected, overrides this default."""
    return model_for("proposals")


class _ModelJSON(dict):
    """A parsed model object that carries its verified route into persistence."""

    def __init__(self, payload: dict, *, model_label: str):
        super().__init__(payload)
        self.model_label = model_label


def _model_label(raw: Any, fallback: str) -> str:
    return getattr(raw, "model_label", fallback)


@attribute_to(COST_FEATURE)
def _llm_json(system: str, prompt: str, *, model_id: str, temperature: float) -> Any:
    """One workspace-bound JSON call, with no client-provider fallback."""
    from google.genai import types

    from rubik_core.gemini_tuning import generation_config_for

    route = None
    try:
        route = resolve_model_route(COST_FEATURE, "json", default_model=model_id)
        if route.provider == "vertex_managed":
            current = resolve_model_route(COST_FEATURE, "json", default_model=model_id)
            if current != route:
                raise IncompatibleModelRoute(
                    "Workspace AI route changed during proposal generation"
                )
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
                response_mime_type="application/json",
            )
            response = get_genai_client().models.generate_content(
                model=route.model,
                contents=prompt,
                config=generation_config_for(route.model, config),
            )
            text = response.text or ""
        else:
            response = generate_text_on_route(
                route,
                prompt,
                json_mode=True,
                system_instruction=system,
                temperature=temperature,
            )
            text = response.text
        parsed = safe_json(text)
        if not isinstance(parsed, dict):
            raise ValueError("Proposal model did not return a JSON object")
        label = (
            route.model
            if route.provider == "vertex_managed"
            else f"{route.provider}:{route.model}"
        )
        return _ModelJSON(parsed, model_label=label)
    except Exception as exc:  # noqa: BLE001 — surfaced as one typed error
        # Model errors and parser failures can echo the prospect's transcript.
        # Neither logs nor HTTP errors may include any provider-supplied text.
        logger.warning(
            "[proposals] model call failed (%s): %s",
            route.provider if route else "route",
            type(exc).__name__,
        )
        if isinstance(exc, IncompatibleModelRoute):
            raise SelectedProviderFailed(
                "Proposal AI route is unavailable. Ask your admin to check it."
            ) from None
        if isinstance(exc, ProviderRequestError):
            raise SelectedProviderFailed(
                "Selected proposal AI provider failed. Check its workspace credential and model."
            ) from None
        if route is not None and route.provider != "vertex_managed":
            raise SelectedProviderFailed(
                "Selected proposal AI provider returned invalid output. Ask your admin to check its model."
            ) from None
        raise ModelFailed("Proposal model failed. Try again.") from None


# ── Inputs ───────────────────────────────────────────────────────────────────


def transcript_lines(db: Session, session_id: str) -> list[str]:
    """``[sequence] Speaker: text`` per entry, the shape the extractor uses."""
    entries = (
        db.query(CallTranscriptEntry)
        .filter(CallTranscriptEntry.call_session_id == session_id)
        .order_by(CallTranscriptEntry.sequence.asc())
        .all()
    )
    lines: list[str] = []
    size = 0
    for e in entries:
        line = (
            f"[{e.sequence}] {e.speaker_name or e.speaker_email or 'Unknown'}: {e.text}"
        )
        if size + len(line) > TRANSCRIPT_MAX_CHARS:
            logger.info(
                "[proposals] transcript for %s truncated at %s chars", session_id, size
            )
            break
        lines.append(line)
        size += len(line) + 1
    return lines


def hubspot_context(session: CallSession) -> dict[str, Any]:
    """Read only currently owned CRM facts for the brief prompt."""
    from rubik_core.retrieval.hubspot_write import HubSpotWriteClient

    if not session_sources_visible(session):
        raise SourceAccessChanged("This call's CRM sources are unavailable.")
    out: dict[str, Any] = {"company": None, "contact": None, "deal": None}
    wants = (
        (
            "company",
            "companies",
            session.hubspot_company_id,
            ["name", "domain", "city", "state", "industry"],
        ),
        (
            "contact",
            "contacts",
            session.hubspot_contact_id,
            ["firstname", "lastname", "jobtitle", "email", "company"],
        ),
        (
            "deal",
            "deals",
            session.hubspot_deal_id,
            ["dealname", "amount", "dealstage", "closedate"],
        ),
    )
    client = None
    for key, object_type, object_id, props in wants:
        if not object_id:
            continue
        try:
            client = client or HubSpotWriteClient()
            record = client.get_object(object_type, object_id, props)
        except Exception as exc:  # noqa: BLE001 — a name is a nicety, not the draft
            logger.warning(
                "[proposals] could not read %s %s: %s", object_type, object_id, exc
            )
            continue
        properties = {
            k: v for k, v in ((record or {}).get("properties") or {}).items() if v
        }
        if key == "contact":
            name = " ".join(
                p
                for p in (properties.get("firstname"), properties.get("lastname"))
                if p
            )
            out[key] = {
                "id": object_id,
                "name": name,
                "title": properties.get("jobtitle"),
                "email": properties.get("email"),
            }
        elif key == "company":
            out[key] = {"id": object_id, **properties}
        else:
            out[key] = {"id": object_id, **properties}
    return out


def ae_identity(db: Session, email: str) -> dict[str, str]:
    """Display name for the AE, falling back to the address (offboarding
    deletes ``users`` rows; the call still happened)."""
    folded = (email or "").strip().lower()
    row = db.query(User).filter(func.lower(User.email) == folded).first()
    return {
        "name": (row.name if row and row.name else folded) or folded,
        "email": folded,
    }


_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def ref_code(company: str | None, when: date, seed: str) -> str:
    """``P-<PROSPECT INITIALS>-<YEAR>-<4 hex>``, e.g. ``P-TPC-2026-1A2B``.

    The hash keeps two proposals to the same prospect in a year from
    colliding. The prefix is neutral: the ref prints on the document, which
    carries the workspace's letterhead, not ours."""
    words = [w for w in _NOT_ALNUM.split(company or "") if w]
    initials = "".join(w[0] for w in words[:4]).upper() or "PROP"
    tail = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:4].upper()
    return f"P-{initials}-{when.year}-{tail}"


# ── Lists ────────────────────────────────────────────────────────────────────


def list_proposals(
    db: Session, *, actor_email: str, include_all: bool = False, limit: int = 200
) -> list[Proposal]:
    """The AE's own proposals, newest first; every AE's when ``include_all``
    (admins, for support). Archived ones are included — the page groups them."""
    q = db.query(Proposal)
    if not include_all:
        q = q.filter(Proposal.owner_email == _fold(actor_email))
    target = max(0, int(limit))
    if not target:
        return []
    ordered = q.order_by(Proposal.updated_at.desc(), Proposal.id.desc())
    batch_size = max(100, target)
    found: list[Proposal] = []
    offset = 0
    while len(found) < target:
        batch = ordered.offset(offset).limit(batch_size).all()
        if not batch:
            break
        found.extend(visible_proposals(batch))
        offset += len(batch)
    return found[:target]


def recent_captured_calls(
    db: Session,
    *,
    actor_email: str,
    include_all: bool = False,
    days: int = 14,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Captured calls a proposal can be drafted from — the AE's own, or every
    AE's for an admin — with whether one already exists.

    Only calls that have a transcript worth reading: status ``transcript_ready``,
    ``extracted`` or ``wrapped``. A confidential (suppressed) call is left out —
    nothing is drafted from it. This is also the one list where a Fathom-recorded
    call the AE never opened from Today's Calls shows up, because Today's Calls
    lists pending wrap-ups only.
    """
    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    q = (
        db.query(CallSession)
        .filter(CallSession.status.in_(("transcript_ready", "extracted", "wrapped")))
        .filter(CallSession.capture_consent != "suppressed")
        .filter(
            (CallSession.ended_at >= since)
            | (CallSession.ended_at.is_(None) & (CallSession.created_at >= since))
        )
    )
    if not include_all:
        q = q.filter(CallSession.ae_email == _fold(actor_email))
    target = max(0, int(limit))
    if not target:
        return []
    ordered = q.order_by(
        CallSession.ended_at.desc().nullslast(),
        CallSession.created_at.desc(),
        CallSession.id.desc(),
    )
    batch_size = max(100, target)
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < target:
        batch = ordered.offset(offset).limit(batch_size).all()
        if not batch:
            break
        offset += len(batch)
        visible_ids = visible_session_ids(batch)
        sessions = [session for session in batch if session.id in visible_ids]
        if not sessions:
            continue
        linked = (
            db.query(Proposal)
            .filter(Proposal.call_session_id.in_([s.id for s in sessions]))
            .all()
        )
        by_session = {p.call_session_id: p for p in visible_proposals(linked)}
        hidden_session_ids = {p.call_session_id for p in linked} - set(by_session)
        for s in sessions:
            if s.id in hidden_session_ids:
                continue
            attrs = s.attrs or {}
            proposal = by_session.get(s.id)
            out.append(
                {
                    "call_session_id": s.id,
                    "ae_email": s.ae_email,
                    "company_name": attrs.get("company_name")
                    if s.hubspot_company_id
                    else None,
                    "hubspot_company_id": s.hubspot_company_id,
                    "title": (attrs.get("fathom") or {}).get("title"),
                    "origin": s.origin,
                    "status": s.status,
                    "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                    "wrapped_at": s.wrapped_at.isoformat() if s.wrapped_at else None,
                    "proposal_id": proposal.id if proposal else None,
                    "proposal_status": proposal.status if proposal else None,
                }
            )
            if len(out) >= target:
                break
    return out


# ── Step 1: brief ────────────────────────────────────────────────────────────


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def existing_for_call(db: Session, session_id: str) -> Proposal | None:
    return db.query(Proposal).filter(Proposal.call_session_id == session_id).first()


def session_for_fathom(
    db: Session,
    recording_id: str,
    *,
    actor_email: str,
    is_admin: bool = False,
    fetch_meeting=None,
) -> CallSession:
    """The captured call behind a Fathom recording — attached on demand.

    The hand-off the Sales Coach uses: it knows every Fathom recording by id
    and links "Continue in Cubie" here. Normally the Fathom webhook has already
    attached the recording (``call_capture/fathom_attach``); when it has not —
    the webhook lagged, or this is the first minute after the call — the
    recording is fetched from Fathom's recent listing and attached now, on the
    same code path, so the two never disagree about what a captured call is.

    Raises :class:`RecordingNotFound`, :class:`NotYourCall` or
    :class:`CouldNotAttach`; the router maps them to 404 / 403 / 409.
    """
    from rubik_core.call_capture import fathom_attach

    rid = str(recording_id or "").strip()
    if not rid:
        raise RecordingNotFound("No recording id.")
    session = (
        db.query(CallSession).filter(CallSession.fathom_recording_id == rid).first()
    )
    if session is None:
        if fetch_meeting is None:
            from rubik_core.integrations.fathom import FathomClient

            fetch_meeting = FathomClient().find_meeting
        meeting = fetch_meeting(rid)
        if not meeting:
            raise RecordingNotFound(
                f"{PRODUCT_NAME} has not received this Fathom recording yet, and "
                "Fathom's recent list does not have it either."
            )
        outcome = fathom_attach.attach_meeting(db, meeting)
        if outcome["outcome"] not in (
            fathom_attach.ATTACHED,
            fathom_attach.REPLAYED,
            fathom_attach.LINKED_EXISTING_TRANSCRIPT,
        ):
            raise CouldNotAttach(_attach_refusal(outcome["outcome"]))
        session = db.get(CallSession, outcome["session_id"])
        if session is None:
            raise CouldNotAttach(
                "The recording was attached but its call could not be read back."
            )
    if _fold(session.ae_email) != _fold(actor_email) and not is_admin:
        raise NotYourCall("This recording belongs to another AE's call.")
    return session


def _attach_refusal(outcome: str) -> str:
    from rubik_core.call_capture import fathom_attach

    return {
        fathom_attach.SKIPPED_INTERNAL_ONLY: "This recording has no external attendee, so it is not a client call.",
        fathom_attach.FATHOM_SKIP_HOST_OUTSIDE: "This recording was not made by one of your company's accounts.",
        fathom_attach.SKIPPED_NO_TRANSCRIPT: "Fathom has no transcript for this recording yet.",
        fathom_attach.SKIPPED_TOO_OLD: f"This recording is older than {PRODUCT_NAME} captures calls for.",
        fathom_attach.SUPPRESSED: "This call is marked confidential; nothing is drafted from it.",
        fathom_attach.SKIPPED_DISABLED: "Fathom capture is switched off in this environment.",
    }.get(
        outcome, f"{PRODUCT_NAME} could not turn this recording into a captured call."
    )


def _apply_template(db: Session, brief: dict) -> None:
    """Clear a template key the workspace does not have, and price a chosen one.

    The model picks a key from the menu it was shown; one it invented, or one
    an admin has since switched off, is cleared. A chosen template's catalog
    rows become pricing lines (``templates.pricing_lines``) unless a line of
    that name is already on the brief, so a price spoken on the call is never
    overwritten by the list price.
    """
    tpl = templates.get(db, (brief.get("template") or {}).get("key"))
    if tpl is None:
        brief["template"] = {"key": ""}
        return
    taken = {
        (line.get("description") or "").strip().casefold() for line in brief["pricing"]
    }
    ids = {line.get("id") for line in brief["pricing"]}
    for line in templates.pricing_lines(db, tpl):
        if len(brief["pricing"]) >= MAX_PRICING_LINES:
            break
        if line["description"].casefold() in taken:
            continue
        n = 1
        while f"p{n}" in ids:
            n += 1
        ids.add(f"p{n}")
        brief["pricing"].append({"id": f"p{n}", **line})


def start_from_call(db: Session, session: CallSession, *, actor_email: str) -> Proposal:
    """Create the proposal for a call and fill its brief from the transcript.

    Idempotent per call: a second click opens the existing proposal. The model
    is asked ONCE; its raw answer is kept on ``brief_source`` for tuning and
    the normalized form becomes the editable ``brief``.
    """
    existing = existing_for_call(db, session.id)
    if existing is not None:
        if not proposal_visible(existing):
            raise SourceAccessChanged("This proposal's CRM sources are unavailable.")
        return existing

    marker = _source_marker(session)

    lines = transcript_lines(db, session.id)
    if not lines:
        raise NoTranscript("This call has no transcript to read.")
    text = "\n".join(lines)

    hubspot = hubspot_context(session)
    ae = ae_identity(db, session.ae_email)
    active = templates.list_templates(db, active_only=True)
    model_id = _model_id()
    raw = _llm_json(
        prompts.brief_system(get_company_profile(db), active),
        prompts.brief_prompt(transcript_lines=lines, hubspot=hubspot, ae=ae),
        model_id=model_id,
        temperature=0.2,
    )
    brief, dropped = normalize_brief(raw, transcript_text=text)
    _apply_template(db, brief)
    if dropped:
        logger.info(
            "[proposals] brief for call %s dropped %s item(s): %s",
            session.id,
            len(dropped),
            sorted({(d["section"], d["reason"]) for d in dropped}),
        )

    company_name = (hubspot.get("company") or {}).get("name")
    contact = hubspot.get("contact") or {}
    # Fill the blanks the CRM can answer; never overwrite what the model heard.
    pf = brief["prepared_for"]
    pf["company"] = pf["company"] or company_name or ""
    pf["name"] = pf["name"] or contact.get("name") or ""
    pf["title"] = pf["title"] or contact.get("title") or ""
    brief["prepared_by"] = {
        "name": ae["name"],
        "title": "Account Executive",
        "email": ae["email"],
    }
    today = date.today()
    brief["date"] = today.isoformat()
    brief["ref_code"] = brief["ref_code"] or ref_code(pf["company"], today, session.id)
    if not brief["service_title"]:
        brief["service_title"] = "Services Proposal"
    company_name = company_name or pf["company"] or None

    if not session_sources_visible(session):
        raise SourceAccessChanged("This call's CRM sources are unavailable.")

    row = Proposal(
        id=uuid.uuid4().hex,
        owner_email=_fold(session.ae_email),
        call_session_id=session.id,
        hubspot_company_id=session.hubspot_company_id,
        hubspot_contact_id=session.hubspot_contact_id,
        hubspot_deal_id=session.hubspot_deal_id,
        company_name=company_name,
        title=brief["service_title"],
        status="brief",
        brief=brief,
        brief_source={
            **(raw if isinstance(raw, dict) else {"raw": raw}),
            _PROVENANCE_KEY: marker,
        },
        model=_model_label(raw, model_id),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Two tabs clicked at once; the UNIQUE on call_session_id arbitrated.
        db.rollback()
        twin = existing_for_call(db, session.id)
        if twin is None:
            raise
        if not proposal_visible(twin):
            raise SourceAccessChanged("This proposal's CRM sources are unavailable.")
        return twin
    logger.info(
        "[proposals] %s started proposal %s from call %s (%s scope, %s outcomes, %s open)",
        actor_email,
        row.id,
        session.id,
        len(brief["scope_items"]),
        len(brief["desired_outcomes"]),
        len(brief["open_questions"]),
    )
    return row


def save_brief(
    db: Session, proposal: Proposal, raw_brief: Any, *, actor_email: str
) -> Proposal:
    """The AE's confirmation. Their words are trusted — no transcript check —
    but the shape is still enforced, and the pinned identity fields stay."""
    if not proposal_visible(proposal):
        raise SourceAccessChanged("This proposal's CRM sources are unavailable.")
    current = proposal.brief or {}
    brief, _ = normalize_brief(raw_brief, keep_unquoted_outcomes=True)
    # The AE edits facts, not provenance.
    brief["prepared_by"] = current.get("prepared_by") or brief["prepared_by"]
    # A client that does not send the template field leaves it as it was.
    if not (isinstance(raw_brief, dict) and "template" in raw_brief):
        brief["template"] = normalize_template(current.get("template"))
    brief["ref_code"] = brief["ref_code"] or current.get("ref_code") or ""
    if not brief["service_title"]:
        brief["service_title"] = current.get("service_title") or "Services Proposal"
    proposal.brief = brief
    proposal.title = brief["service_title"]
    proposal.brief_confirmed_at = datetime.utcnow()
    db.commit()
    return proposal


# ── Step 2: generate ─────────────────────────────────────────────────────────


def latest_version(db: Session, proposal_id: str) -> ProposalVersion | None:
    return (
        db.query(ProposalVersion)
        .filter(ProposalVersion.proposal_id == proposal_id)
        .order_by(ProposalVersion.version_no.desc())
        .first()
    )


def _append_version(
    db: Session,
    proposal: Proposal,
    *,
    brief: dict,
    content: dict,
    instruction: str | None,
    change_summary: str | None,
    model_id: str,
    actor_email: str,
) -> ProposalVersion:
    if not proposal_visible(proposal):
        raise SourceAccessChanged("This proposal's CRM sources are unavailable.")
    for attempt in range(2):
        last = latest_version(db, proposal.id)
        row = ProposalVersion(
            id=uuid.uuid4().hex,
            proposal_id=proposal.id,
            version_no=(last.version_no if last else 0) + 1,
            brief=brief,
            content=content,
            instruction=instruction,
            change_summary=change_summary,
            model=f"{model_id} / {prompts.PROMPT_VERSION}",
            created_by=_fold(actor_email),
        )
        db.add(row)
        proposal.status = "drafted"
        proposal.brief = brief
        proposal.title = brief.get("service_title") or proposal.title
        try:
            db.commit()
            return row
        except IntegrityError:
            # Two revisions raced for the same version_no; take the next one.
            db.rollback()
            if attempt == 1:
                raise
    raise AssertionError("unreachable")


def _fence_acted(fence: dict) -> bool:
    return bool(
        fence["dropped_scope"]
        or fence["dropped_outcomes"]
        or fence["notes_reset"]
        or fence["priced_sentences"]
    )


def generate(db: Session, proposal: Proposal, *, actor_email: str) -> ProposalVersion:
    """Version N+1 straight from the confirmed brief."""
    if not proposal_visible(proposal):
        raise SourceAccessChanged("This proposal's CRM sources are unavailable.")
    if proposal.brief_confirmed_at is None or not proposal.brief:
        raise BriefNotConfirmed("Confirm the brief before generating.")
    brief = proposal.brief
    # Active or retired: a template switched off after the brief chose it
    # still belongs to this proposal (only a deleted one is gone).
    tpl = templates.get_by_key(db, (brief.get("template") or {}).get("key"))
    model_id = _model_id()
    raw = _llm_json(
        prompts.draft_system(get_company_profile(db)),
        prompts.draft_prompt(brief, tpl),
        model_id=model_id,
        temperature=0.3,
    )
    content, fence = normalize_content(raw, brief)
    if _fence_acted(fence):
        logger.info("[proposals] fence acted on %s: %s", proposal.id, fence)
    return _append_version(
        db,
        proposal,
        brief=brief,
        content=content,
        instruction=None,
        change_summary=None,
        model_id=_model_label(raw, model_id),
        actor_email=actor_email,
    )


# ── Step 3: revise ───────────────────────────────────────────────────────────


def revise(
    db: Session, proposal: Proposal, instruction: str, *, actor_email: str
) -> ProposalVersion:
    """Apply one instruction to the latest version and append the result."""
    if not proposal_visible(proposal):
        raise SourceAccessChanged("This proposal's CRM sources are unavailable.")
    text = (instruction or "").strip()[:MAX_INSTRUCTION_CHARS]
    if not text:
        raise ProposalError("Say what to change.")
    last = latest_version(db, proposal.id)
    if last is None:
        raise NothingToRevise("Generate the proposal before revising it.")

    model_id = _model_id()
    raw = _llm_json(
        prompts.revise_system(get_company_profile(db)),
        prompts.revise_prompt(brief=last.brief, content=last.content, instruction=text),
        model_id=model_id,
        temperature=0.2,
    )
    payload = raw if isinstance(raw, dict) else {}
    transcript = (
        "\n".join(transcript_lines(db, proposal.call_session_id))
        if proposal.call_session_id
        else None
    )
    brief, _ = normalize_brief(
        payload.get("brief") or last.brief,
        transcript_text=transcript,
        keep_unquoted_outcomes=True,
    )
    # Provenance fields are the system's, not the editor's.
    brief["prepared_by"] = (last.brief or {}).get("prepared_by") or brief["prepared_by"]
    brief["ref_code"] = brief["ref_code"] or (last.brief or {}).get("ref_code") or ""
    # The template is the AE's pick on the brief form, not the editor's.
    brief["template"] = normalize_template((last.brief or {}).get("template"))
    content, fence = normalize_content(payload.get("content") or last.content, brief)
    if _fence_acted(fence):
        logger.info("[proposals] fence acted on revision of %s: %s", proposal.id, fence)
    summary = str(payload.get("change_summary") or "").strip()[:500] or None
    return _append_version(
        db,
        proposal,
        brief=brief,
        content=content,
        instruction=text,
        change_summary=summary,
        model_id=_model_label(raw, model_id),
        actor_email=actor_email,
    )
