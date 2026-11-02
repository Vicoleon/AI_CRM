"""Voice commands — ``POST /api/voice/commands``.

Gate, per the repo convention: ``VOICE_COMMANDS_ENABLED`` off answers 404, a
person outside the ``voice_commands`` roster answers 403. Mounted on ``core``
in ``backend/main.py``, so the copilot pilot gate applies first.

One request is one command: a recording (or typed ``text``, which takes the
same path), the pages the person may open (their own sidebar, sent by the
browser) and the page they are on. The answer says what to do next:

* ``navigate`` — open ``destination``, one of the pages that were sent;
* ``open`` — open a specific record (``places``);
* ``ask`` — hand ``ask`` to Ask Cubie as the person's question;
* ``action`` — a card for the person to confirm (``plan``), or several when the
  words fit more than one record (``options`` and ``prompt``), each carrying
  the exact request the browser sends once the person says yes
  (``backend/services/voice_actions.py``);
* ``message`` — nothing to do, and why (with ``ask`` set when the words could
  still go to Ask Cubie);
* ``none`` — nobody spoke.

With ``expect=confirm`` the recording is the person's answer to the card named
in ``question``, and the reply is ``answer``: ``yes``, ``no`` or ``unclear``.

NOTHING IS WRITTEN HERE. A change is made by the browser, after the person
confirms, through the page's own endpoint, as the person. Like dictation, a
recording nobody spoke in never reaches the model, the audio is never stored,
and the words are never logged (only which action was chosen).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user
from backend.routers.voice import people_names, read_upload
from backend.services import voice_actions, voice_records
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows
from rubik_core.promises.store import local_today, owner_timezone
from rubik_core.voice import commands as cmd
from rubik_core.voice.signal import heard_speech, to_wav

logger = logging.getLogger(__name__)

FEATURE_KEY = "voice_commands"
MAX_TEXT = 500
MAX_DESTINATIONS = 400


def require_voice_commands(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """Gate for the whole router — 404 when switched off, 403 when not on it."""
    if not settings.VOICE_COMMANDS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows(FEATURE_KEY, user):
        raise HTTPException(
            status_code=403,
            detail="Voice commands are not turned on for you. Ask your admin.",
        )
    return user


router = APIRouter(
    prefix="/voice/commands",
    tags=["voice"],
    dependencies=[Depends(require_voice_commands)],
)


class DestinationIn(BaseModel):
    # An in-app path: one leading slash, never "//host" (another site).
    path: str = Field(min_length=1, max_length=300, pattern=r"^/([^/]|$)")
    label: str = Field(min_length=1, max_length=120)
    group: str = Field(default="", max_length=120)


_DESTINATIONS = TypeAdapter(list[DestinationIn])


class FieldOut(BaseModel):
    label: str
    value: str


class RequestOut(BaseModel):
    method: Literal["POST", "PATCH", "PUT"]
    path: str
    body: dict[str, Any]


class PlanOut(BaseModel):
    action: str
    title: str
    fields: list[FieldOut]
    request: RequestOut
    done: str
    confirm: str
    open_path: str | None = None
    open_label: str | None = None
    option: str | None = None


class PlaceOut(BaseModel):
    """A specific record's page (an X-Ray, an L10, a proposal …)."""

    path: str
    label: str
    detail: str = ""
    state: dict[str, Any] | None = None


class CommandOut(BaseModel):
    kind: Literal["navigate", "open", "ask", "action", "message", "none", "answer"]
    heard: str = ""
    destination: DestinationIn | None = None
    #: kind="open": where the named record is; several → "which one?".
    places: list[PlaceOut] = []
    #: kind="open": ask even for one place (opening an L10 starts it).
    ask_first: bool = False
    ask: str | None = None
    plan: PlanOut | None = None
    options: list[PlanOut] = []
    prompt: str | None = None
    message: str | None = None
    answer: Literal["yes", "no", "unclear"] | None = None


# Where a confirmed card may send its request. Every plan path is built by a
# planner from a constant and a record id, never from the model's words; this
# pins it anyway, so a new planner cannot hand the browser any other address.
PLAN_PATH_PREFIXES = ("/api/eos/",)


def plan_path_allowed(path: str) -> bool:
    return path.startswith(PLAN_PATH_PREFIXES)


def _plan_out(plan: voice_actions.ActionPlan) -> PlanOut:
    if not plan_path_allowed(plan.path):
        raise ValueError(f"a voice plan may not send to {plan.path!r}")
    return PlanOut(
        action=plan.action,
        title=plan.title,
        fields=[FieldOut(label=f.label, value=f.value) for f in plan.fields],
        request=RequestOut(method=plan.method, path=plan.path, body=plan.body),
        done=plan.done,
        confirm=plan.confirm,
        open_path=plan.open_path,
        open_label=plan.open_label,
        option=plan.option,
    )


def _parse_destinations(raw: str) -> list[cmd.Destination]:
    try:
        rows = _DESTINATIONS.validate_json(raw or "[]")
    except ValidationError:
        raise HTTPException(
            status_code=422, detail=f"{PRODUCT_NAME} couldn't read the list of pages."
        ) from None
    seen: set[tuple[str, str]] = set()
    out: list[cmd.Destination] = []
    for row in rows[:MAX_DESTINATIONS]:
        key = (row.path, row.label)
        if key not in seen:
            seen.add(key)
            out.append(cmd.Destination(path=row.path, label=row.label, group=row.group))
    return out


def _context(
    db: Session,
    user: UserProfile,
    destinations: list[cmd.Destination],
    page: str,
) -> tuple[cmd.CommandContext, Any, str]:
    """What the model is told about the speaker, their own today, and their
    timezone."""
    from rubik_core.database.eos import Person

    tz = owner_timezone(db, user.email)
    today = local_today(tz)
    people = list(people_names(db))
    try:
        people += [
            f"{first} {last}"
            for first, last in db.query(Person.first_name, Person.last_name)
            .filter(Person.is_deleted == False)  # noqa: E712
            .all()
        ]
    except Exception:  # noqa: BLE001 — spelling hints only
        # A failed query leaves the transaction aborted (Postgres); roll it
        # back so the reads below still work.
        db.rollback()
        logger.warning("[voice] could not load EOS names for hints", exc_info=True)
    context = cmd.CommandContext(
        speaker=user.name or user.email,
        today=today,
        current_quarter=voice_actions.current_quarter_name(db),
        teams=voice_actions.team_labels(user, db),
        destinations=destinations,
        page=page[:300],
        people=people,
        record_kinds=[
            (k.key, k.description) for k in voice_records.available_kinds(user)
        ],
    )
    # Reads only: end the transaction rather than hold it open across the
    # model call.
    db.rollback()
    return context, today, tz


def _proposal_out(heard: str, proposal: voice_actions.Proposal) -> CommandOut:
    if proposal.message:
        return CommandOut(kind="message", heard=heard, message=proposal.message)
    return CommandOut(
        kind="action",
        heard=heard,
        plan=_plan_out(proposal.plan) if proposal.plan else None,
        options=[_plan_out(p) for p in proposal.options],
        prompt=proposal.question,
    )


def _outcome(
    command: cmd.Command,
    destinations: list[cmd.Destination],
    user: UserProfile,
    db: Session,
    today,
    tz: str,
) -> CommandOut:
    heard = command.heard
    if command.no_speech:
        return CommandOut(kind="none")
    if command.name == cmd.OPEN_PAGE:
        path = str(command.args.get("destination") or "")
        match = next((d for d in destinations if d.path == path), None)
        if match is None:
            return CommandOut(
                kind="message",
                heard=heard,
                message="That page isn't one you can open.",
            )
        return CommandOut(
            kind="navigate",
            heard=heard,
            destination=DestinationIn(
                path=match.path, label=match.label, group=match.group
            ),
        )
    if command.name == cmd.OPEN_RECORD:
        opening = voice_records.open_record(command.args, user, db, today=today, tz=tz)
        if opening.message:
            return CommandOut(kind="message", heard=heard, message=opening.message)
        return CommandOut(
            kind="open",
            heard=heard,
            places=[
                PlaceOut(path=p.path, label=p.label, detail=p.detail, state=p.state)
                for p in opening.places
                if voice_records.place_allowed(p.path)
            ],
            prompt=opening.prompt,
            ask_first=opening.ask_first,
        )
    if command.name == cmd.ASK_CUBIE:
        question = " ".join(str(command.args.get("question") or "").split()) or heard
        return CommandOut(kind="ask", heard=heard, ask=question or None)
    if command.name in cmd.EOS_ACTIONS:
        return _proposal_out(
            heard,
            voice_actions.propose(command.name, command.args, user, db, today=today),
        )
    # not_understood
    return CommandOut(
        kind="message",
        heard=heard,
        message=f"I didn't catch something {PRODUCT_NAME} can do there.",
        ask=heard or None,
    )


@router.post("", response_model=CommandOut)
async def run_command(
    audio: UploadFile | None = File(None),
    text: str = Form(""),
    destinations: str = Form("[]"),
    page: str = Form(""),
    expect: Literal["command", "confirm"] = Form("command"),
    question: str = Form(""),
    user: UserProfile = Depends(require_voice_commands),
    db: Session = Depends(get_db),
) -> CommandOut:
    typed = " ".join(text.split())[:MAX_TEXT]
    wav: bytes | None = None
    seconds: float | None = None
    if audio is not None:
        limit = settings.VOICE_COMMAND_MAX_SECONDS
        clip = await read_upload(
            audio,
            max_seconds=limit,
            too_long=f"That was longer than {limit} seconds. Try a shorter command.",
        )
        if not heard_speech(clip):
            # No model call, so no cost and no invented command.
            return CommandOut(kind="none")
        wav, seconds = to_wav(clip), clip.seconds
    elif not typed:
        raise HTTPException(status_code=422, detail="Say or type a command.")

    try:
        if expect == "confirm":
            command = await cmd.read_confirmation(
                question,
                audio=wav,
                text=typed,
                user_email=user.email,
                clip_seconds=seconds,
            )
            if command.no_speech:
                return CommandOut(kind="none")
            answer = str(command.args.get("answer") or "unclear")
            return CommandOut(
                kind="answer",
                heard=command.heard,
                answer=answer if answer in {"yes", "no"} else "unclear",
            )

        pages = _parse_destinations(destinations)
        context, today, tz = await run_in_threadpool(_context, db, user, pages, page)
        command = await cmd.read_command(
            context,
            audio=wav,
            text=typed,
            user_email=user.email,
            clip_seconds=seconds,
        )
    except cmd.CommandTimedOut:
        raise HTTPException(
            status_code=504,
            detail=f"{PRODUCT_NAME} took too long to work out that command. Please try again.",
        ) from None
    except cmd.CommandFailed:
        raise HTTPException(
            status_code=502,
            detail=f"{PRODUCT_NAME} couldn't work out that command. Please try again.",
        ) from None
    logger.info("[voice] command: %s", command.name)
    return await run_in_threadpool(_outcome, command, pages, user, db, today, tz)
