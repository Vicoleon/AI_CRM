"""A voice command's EOS change → the request the page's own button sends.

``rubik_core/voice/commands.py`` reads what the person said into an action and
its words ("the voice rock", "Jordan", "Q4"). This module turns those words into
records the person can actually reach (the Rock they may update, the owner from
the same list the owner dropdown shows, a team they may file under) and returns
a :class:`Proposal`: the card the person confirms, and the exact request
(method, path, body) that makes the change.

NOTHING HERE WRITES. After the person confirms, the browser sends that request
to the page's own endpoint (``/api/eos/rocks/`` …) as the person, so the change
runs through the same permission checks and side effects as a click, and a
voice command can never do what the person's own button cannot. The lookups
below only decide what to offer: a Rock the person may not update is never
proposed, because the endpoint would refuse it anyway.

When the words fit more than one record (two Rocks about "voice", two people
called Jordan) the proposal carries one ready plan per match and asks which;
when they fit none, it says so instead of guessing.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, TypeVar

from sqlalchemy.orm import Session

from backend.models.eos.traction import truncate_todo_text
from backend.routers.eos._authz import (
    mutable_ids,
    person_by_email,
    rock_write_flags,
    scorecard_value_writable_ids,
    seat_for_person,
    visible_measurable_ids,
    visible_rock_ids,
    visible_todo_ids,
)
from backend.routers.eos.me import team_scopes_for
from backend.routers.eos.people import eligible_owner_people
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.eos import (
    Person,
    Quarter,
    Rock,
    ScorecardEntry,
    ScorecardMetric,
    Todo,
)
from rubik_core.database.eos._base import RockStatus, TodoStatus
from rubik_core.rag.config import slugify_department
from rubik_core.voice import commands as cmd

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ── What a proposal looks like ───────────────────────────────────────────────


@dataclass
class PlanField:
    label: str
    value: str


@dataclass
class ActionPlan:
    """One change, ready for the person to confirm and the browser to send."""

    action: str
    #: The card's heading: "Add a Rock".
    title: str
    fields: list[PlanField]
    method: str
    path: str
    body: dict[str, Any]
    #: Shown once it worked: "Rock added".
    done: str
    #: The confirm button: "Add Rock".
    confirm: str = "Confirm"
    #: Where the result lives, offered as a link afterwards.
    open_path: str | None = None
    open_label: str | None = None
    #: How this plan is named in a "which one?" list.
    option: str | None = None


@dataclass
class Proposal:
    plan: ActionPlan | None = None
    #: Several matches: one ready plan each, and the question to ask.
    options: list[ActionPlan] = field(default_factory=list)
    question: str | None = None
    #: Nothing can be proposed, and why, in words for the person.
    message: str | None = None


class _Refusal(Exception):
    """A reason nothing can be proposed, worded for the person."""


# ── Matching words to records ────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9]+")
# Words that name the kind of thing rather than which one ("the voice ROCK").
_FILLER = frozenset(
    {
        "a",
        "an",
        "the",
        "my",
        "our",
        "this",
        "that",
        "one",
        "of",
        "for",
        "about",
        "called",
        "named",
        "rock",
        "rocks",
        "to",
        "do",
        "todo",
        "todos",
        "measurable",
        "metric",
        "number",
        "scorecard",
    }
)


def _fold(text: str | None) -> str:
    """Lower case, accents off: "Renée" and "renee" are the same name."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _words(text: str | None) -> list[str]:
    return _WORD.findall(_fold(text))


def _said(value: Any) -> str:
    """A spoken argument as plain text, whatever type the model sent."""
    return " ".join(str(value).split()) if value is not None else ""


def _key_words(text: str | None) -> list[str]:
    return [w for w in _words(text) if w not in _FILLER]


def _close(a: str, b: str) -> bool:
    """Two words that mean the same thing as heard: plurals, a clipped ending."""
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return len(a) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.85


def similarity(words: str, title: str) -> float:
    """How well what was said names ``title``, 0..1.

    The share of the spoken words found in the title, so "the voice rock"
    names "Implement Cubie voice across Cubie" fully, blended with the plain
    string likeness for a title said nearly in full.
    """
    said = _key_words(words)
    have = _key_words(title)
    if not said or not have:
        return 0.0
    found = sum(1 for w in said if any(_close(w, h) for h in have)) / len(said)
    likeness = SequenceMatcher(None, " ".join(said), " ".join(have)).ratio()
    return max(found * 0.95, likeness)


MATCH_FLOOR = 0.5
# Matches this close to the best one are all offered, not just the best.
TIE_MARGIN = 0.12
MAX_OPTIONS = 4


def best_matches(
    words: str,
    items: Iterable[T],
    title_of: Callable[[T], str],
    *,
    prefer: Callable[[T], bool] | None = None,
) -> list[T]:
    """The records ``words`` names: one when it is clear, several when not.

    ``prefer`` breaks a tie (the speaker's own To-Do over a teammate's of the
    same name); it never adds a record that did not match.
    """
    scored = sorted(
        ((similarity(words, title_of(i)), n, i) for n, i in enumerate(items)),
        key=lambda s: (-s[0], s[1]),
    )
    scored = [s for s in scored if s[0] >= MATCH_FLOOR]
    if not scored:
        return []
    best = scored[0][0]
    close = [i for score, _, i in scored if score >= best - TIE_MARGIN]
    if prefer and len(close) > 1:
        preferred = [i for i in close if prefer(i)]
        if preferred:
            close = preferred
    return close[:MAX_OPTIONS]


def _full_name(person: Person) -> str:
    return " ".join(f"{person.first_name or ''} {person.last_name or ''}".split())


def match_people(words: str, people: Sequence[Person]) -> list[Person]:
    """Everyone ``words`` could name, best first: full name, then first or last
    name alone, then a near spelling. Several when the name is shared."""
    said = _words(words)
    if not said:
        return []
    joined = " ".join(said)
    exact = [p for p in people if " ".join(_words(_full_name(p))) == joined]
    if exact:
        return exact
    if len(said) == 1:
        firsts = [p for p in people if _words(p.first_name) == said]
        if firsts:
            return firsts
        lasts = [p for p in people if _words(p.last_name) == said]
        if lasts:
            return lasts
    # Every spoken word in the name, a lone letter as an initial
    # ("Jordan H" → Jordan Hayes).
    within = [
        p
        for p in people
        if all(
            any(
                _close(w, n) or (len(w) == 1 and n.startswith(w))
                for n in _words(_full_name(p))
            )
            for w in said
        )
    ]
    if within:
        return within
    near = [
        p
        for p in people
        if SequenceMatcher(None, joined, " ".join(_words(_full_name(p)))).ratio()
        >= 0.82
    ]
    return near


# ── Dates, quarters, numbers ─────────────────────────────────────────────────


def quarter_of(day: date) -> int:
    return (day.month - 1) // 3 + 1


def quarter_for(number: int, year: int | None, today: date) -> tuple[int, int]:
    """The quarter "Q<number>" means today: this year's, unless it is already
    over, in which case next year's ("Q1" said in October is next January)."""
    if year:
        return number, year
    return number, today.year if number >= quarter_of(today) else today.year + 1


def week_sunday(day: date) -> date:
    """The Sunday that starts ``day``'s scorecard week (see Scorecard.tsx)."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _show_day(day: date) -> str:
    return f"{day:%a}, {day:%b} {day.day}"


def parse_number(said: str | None) -> Decimal | None:
    """ "$12,500" → 12500, "92%" → 92, "4.5" → 4.5; None if it is not a number."""
    cleaned = re.sub(r"[\s,$%]", "", str(said or ""))
    if not re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _plain(number: Decimal) -> str:
    """12500 → "12500", 0.920 → "0.92": never an exponent, never a trailing 0."""
    return format(number.normalize(), "f")


def scorecard_storage_value(number: Decimal, value_type: str | None) -> str:
    """What to store for a spoken number — ``fromEditableScorecardValue``'s rule.

    A percent measurable stores a fraction, and people say percent points, so
    "92" (or "92%") is stored as "0.92". Everything else is stored as said.
    """
    if (value_type or "").lower() == "percent":
        return _plain(number / Decimal(100))
    return _plain(number)


def scorecard_display(stored: str | None, value_type: str | None) -> str:
    number = parse_number(stored)
    if number is None:
        return stored or "—"
    kind = (value_type or "").lower()
    if kind == "percent":
        return f"{_plain(number * Decimal(100))}%"
    whole = number == number.to_integral_value()
    if kind == "currency":
        return f"${number:,.0f}" if whole else f"${number:,.2f}"
    return f"{number:,.0f}" if whole else _plain(number)


# ── The speaker, their teams, the current quarter ────────────────────────────


@dataclass
class _Context:
    user: UserProfile
    db: Session
    today: date
    me: Person | None
    #: Who may be named as an owner: the owner dropdown's list, plus the
    #: speaker (who is always their own owner).
    people: list[Person]
    scopes: list[dict]
    quarter: Quarter | None

    def owns(self, record) -> bool:
        return self.me is not None and record.owner_id == self.me.id

    def owner_of(self, record) -> Person | None:
        by_id = {p.id: p for p in self.people}
        return by_id.get(record.owner_id) or self.db.get(Person, record.owner_id)

    def name(self, person: Person | None) -> str:
        if person is None:
            return "—"
        if self.me and person.id == self.me.id:
            return f"You ({_full_name(person)})"
        return _full_name(person)


def _context(user: UserProfile, db: Session, today: date) -> _Context:
    quarter = (
        db.query(Quarter)
        .filter(Quarter.is_current == True, Quarter.is_deleted == False)  # noqa: E712
        .first()
    )
    # The owner list FIRST: reading it brings eos_people up to date with the
    # users table (as the page's own owner dropdown does), so someone who has
    # never opened an EOS page still resolves as themselves just after.
    people = list(eligible_owner_people(db, user))
    me = person_by_email(db, user.email)
    if me and all(p.id != me.id for p in people):
        people.append(me)
    return _Context(
        user=user,
        db=db,
        today=today,
        me=me,
        people=people,
        scopes=team_scopes_for(db, user),
        quarter=quarter,
    )


def current_quarter_name(db: Session) -> str:
    quarter = (
        db.query(Quarter)
        .filter(Quarter.is_current == True, Quarter.is_deleted == False)  # noqa: E712
        .first()
    )
    return quarter.name if quarter else ""


def team_labels(user: UserProfile, db: Session) -> list[str]:
    return [s["label"] for s in team_scopes_for(db, user)]


_EXEC_WORDS = frozenset({"exec", "executive", "executives", "leadership", "et"})


def _team(ctx: _Context, said: Any, *, default: bool) -> tuple[dict, str]:
    """The team fields for a create body, and the team's name for the card.

    A named team must be one of the speaker's own (the create forms offer the
    same list). Unnamed, with ``default``: the first of them, which is what the
    To-Do and Issue forms pre-select (the Exec team, for an exec). Without
    ``default``: nothing, and the caller decides.
    """
    said = _said(said)
    if said:
        words = [w for w in _words(said) if w not in {"team", "the", "my", "our"}]
        slug = slugify_department(" ".join(words))
        for scope in ctx.scopes:
            if scope["kind"] == "exec" and set(words) & _EXEC_WORDS:
                return {"is_exec": True}, scope["label"]
            if scope["kind"] == "department" and slug in {
                slugify_department(scope["value"]),
                slugify_department(scope["label"]),
            }:
                return {"department_name": scope["value"]}, scope["label"]
        labels = ", ".join(s["label"] for s in ctx.scopes) or "none"
        raise _Refusal(
            f"You can't file to a team called “{said}”. Your teams: {labels}."
        )
    if not default:
        return {}, ""
    if not ctx.scopes:
        raise _Refusal(
            f"You aren't on an EOS team yet, so {PRODUCT_NAME} can't file it. Say which "
            "team, or ask an admin to add you to one."
        )
    first = ctx.scopes[0]
    if first["kind"] == "exec":
        return {"is_exec": True}, first["label"]
    return {"department_name": first["value"]}, first["label"]


def _owners(ctx: _Context, said: Any) -> list[Person]:
    """Who a new record is for: the speaker unless someone else was named."""
    said = _said(said)
    if not said or set(_words(said)) <= {"me", "myself", "i"}:
        if ctx.me is None:
            raise _Refusal(
                f"You're not set up in EOS yet, so {PRODUCT_NAME} can't make that yours. "
                "An admin can add you on the Accountability Chart."
            )
        return [ctx.me]
    found = match_people(said, ctx.people)
    if not found:
        raise _Refusal(
            f"I couldn't find “{said}” among the people you can assign in EOS."
        )
    return found[:MAX_OPTIONS]


def _choose(plans: list[ActionPlan], question: str) -> Proposal:
    if len(plans) == 1:
        return Proposal(plan=plans[0])
    return Proposal(options=plans, question=question)


def _title(args: dict, key: str, what: str, example: str) -> str:
    text = " ".join(str(args.get(key) or "").split()).strip(" .\"'“”")
    if not text:
        raise _Refusal(
            f"What should the {what} say? Try again with it, like “{example}”."
        )
    return text[0].upper() + text[1:]


# ── The six EOS changes ──────────────────────────────────────────────────────


def _add_rock(ctx: _Context, args: dict) -> Proposal:
    title = _title(args, "title", "Rock", "Add a Rock called Launch the client portal")
    number = args.get("quarter")
    body: dict[str, Any] = {"description": title}
    # 0 and "" are "not said": a forced function call fills optional numbers.
    if number not in (None, "", 0, "0"):
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = 0
        if not 1 <= number <= 4:
            raise _Refusal("A quarter is Q1 to Q4. Which quarter is this Rock for?")
        year_said = args.get("year")
        try:
            year_said = int(year_said) if year_said else None
        except (TypeError, ValueError):
            year_said = None
        q, year = quarter_for(number, year_said, ctx.today)
        if not ctx.today.year - 1 <= year <= ctx.today.year + 3:
            raise _Refusal(f"Q{q} {year} is too far out for a Rock.")
        body.update(quarter_number=q, quarter_year=year)
        quarter_name = f"Q{q} {year}"
    else:
        quarter_name = ctx.quarter.name if ctx.quarter else "This quarter"
    team_fields, team_name = _team(ctx, args.get("team"), default=False)
    body.update(team_fields)
    plans = []
    for owner in _owners(ctx, args.get("owner")):
        rock_team, rock_team_name = team_fields, team_name
        if not team_fields:
            rock_team, rock_team_name = _rock_team(ctx, owner)
        plans.append(
            ActionPlan(
                action=cmd.ADD_ROCK,
                title="Add a Rock",
                fields=[
                    PlanField("Rock", title),
                    PlanField("Quarter", quarter_name),
                    PlanField("Owner", ctx.name(owner)),
                    PlanField("Team", rock_team_name),
                ],
                method="POST",
                path="/api/eos/rocks/",
                body={**body, **rock_team, "owner_id": str(owner.id)},
                done="Rock added",
                confirm="Add Rock",
                open_path="/eos/rocks",
                open_label="Rocks",
                option=_full_name(owner),
            )
        )
    return _choose(plans, "Who is this Rock for?")


def _rock_team(ctx: _Context, owner: Person) -> tuple[dict, str]:
    """An unnamed team for a Rock, as the Rocks page would file it.

    The owner's seat department when they have one (sent as nothing: the
    endpoint applies that rule itself). Otherwise the speaker's team if they
    have exactly one, else a company-wide Rock, which is what the page's own
    form does for someone who files under several teams or none.
    """
    seat = seat_for_person(owner, ctx.db)
    if seat and seat.department_name:
        return {}, seat.department_name
    departments = [s for s in ctx.scopes if s["kind"] == "department"]
    if len(departments) == 1:
        return {"department_name": departments[0]["value"]}, departments[0]["label"]
    return {"visibility": "company"}, "Company-wide"


def _rock_title(rock: Rock) -> str:
    return rock.description or ""


def _todo_title(todo: Todo) -> str:
    return todo.description or ""


def _metric_title(metric: ScorecardMetric) -> str:
    return metric.name or ""


_STATUS_LABEL = {
    RockStatus.ON_TRACK.value: "On track",
    RockStatus.OFF_TRACK.value: "Off track",
    RockStatus.DONE.value: "Done",
}


def _status_value(rock: Rock) -> str:
    return getattr(rock.status, "value", rock.status) or ""


def _update_rock_status(ctx: _Context, args: dict) -> Proposal:
    status = str(args.get("status") or "")
    if status not in _STATUS_LABEL:
        raise _Refusal("A Rock can be on track, off track or done. Which is it?")
    said = _said(args.get("rock"))
    query = ctx.db.query(Rock).filter(Rock.is_deleted == False)  # noqa: E712
    visible = visible_rock_ids(ctx.user, ctx.db)
    if visible is not None:
        query = query.filter(Rock.id.in_(visible))
    rocks = query.all()
    writable = rock_write_flags(ctx.user, rocks, ctx.db)
    rocks = [r for r in rocks if writable.get(r.id, (False, False))[0]]
    # This quarter's Rocks first: "the voice rock" means the live one.
    this_quarter = [r for r in rocks if ctx.quarter and r.quarter_id == ctx.quarter.id]
    matches = best_matches(said, this_quarter, _rock_title, prefer=ctx.owns)
    if not matches:
        matches = best_matches(said, rocks, _rock_title, prefer=ctx.owns)
    if not matches:
        raise _Refusal(f"I couldn't find a Rock you can update that matches “{said}”.")
    plans = []
    for rock in matches:
        owner = ctx.owner_of(rock)
        was = _STATUS_LABEL.get(_status_value(rock), _status_value(rock))
        plans.append(
            ActionPlan(
                action=cmd.UPDATE_ROCK_STATUS,
                title="Update a Rock",
                fields=[
                    PlanField("Rock", rock.description),
                    PlanField("Owner", ctx.name(owner)),
                    PlanField("Status", f"{was} → {_STATUS_LABEL[status]}"),
                ],
                method="PATCH",
                path=f"/api/eos/rocks/{rock.id}",
                body={"status": status},
                done="Rock updated",
                confirm="Update Rock",
                open_path="/eos/rocks",
                open_label="Rocks",
                option=f"{rock.description} — {_full_name(owner) if owner else '?'}",
            )
        )
    return _choose(plans, "Which Rock did you mean?")


TODO_DAYS = 7


def _due(ctx: _Context, said: Any) -> date:
    """The day a To-Do is due: what was said, else a week out (EOS's rule)."""
    if said:
        try:
            day = date.fromisoformat(str(said)[:10])
        except ValueError:
            day = None
        if day and ctx.today - timedelta(days=1) <= day <= ctx.today + timedelta(
            days=366
        ):
            return day
    return ctx.today + timedelta(days=TODO_DAYS)


def _add_todo(ctx: _Context, args: dict) -> Proposal:
    # Capped as the endpoint will cap it, so the card shows what is saved.
    title = truncate_todo_text(
        _title(args, "title", "To-Do", "Add a To-Do to send Jordan the deck")
    )
    due = _due(ctx, args.get("due"))
    team_fields, team_name = _team(ctx, args.get("team"), default=True)
    plans = [
        ActionPlan(
            action=cmd.ADD_TODO,
            title="Add a To-Do",
            fields=[
                PlanField("To-Do", title),
                PlanField("Owner", ctx.name(owner)),
                PlanField("Due", _show_day(due)),
                PlanField("Team", team_name or "—"),
            ],
            method="POST",
            path="/api/eos/todos/",
            body={
                "description": title,
                "owner_id": str(owner.id),
                # The To-Dos page sends the picked day as UTC midnight.
                "due_date": f"{due.isoformat()}T00:00:00",
                **team_fields,
            },
            done="To-Do added",
            confirm="Add To-Do",
            open_path="/eos/todos",
            open_label="To-Dos",
            option=_full_name(owner),
        )
        for owner in _owners(ctx, args.get("owner"))
    ]
    return _choose(plans, "Who is this To-Do for?")


def _complete_todo(ctx: _Context, args: dict) -> Proposal:
    said = _said(args.get("todo"))
    query = ctx.db.query(Todo).filter(
        Todo.is_deleted == False,  # noqa: E712
        Todo.status == TodoStatus.PENDING,
    )
    visible = visible_todo_ids(ctx.user, ctx.db)
    if visible is not None:
        query = query.filter(Todo.id.in_(visible))
    todos = query.all()
    writable = mutable_ids(ctx.user, todos, ctx.db)
    todos = [t for t in todos if writable.get(t.id)]
    matches = best_matches(said, todos, _todo_title, prefer=ctx.owns)
    if not matches:
        raise _Refusal(
            f"I couldn't find an open To-Do you can update that matches “{said}”."
        )
    plans = []
    for todo in matches:
        owner = ctx.owner_of(todo)
        fields = [
            PlanField("To-Do", todo.description),
            PlanField("Owner", ctx.name(owner)),
        ]
        if todo.due_date:
            fields.append(PlanField("Due", _show_day(todo.due_date.date())))
        plans.append(
            ActionPlan(
                action=cmd.COMPLETE_TODO,
                title="Mark a To-Do done",
                fields=fields,
                method="PATCH",
                path=f"/api/eos/todos/{todo.id}",
                body={"status": TodoStatus.DONE.value},
                done="To-Do marked done",
                confirm="Mark done",
                open_path="/eos/todos",
                open_label="To-Dos",
                option=f"{todo.description} — {_full_name(owner) if owner else '?'}",
            )
        )
    return _choose(plans, "Which To-Do did you mean?")


def _add_issue(ctx: _Context, args: dict) -> Proposal:
    title = _title(
        args, "title", "Issue", "Add an Issue: the client portal keeps timing out"
    )
    details = " ".join(str(args.get("details") or "").split())
    priority = str(args.get("priority") or "low")
    if priority not in {"low", "medium", "high"}:
        priority = "low"
    team_fields, team_name = _team(ctx, args.get("team"), default=True)
    fields = [PlanField("Issue", title)]
    if details:
        fields.append(PlanField("Details", details))
    fields += [
        PlanField("Team", team_name or "—"),
        PlanField("Priority", priority.title()),
    ]
    return Proposal(
        plan=ActionPlan(
            action=cmd.ADD_ISSUE,
            title="Add an Issue",
            fields=fields,
            method="POST",
            path="/api/eos/issues/",
            # The Issues page sends the same fields; it sets no owner either.
            body={
                "title": title,
                "description": details or title,
                "priority": priority,
                "status": "open",
                **team_fields,
            },
            done="Issue added",
            confirm="Add Issue",
            open_path="/eos/issues",
            open_label="Issues",
        )
    )


def _record_scorecard_value(ctx: _Context, args: dict) -> Proposal:
    said = _said(args.get("measurable"))
    number = parse_number(args.get("value"))
    if number is None:
        raise _Refusal(
            f"“{args.get('value') or ''}” isn't a number {PRODUCT_NAME} can record. Say the number, like “42” or “92 percent”."
        )
    query = ctx.db.query(ScorecardMetric).filter(
        ScorecardMetric.is_deleted == False  # noqa: E712
    )
    visible = visible_measurable_ids(ctx.user, ctx.db)
    if visible is not None:
        query = query.filter(ScorecardMetric.id.in_(visible))
    metrics = query.all()
    # The same team-wide grant the upsert enforces, so voice offers exactly the
    # rows the Scorecard page would let this caller type into.
    writable = scorecard_value_writable_ids(ctx.user, metrics, ctx.db)
    metrics = [m for m in metrics if writable.get(m.id)]
    if _said(args.get("owner")):
        owner_ids = {p.id for p in _owners(ctx, args.get("owner"))}
        metrics = [m for m in metrics if m.owner_id in owner_ids]
    matches = best_matches(said, metrics, _metric_title, prefer=ctx.owns)
    if not matches:
        raise _Refusal(
            f"I couldn't find a measurable you can update that matches “{said}”."
        )
    # A weekly number only. A measurable kept every other week (or month) has
    # its own columns on the Scorecard page, and "this week" may not be one.
    weekly = [m for m in matches if (m.frequency or "weekly") == "weekly"]
    if not weekly:
        raise _Refusal(
            f"{matches[0].name} isn't recorded weekly. Record it on the Scorecard page."
        )
    matches = weekly
    sunday = week_sunday(ctx.today)
    if args.get("week") == "last_week":
        sunday -= timedelta(days=7)
    week_label = "Last week" if args.get("week") == "last_week" else "This week"
    plans = []
    for metric in matches:
        owner = ctx.owner_of(metric)
        stored = scorecard_storage_value(number, metric.value_type)
        fields = [
            PlanField("Measurable", metric.name),
            PlanField("Owner", ctx.name(owner)),
            PlanField("Week", f"{week_label} (from {_show_day(sunday)})"),
            PlanField("Value", scorecard_display(stored, metric.value_type)),
        ]
        existing = _existing_entry(ctx.db, metric.id, sunday)
        if existing is not None and (existing.value or "").strip():
            fields.append(
                PlanField(
                    "Replaces", scorecard_display(existing.value, metric.value_type)
                )
            )
        plans.append(
            ActionPlan(
                action=cmd.RECORD_SCORECARD_VALUE,
                title="Record a Scorecard number",
                fields=fields,
                method="POST",
                path="/api/eos/scorecard/entries/upsert",
                # A scorecard week IS its Sunday; the upsert matches on the day.
                body={
                    "metric_id": str(metric.id),
                    "week_start_date": f"{sunday.isoformat()}T00:00:00",
                    "value": stored,
                },
                done="Scorecard updated",
                confirm="Record it",
                open_path="/eos/scorecard",
                open_label="Scorecard",
                option=f"{metric.name} — {_full_name(owner) if owner else '?'}",
            )
        )
    return _choose(plans, "Which measurable did you mean?")


def _existing_entry(db: Session, metric_id, sunday: date) -> ScorecardEntry | None:
    start = datetime(sunday.year, sunday.month, sunday.day)
    return (
        db.query(ScorecardEntry)
        .filter(
            ScorecardEntry.metric_id == metric_id,
            ScorecardEntry.week_start_date >= start,
            ScorecardEntry.week_start_date < start + timedelta(days=1),
            ScorecardEntry.is_deleted == False,  # noqa: E712
        )
        .first()
    )


PLANNERS: dict[str, Callable[[_Context, dict], Proposal]] = {
    cmd.ADD_ROCK: _add_rock,
    cmd.UPDATE_ROCK_STATUS: _update_rock_status,
    cmd.ADD_TODO: _add_todo,
    cmd.COMPLETE_TODO: _complete_todo,
    cmd.ADD_ISSUE: _add_issue,
    cmd.RECORD_SCORECARD_VALUE: _record_scorecard_value,
}


def propose(
    action: str, args: dict, user: UserProfile, db: Session, *, today: date
) -> Proposal:
    """What ``action`` with ``args`` would change, for the person to confirm."""
    planner = PLANNERS.get(action)
    if planner is None:
        return Proposal(message=f"{PRODUCT_NAME} can't do that by voice yet.")
    from rubik_core.control.repository import module_enabled

    try:
        if not module_enabled("eos"):
            return Proposal(message="EOS is not enabled for this workspace.")
    except Exception:  # noqa: BLE001 — a failed control read cannot authorize EOS
        logger.warning("[voice] EOS module lookup failed", exc_info=True)
        return Proposal(message="EOS is not available right now.")
    try:
        return planner(_context(user, db, today), args or {})
    except _Refusal as refusal:
        return Proposal(message=str(refusal))
    except (TypeError, ValueError, AttributeError):
        # An argument of a shape no planner expects: say so, never a 500.
        logger.warning("[voice] could not plan %s from the model's arguments", action)
        return Proposal(
            message=f"{PRODUCT_NAME} couldn't read that command. Please try again."
        )


__all__ = [
    "ActionPlan",
    "PLANNERS",
    "PlanField",
    "Proposal",
    "best_matches",
    "current_quarter_name",
    "match_people",
    "parse_number",
    "propose",
    "quarter_for",
    "scorecard_display",
    "scorecard_storage_value",
    "similarity",
    "team_labels",
    "week_sunday",
]
