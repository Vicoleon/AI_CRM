"""One spoken (or typed) command → what the person wants Cubie to do.

One Gemini call reads the recording and answers with exactly one of a fixed set
of functions: open a page or a record, hand the words to Ask Cubie, one of the
EOS changes, or "not understood". The model never acts. It names the action and the words
that go with it ("the voice rock", "Jordan", "Q4"); turning those words into real
records, and the request that makes the change, is
``backend/services/voice_actions.py``, and the change is only sent by the
browser after the person confirms it.

Why function calling with every function forced (``mode=ANY``): the answer is
always one of the things Cubie can do, never prose to parse. Each function also
carries ``heard`` (the words as said), so the person sees what Cubie heard in
the same round trip, without a second transcription call.

The same guards as dictation apply (``transcribe.py``): a clip nobody spoke in
never reaches the model (the router checks the samples first), and words the
clip is too short to hold are thrown away as invented.

A page can only be opened from the list the browser sends, which is the
person's own menu: the model cannot invent a destination, and every page is
still gated by the server when it loads.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from google.genai import types as genai_types

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to, feature_context
from rubik_core.gemini_tuning import generation_config_for
from rubik_core.models import get_genai_client
from rubik_core.voice import COMMANDS_COST_FEATURE
from rubik_core.voice.transcribe import (
    _implausible,
    _profile_or_saved,
    _quiet_thinking,
    company_vocabulary,
)

if TYPE_CHECKING:
    from rubik_core.company.profile import CompanyProfile

logger = logging.getLogger(__name__)


class CommandFailed(Exception):
    """Vertex did not return a usable answer. Nothing was read."""


class CommandTimedOut(CommandFailed):
    """Vertex did not answer within ``VOICE_COMMAND_TIMEOUT_SECONDS``."""


# ── What the browser tells us ────────────────────────────────────────────────


@dataclass(frozen=True)
class Destination:
    """A page the person can open: a row of their own sidebar."""

    path: str
    label: str
    group: str = ""


@dataclass(frozen=True)
class CommandContext:
    speaker: str
    today: date
    current_quarter: str
    teams: Sequence[str] = ()
    destinations: Sequence[Destination] = ()
    page: str = ""
    people: Sequence[str] = ()
    #: The kinds of record this person may open by name, as (key, what it is)
    #: (``backend/services/voice_records.py``). Empty: no open_record.
    record_kinds: Sequence[tuple[str, str]] = ()


@dataclass(frozen=True)
class Command:
    """The function the model chose, its arguments, and the words it heard."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    heard: str = ""
    #: The words could not have been said in a clip that short: invented.
    no_speech: bool = False


# ── The functions the model chooses from ─────────────────────────────────────

OPEN_PAGE = "open_page"
OPEN_RECORD = "open_record"
ASK_CUBIE = "ask_cubie"
NOT_UNDERSTOOD = "not_understood"
ANSWER = "answer"

ADD_ROCK = "add_rock"
UPDATE_ROCK_STATUS = "update_rock_status"
ADD_TODO = "add_todo"
COMPLETE_TODO = "complete_todo"
ADD_ISSUE = "add_issue"
RECORD_SCORECARD_VALUE = "record_scorecard_value"

#: The EOS changes. Each has a planner in ``backend/services/voice_actions.py``
#: (a test pins that the two lists match).
EOS_ACTIONS: tuple[str, ...] = (
    ADD_ROCK,
    UPDATE_ROCK_STATUS,
    ADD_TODO,
    COMPLETE_TODO,
    ADD_ISSUE,
    RECORD_SCORECARD_VALUE,
)

_HEARD = {
    "type": "string",
    "description": (
        "The person's words exactly as they said them, with normal punctuation "
        "and the company's spellings. Leave out filler sounds (um, uh)."
    ),
}


def _owner(what: str) -> dict:
    return {
        "type": "string",
        "description": (
            f"The full name of the person the {what} is for, only when it is "
            "someone other than the speaker. Leave it out for the speaker "
            '("me", "my", "I").'
        ),
    }


_TEAM = {
    "type": "string",
    "description": (
        "The team, only when the person named one; must be one of the speaker's "
        "teams listed in the instructions."
    ),
}


def _eos_functions() -> list[dict]:
    return [
        {
            "name": ADD_ROCK,
            "description": "Add an EOS Rock: a priority to finish this quarter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": 'What the Rock is, as the person said it, without the words around it ("called", "for Q4").',
                    },
                    "owner": _owner("Rock"),
                    "quarter": {
                        "type": "integer",
                        "description": '1 to 4, only when the person named a quarter ("Q4", "next quarter").',
                    },
                    "year": {
                        "type": "integer",
                        "description": (
                            "Only when the person said a year, or named the quarter "
                            'relative to now ("next quarter", "last quarter"): '
                            "then the year that quarter falls in."
                        ),
                    },
                    "team": _TEAM,
                },
                "required": ["title"],
            },
        },
        {
            "name": UPDATE_ROCK_STATUS,
            "description": "Change the status of an existing Rock: on track, off track or done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rock": {
                        "type": "string",
                        "description": "The words the person used for the Rock.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["on_track", "off_track", "done"],
                    },
                },
                "required": ["rock", "status"],
            },
        },
        {
            "name": ADD_TODO,
            "description": (
                "Add an EOS To-Do: a one-week action item on a team's L10. Not "
                'the person\'s own My To-Dos list ("remind me", "add to my '
                f'personal to-dos") — that is {ASK_CUBIE}.'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "What has to be done, as the person said it.",
                    },
                    "owner": _owner("To-Do"),
                    "due": {
                        "type": "string",
                        "description": "YYYY-MM-DD, only when the person said when it is due.",
                    },
                    "team": _TEAM,
                },
                "required": ["title"],
            },
        },
        {
            "name": COMPLETE_TODO,
            "description": "Mark an existing EOS To-Do done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todo": {
                        "type": "string",
                        "description": "The words the person used for the To-Do.",
                    },
                },
                "required": ["todo"],
            },
        },
        {
            "name": ADD_ISSUE,
            "description": "Add an EOS Issue: something for the team to discuss and solve in its L10.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "A short headline for the Issue, in the person's words.",
                    },
                    "details": {
                        "type": "string",
                        "description": "Anything more the person said about it.",
                    },
                    "team": _TEAM,
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Only when the person said how urgent it is.",
                    },
                },
                "required": ["title"],
            },
        },
        {
            "name": RECORD_SCORECARD_VALUE,
            "description": "Record a week's number for a measurable on the EOS Scorecard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "measurable": {
                        "type": "string",
                        "description": "The words the person used for the measurable.",
                    },
                    "value": {
                        "type": "string",
                        "description": 'The number, written in digits as said ("42", "92%", "$12,500").',
                    },
                    "week": {
                        "type": "string",
                        "enum": ["this_week", "last_week"],
                        "description": "Leave it out for this week.",
                    },
                    "owner": _owner("measurable"),
                },
                "required": ["measurable", "value"],
            },
        },
    ]


def _open_record_function(record_kinds: Sequence[tuple[str, str]]) -> dict:
    kinds = "; ".join(f"{key}: {what}" for key, what in record_kinds)
    return {
        "name": OPEN_RECORD,
        "description": (
            "Open ONE specific record by what it is about, rather than a page: "
            '"open Globex\'s X-Ray", "open Thursday\'s Systems L10", '
            '"prep for my 2pm", "open the Globex proposal". Kinds: ' + kinds + "."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": [key for key, _ in record_kinds]},
                "name": {
                    "type": "string",
                    "description": (
                        "The words that pick the record (the company, team, meeting, "
                        "topic or person), without the words around them. "
                        'Leave it out for "my next …" or "my latest …".'
                    ),
                },
                "day": {
                    "type": "string",
                    "description": 'YYYY-MM-DD, only when the person named a day ("Thursday\'s", "tomorrow\'s").',
                },
                "time": {
                    "type": "string",
                    "description": 'HH:MM on a 24-hour clock, only when the person said a time ("my 2pm" is 14:00).',
                },
            },
            "required": ["kind"],
        },
    }


def build_functions(
    destinations: Sequence[Destination],
    record_kinds: Sequence[tuple[str, str]] = (),
) -> list[dict]:
    """Every function the model may call, each with ``heard`` first."""
    functions: list[dict] = []
    paths = sorted({d.path for d in destinations})
    if paths:
        functions.append(
            {
                "name": OPEN_PAGE,
                "description": (
                    f'Go to a page in {PRODUCT_NAME}: "open …", "take me to …", "show '
                    'me …", and "I want to <do something>" when one of the '
                    "pages is where that is done."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "enum": paths,
                            "description": "The page's path, from the list in the instructions.",
                        },
                    },
                    "required": ["destination"],
                },
            }
        )
    if record_kinds:
        functions.append(_open_record_function(record_kinds))
    functions.extend(_eos_functions())
    functions.append(
        {
            "name": ASK_CUBIE,
            "description": (
                f"Hand the words to Ask {PRODUCT_NAME}, the chat assistant. It answers "
                "questions about the person's work, email, calendar, meetings, "
                "customers, documents and the company's knowledge, and it can draft "
                "email, book meetings and keep the person's own My To-Dos "
                "list. Use it for every question, and for any request no other "
                "function covers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The person's request, in their words.",
                    },
                },
                "required": ["question"],
            },
        }
    )
    functions.append(
        {
            "name": NOT_UNDERSTOOD,
            "description": (
                f"Nobody asked {PRODUCT_NAME} for anything (background talk, a fragment), "
                "or the words are too unclear to act on."
            ),
            "parameters": {"type": "object", "properties": {}},
        }
    )
    return [_with_heard(f) for f in functions]


def _with_heard(function: dict) -> dict:
    params = dict(function["parameters"])
    params["properties"] = {"heard": _HEARD, **params.get("properties", {})}
    params["required"] = ["heard", *params.get("required", [])]
    return {**function, "parameters": params}


def _answer_functions() -> list[dict]:
    return [
        _with_heard(
            {
                "name": ANSWER,
                "description": "Whether the person said to go ahead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "answer": {
                            "type": "string",
                            "enum": ["yes", "no", "unclear"],
                            "description": (
                                'yes = go ahead ("yes", "yeah", "do it", '
                                '"correct"); no = don\'t ("no", "cancel", "stop", '
                                '"never mind"); unclear = anything else.'
                            ),
                        },
                    },
                    "required": ["answer"],
                },
            }
        )
    ]


# ── Instructions ─────────────────────────────────────────────────────────────

_MAX_PEOPLE = 300
_MAX_DESTINATIONS = 400

_INSTRUCTIONS = """You read one command a person gave {product}, the internal app of {company}, and call exactly one function for it. The company runs on EOS (Rocks, To-Dos, Issues, the Scorecard and weekly L10 meetings).

The person pressed a key and {how}. Choose:
- open_page to go to a page; pick it from the list below and never invent one.
- open_record (when offered) to open one specific record named by what it is about: a company's X-Ray, an L10, the prep for a meeting, a proposal, a past chat, a colleague's guide.
- an EOS function (add_rock, update_rock_status, add_todo, complete_todo, add_issue, record_scorecard_value) when they ask for that change. Nothing is changed until they confirm.
- {ask} for every question and for any other request.
- not_understood when nobody asked {product} for anything.

Rules:
- Never fill in a value the person did not say. Leave optional fields out instead.
- Keep titles in the person's words; drop the words around them ("add a Rock called", "please"). Capitalize the first word. No quotation marks.
- Write numbers as digits.
- Today is {weekday}, {today}. Write dates as YYYY-MM-DD. The current EOS quarter is {quarter}.
- The speaker is {speaker}. Their EOS teams: {teams}.
- They are on the page: {page}.

Pages they can open (path — name):
{destinations}

Spell these the way {company_name} writes them when you hear them:
{vocabulary}

People at {company_name}:
{people}"""

_CONFIRM_INSTRUCTIONS = """{product} showed a person this change and asked them to confirm it: {question}

They pressed a key and {how}. Call the answer function: did they tell {product} to go ahead?"""


def _how(spoken: bool) -> str:
    return (
        "said it out loud (the recording is attached)" if spoken else "typed it: {text}"
    )


def build_instructions(
    context: CommandContext,
    *,
    spoken: bool,
    text: str = "",
    profile: CompanyProfile | None = None,
) -> str:
    """The command reader's instructions. ``profile`` defaults to the saved one.

    With no profile saved the company is "the company" throughout; with one,
    its name (and industry, in the opening line) is used everywhere.
    """
    names = sorted(
        {" ".join(str(p).split()) for p in context.people if p and str(p).strip()}
    )
    rows = [
        f"- {d.path} — {d.label}"
        + (f" ({d.group})" if d.group and d.group != d.label else "")
        for d in list(context.destinations)[:_MAX_DESTINATIONS]
    ]
    from rubik_core.company.profile import company_label, company_name_or_default

    current = _profile_or_saved(profile)
    return _INSTRUCTIONS.format(
        product=PRODUCT_NAME,
        ask=ASK_CUBIE,
        company=company_label(current),
        company_name=company_name_or_default(current),
        how=_how(spoken).format(text=text),
        weekday=context.today.strftime("%A"),
        today=context.today.isoformat(),
        quarter=context.current_quarter or "unknown",
        speaker=context.speaker or "unknown",
        teams=", ".join(context.teams) or "(none)",
        page=context.page or "unknown",
        destinations="\n".join(rows) or "(none)",
        vocabulary=", ".join(company_vocabulary(current)),
        people=", ".join(names[:_MAX_PEOPLE]) or "(none listed)",
    )


def command_model() -> str:
    return settings.VOICE_COMMAND_MODEL or settings.VERTEX_LLM_MODEL_FLASH


# ── The call ─────────────────────────────────────────────────────────────────


def _declarations(functions: list[dict]) -> list[genai_types.FunctionDeclaration]:
    return [
        genai_types.FunctionDeclaration(
            name=f["name"],
            description=f["description"],
            parameters_json_schema=f["parameters"],
        )
        for f in functions
    ]


def read_response(
    response, *, allowed: set[str], clip_seconds: float | None = None
) -> Command:
    """The first function call in ``response``, with ``heard`` split out.

    Raises :class:`CommandFailed` when the model answered with no call or with
    a function it was not offered.
    """
    calls = list(getattr(response, "function_calls", None) or [])
    if not calls:
        raise CommandFailed("the model did not choose a function")
    call = calls[0]
    name = str(getattr(call, "name", "") or "")
    if name not in allowed:
        raise CommandFailed(f"the model chose an unknown function: {name[:60]}")
    args = dict(getattr(call, "args", None) or {})
    heard = " ".join(str(args.pop("heard", "") or "").split())
    if clip_seconds is not None and _implausible(heard, clip_seconds):
        # Counts only: the words themselves are never logged.
        logger.warning(
            "[voice] discarded an invented command: %d words for %.1fs of audio",
            len(heard.split()),
            clip_seconds,
        )
        return Command(name=NOT_UNDERSTOOD, heard="", no_speech=True)
    return Command(name=name, args=args, heard=heard)


async def _call(
    *,
    instructions: str,
    functions: list[dict],
    audio: bytes | None,
    mime_type: str,
    user_email: str | None,
    clip_seconds: float | None,
) -> Command:
    model_id = command_model()
    contents: list = []
    if audio is not None:
        contents.append(genai_types.Part.from_bytes(data=audio, mime_type=mime_type))
    contents.append(instructions)
    limit = settings.VOICE_COMMAND_TIMEOUT_SECONDS
    try:
        from rubik_core.ai_providers.registry import resolve_model_route
        from rubik_core.ai_providers.runtime import (
            generate_native_content_async_on_route,
        )

        with feature_context(COMMANDS_COST_FEATURE, user_email=user_email):
            capability = "audio_tools" if audio is not None else "native_tools"
            route = await asyncio.to_thread(
                resolve_model_route,
                "voice_command_audio" if audio is not None else "voice_command_text",
                capability,
                default_model=model_id,
            )
            config = genai_types.GenerateContentConfig(
                temperature=0.0,
                tools=[
                    genai_types.Tool(function_declarations=_declarations(functions))
                ],
                tool_config=genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode="ANY"
                    )
                ),
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
                thinking_config=_quiet_thinking(route.model),
            )
            response = await asyncio.wait_for(
                get_genai_client().aio.models.generate_content(
                    model=route.model,
                    contents=contents,
                    config=generation_config_for(route.model, config),
                )
                if route.provider == "vertex_managed"
                else generate_native_content_async_on_route(
                    route,
                    contents,
                    capability=capability,
                    config=generation_config_for(route.model, config),
                ),
                timeout=limit,
            )
    except asyncio.TimeoutError as exc:
        logger.warning("[voice] command call took over %ss (%s)", limit, model_id)
        raise CommandTimedOut(f"no answer within {limit}s") from exc
    except Exception:  # noqa: BLE001 — surfaced as one typed error
        logger.warning("[voice] command call failed")
        raise CommandFailed(
            "AI voice command unavailable. Check the workspace model route."
        ) from None
    return read_response(
        response,
        allowed={f["name"] for f in functions},
        clip_seconds=clip_seconds if audio is not None else None,
    )


@attribute_to(COMMANDS_COST_FEATURE)
async def read_command(
    context: CommandContext,
    *,
    audio: bytes | None = None,
    text: str = "",
    mime_type: str = "audio/wav",
    user_email: str | None = None,
    clip_seconds: float | None = None,
) -> Command:
    """Read one command from a recording (or typed ``text``).

    The caller must already have checked that someone speaks in ``audio``
    (:func:`rubik_core.voice.signal.heard_speech`): the model will not say so.
    """
    spoken = audio is not None
    return await _call(
        instructions=build_instructions(context, spoken=spoken, text=text),
        functions=build_functions(context.destinations, context.record_kinds),
        audio=audio,
        mime_type=mime_type,
        user_email=user_email,
        clip_seconds=clip_seconds,
    )


@attribute_to(COMMANDS_COST_FEATURE)
async def read_confirmation(
    question: str,
    *,
    audio: bytes | None = None,
    text: str = "",
    mime_type: str = "audio/wav",
    user_email: str | None = None,
    clip_seconds: float | None = None,
) -> Command:
    """Did the person say yes to ``question``? ``args["answer"]`` is yes/no/unclear."""
    spoken = audio is not None
    instructions = _CONFIRM_INSTRUCTIONS.format(
        product=PRODUCT_NAME,
        question=" ".join(question.split())[:500] or f"(a change in {PRODUCT_NAME})",
        how=_how(spoken).format(text=text),
    )
    return await _call(
        instructions=instructions,
        functions=_answer_functions(),
        audio=audio,
        mime_type=mime_type,
        user_email=user_email,
        clip_seconds=clip_seconds,
    )
