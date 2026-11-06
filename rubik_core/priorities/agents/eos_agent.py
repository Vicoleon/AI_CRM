"""EOS (Traction) channel agent — internal execution commitments.

Feeds the calling user's own — and, for Team Managers / execs, their team's —
EOS Rocks / To-Dos / Issues / Milestones into the AI-Priorities pipeline as
``ChannelSignal`` objects tagged with ``entity_kind`` ROCK / TODO / ISSUE /
MILESTONE, so the decision-maker can correlate an EOS commitment with the
emails, meetings, and deals that touch the same work.

This agent is a pure extractor — no judgment, no urgency, no "flag". EOS rows
are already fully structured: the deterministic Python here decides WHICH rows
are worth surfacing (due-soon / overdue To-Dos & Milestones, off-track or stale
Rocks, open Issues) and populates every attr; the single LLM call only composes
the one-sentence ``summary``.

Ownership scoping ("own + team") is delegated to the EOS row-level access
control the app already uses:

  * ``_compute_scope(user, db).owner_pids`` — the caller's own Person id, their
    direct reports, and (for Team Managers / execs) every person on the team(s)
    they lead.
  * ``visible_*_ids(user, db)`` — the RLAC visibility ceiling (``None`` when the
    caller is unrestricted, else the exact id set they may read).

Every entity is filtered by ``owner_id IN owner_pids`` AND (``id IN visible_ids``
when the ceiling is not ``None``). Intersecting the ownership set with the
visibility ceiling gives own+team with zero exec-scope leak — a non-exec Team
Manager never sees a confidential Exec-team row they don't personally own.

Mirrors ``hubspot_agent.py``: a structured, non-Workspace source that stamps a
domain entity per row and uses an extractor-only ``build_prompt``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from rubik_core.priorities.agents.base import BaseChannelAgent
from rubik_core.priorities.models import (
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PriorityKind,
)

logger = logging.getLogger(__name__)


class EosChannelAgent(BaseChannelAgent):
    channel = ChannelKind.EOS

    # Total signals emitted per run — bounded so token cost stays predictable.
    MAX_SIGNALS_OUT = 40
    # A To-Do / Milestone is "due soon" when its deadline lands within this
    # window; anything already past is overdue (more urgent, never dropped).
    DUE_SOON_DAYS = 7
    # A Rock with no weekly update in this many days is "stale" and warrants
    # attention even if its cached status still reads on-track.
    STALE_ROCK_DAYS = 10
    # Open Issues can be numerous; cap them (highest priority first) so they
    # don't crowd out the deadline-bearing signals.
    MAX_ISSUES_OUT = 10

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_signals(self) -> list[dict]:
        """Deterministically pull the caller's own+team EOS work worth surfacing.

        Resilient by contract: any DB / import failure logs a warning and
        returns ``[]`` so a single bad channel never breaks the bundle. Each
        entity block is additionally isolated so a failure in one source (e.g.
        the still-landing Milestone table) can't drop the others.
        """
        try:
            return self._fetch()
        except Exception as exc:  # noqa: BLE001 — pipeline must stay alive
            logger.warning("EOS fetch failed for %s: %s", self.user_email, exc)
            return []

    def _fetch(self) -> list[dict]:
        from sqlalchemy import func, or_

        from backend.routers.eos._authz import (
            _compute_scope,
            person_by_email,
            visible_issue_ids,
            visible_rock_ids,
            visible_todo_ids,
        )
        from rubik_core.auth.registry import UserRegistry
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.eos import (
            Issue,
            IssueStatus,
            Person,
            Rock,
            RockStatus,
            RockWeeklyUpdate,
            Todo,
            TodoStatus,
        )

        # A NEW child table another change is landing concurrently — import it
        # defensively and skip Milestones entirely if it isn't there yet.
        try:
            from rubik_core.database.eos.rocks import RockMilestone
        except Exception:  # noqa: BLE001
            RockMilestone = None

        user = UserRegistry().get_user(self.user_email)
        if user is None:
            logger.info("EOS agent: no user profile for %s; skipping", self.user_email)
            return []

        db = SessionLocal()
        try:
            scope = _compute_scope(user, db)
            owner_pids = list(scope.owner_pids)
            if not owner_pids:
                # No EOS person/seat and no team → nothing owned to surface.
                return []

            vis_todo = visible_todo_ids(user, db)
            vis_rock = visible_rock_ids(user, db)
            vis_issue = visible_issue_ids(user, db)

            # The caller's OWN person id (for the is_own flag). Same lookup the
            # authz layer uses: soft, case-insensitive email match on
            # eos_people (see ``_authz.person_by_email``).
            own_person = person_by_email(db, user.email)
            own_pid = own_person.id if own_person is not None else None

            # Exec-team membership is the ONE gate for Exec-tagged content in the
            # AI feed. Being a workspace *admin* (roles=["admin"]) does NOT count:
            # ``visible_*_ids`` return ``None`` for admins (see ``_authz.
            # _is_unrestricted``), so the RLAC ceiling below is disabled for them
            # and the ``is_exec`` filter in ``_scoped`` is the only thing that
            # stops an Exec-team Rock/Issue/To-Do from surfacing on a non-exec
            # admin's briefing. Invariant: nothing tagged Exec team ever reaches
            # someone outside the Exec team in AI Priorities.
            caller_is_exec = bool(getattr(user, "is_exec", False))

            now = datetime.utcnow()
            today = now.date()
            horizon = now + timedelta(days=self.DUE_SOON_DAYS)
            stale_cutoff = today - timedelta(days=self.STALE_ROCK_DAYS)

            def _scoped(query, model, vis_ids):
                """own+team ownership AND the RLAC visibility ceiling, with a
                hard Exec-team fence for non-exec callers (admins included)."""
                query = query.filter(model.owner_id.in_(owner_pids))
                if vis_ids is not None:
                    query = query.filter(model.id.in_(vis_ids))
                if not caller_is_exec:
                    # Drop Exec-tagged rows outright — never surface them to a
                    # non-exec user even when they own the row or an admin's
                    # ceiling is open. Milestones inherit this via ``rock_by_id``
                    # (their parent Rock is fetched through ``_scoped``).
                    query = query.filter(model.is_exec.is_(False))
                return query

            records: list[dict] = []

            # -- To-Dos: pending, due-soon / overdue / no-due-date ------------
            try:
                todos = (
                    _scoped(
                        db.query(Todo).filter(
                            Todo.is_deleted.is_(False),
                            Todo.status == TodoStatus.PENDING,
                        ),
                        Todo,
                        vis_todo,
                    )
                    .filter(or_(Todo.due_date.is_(None), Todo.due_date <= horizon))
                    # Defensive DB-side bound: a manager/exec's ownership set can
                    # be large. Soonest-due (and overdue) first, no-due-date
                    # last, capped well above MAX_SIGNALS_OUT so the in-memory
                    # cap still decides output while the fetch stays bounded.
                    .order_by(Todo.due_date.is_(None), Todo.due_date.asc())
                    .limit(self.MAX_SIGNALS_OUT * 5)
                    .all()
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EOS To-Do fetch failed for %s: %s", self.user_email, exc
                )
                todos = []

            # -- Rocks: not done, off-track OR stale --------------------------
            all_scoped_rocks: list = []
            try:
                all_scoped_rocks = (
                    _scoped(
                        db.query(Rock).filter(Rock.is_deleted.is_(False)),
                        Rock,
                        vis_rock,
                    )
                    # Defensive bound for very large teams (see To-Do note).
                    .limit(self.MAX_SIGNALS_OUT * 10)
                    .all()
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("EOS Rock fetch failed for %s: %s", self.user_email, exc)
                all_scoped_rocks = []

            rock_by_id = {r.id: r for r in all_scoped_rocks}

            # Latest weekly-update date per scoped rock (for the stale check).
            latest_update: dict[Any, date] = {}
            if all_scoped_rocks:
                try:
                    rows = (
                        db.query(
                            RockWeeklyUpdate.rock_id,
                            func.max(RockWeeklyUpdate.update_date),
                        )
                        .filter(RockWeeklyUpdate.rock_id.in_(list(rock_by_id)))
                        .group_by(RockWeeklyUpdate.rock_id)
                        .all()
                    )
                    latest_update = {rid: d for rid, d in rows if d is not None}
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "EOS Rock update history failed for %s: %s",
                        self.user_email,
                        exc,
                    )
                    latest_update = {}

            attention_rocks = []
            for r in all_scoped_rocks:
                if r.status == RockStatus.DONE:
                    continue
                last = latest_update.get(r.id)
                is_stale = last is None or last < stale_cutoff
                is_off_track = r.status == RockStatus.OFF_TRACK
                if is_off_track or is_stale:
                    attention_rocks.append((r, is_off_track, is_stale, last))

            # -- Milestones: incomplete, on a scoped rock, due-soon / overdue -
            milestones: list = []
            if RockMilestone is not None and rock_by_id:
                try:
                    milestones = (
                        db.query(RockMilestone)
                        .filter(
                            RockMilestone.completed.is_(False),
                            RockMilestone.rock_id.in_(list(rock_by_id)),
                            RockMilestone.due_date.isnot(None),
                            RockMilestone.due_date <= horizon,
                        )
                        .all()
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "EOS Milestone fetch failed for %s: %s", self.user_email, exc
                    )
                    milestones = []

            # -- Issues: open, top MAX_ISSUES_OUT by priority -----------------
            try:
                issues = _scoped(
                    db.query(Issue).filter(
                        Issue.is_deleted.is_(False),
                        Issue.status == IssueStatus.OPEN,
                    ),
                    Issue,
                    vis_issue,
                ).all()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EOS Issue fetch failed for %s: %s", self.user_email, exc
                )
                issues = []

            _issue_rank = {"high": 0, "medium": 1, "low": 2}
            issues.sort(key=lambda i: _issue_rank.get(_enum_value(i.priority), 3))
            issues = issues[: self.MAX_ISSUES_OUT]

            # -- Resolve owners in one batch ----------------------------------
            owner_ids: set = set()
            for t in todos:
                owner_ids.add(t.owner_id)
            for i in issues:
                if i.owner_id is not None:
                    owner_ids.add(i.owner_id)
            for r, *_ in attention_rocks:
                owner_ids.add(r.owner_id)
            for m in milestones:
                parent = rock_by_id.get(m.rock_id)
                if parent is not None:
                    owner_ids.add(parent.owner_id)

            person_by_id: dict[Any, Person] = {}
            owner_ids.discard(None)
            if owner_ids:
                for p in db.query(Person).filter(Person.id.in_(list(owner_ids))).all():
                    person_by_id[p.id] = p

            def _owner_bits(owner_id):
                p = person_by_id.get(owner_id) if owner_id is not None else None
                if p is None:
                    return None, None
                name = f"{p.first_name or ''} {p.last_name or ''}".strip() or None
                email = (p.email or "").strip().lower() or None
                return email, name

            def _due_iso(value) -> str | None:
                if value is None:
                    return None
                if isinstance(value, datetime):
                    return value.date().isoformat()
                if isinstance(value, date):
                    return value.isoformat()
                return None

            def _due_context(due_iso: str | None) -> str:
                if not due_iso:
                    return "no due date"
                try:
                    d = date.fromisoformat(due_iso)
                except ValueError:
                    return f"due {due_iso}"
                if d < today:
                    return f"overdue (was due {due_iso})"
                return f"due {due_iso}"

            def _add(
                *,
                kind: str,
                entity_kind: EntityKind,
                priority_kind: PriorityKind,
                entity_id,
                description: str,
                status: str,
                owner_id,
                due_value,
                created_at,
                extra: dict,
                sort_key: tuple,
            ) -> None:
                desc = (description or "").strip()
                due_iso = _due_iso(due_value)
                owner_email, owner_name = _owner_bits(owner_id)
                is_own = own_pid is not None and owner_id == own_pid

                snippet_bits = []
                if owner_name:
                    snippet_bits.append(f"Owner: {owner_name}")
                snippet_bits.append(f"Status: {status}")
                snippet_bits.append(_due_context(due_iso))
                snippet = " · ".join(snippet_bits)[:300]

                attrs: dict[str, Any] = {
                    "eos_priority_kind": priority_kind.value,
                    "is_own": bool(is_own),
                    "owner_email": owner_email,
                    "owner_name": owner_name,
                    "status": status,
                    "due_date": due_iso,
                }
                if due_iso:
                    # Give the temporal layer an anchor to resolve + tense-stamp.
                    attrs["mentioned_dates"] = [{"raw": due_iso, "resolved": due_iso}]
                attrs.update(extra)

                timestamp = _due_iso(created_at) or due_iso

                records.append(
                    {
                        "kind": kind,
                        "entity_kind": entity_kind,
                        "entity_id": str(entity_id),
                        "title": f"{_KIND_LABEL[kind]}: {desc[:60]}",
                        "snippet": snippet,
                        "timestamp": timestamp,
                        "entity_label": desc[:80],
                        "attrs": attrs,
                        "_sort": sort_key,
                    }
                )

            # To-Dos
            for t in todos:
                due_d = t.due_date.date() if isinstance(t.due_date, datetime) else None
                if due_d is None:
                    bucket = (7, 0)  # no due date — lowest of the deadline family
                elif due_d < today:
                    bucket = (0, due_d.toordinal())  # overdue
                else:
                    bucket = (2, due_d.toordinal())  # due soon
                _add(
                    kind="todo",
                    entity_kind=EntityKind.TODO,
                    priority_kind=PriorityKind.EOS_TODO_DUE,
                    entity_id=t.id,
                    description=t.description,
                    status=_enum_value(t.status),
                    owner_id=t.owner_id,
                    due_value=t.due_date,
                    created_at=getattr(t, "created_at", None),
                    extra={"rock_id": str(t.rock_id) if t.rock_id else None},
                    sort_key=bucket,
                )

            # Milestones
            for m in milestones:
                parent = rock_by_id.get(m.rock_id)
                owner_id = parent.owner_id if parent is not None else None
                due_val = getattr(m, "due_date", None)
                due_d = due_val.date() if isinstance(due_val, datetime) else due_val
                if isinstance(due_d, date) and due_d < today:
                    bucket = (0, due_d.toordinal())  # overdue
                elif isinstance(due_d, date):
                    bucket = (2, due_d.toordinal())  # due soon
                else:
                    bucket = (2, 0)
                _add(
                    kind="milestone",
                    entity_kind=EntityKind.MILESTONE,
                    priority_kind=PriorityKind.EOS_MILESTONE_DUE,
                    entity_id=m.id,
                    description=getattr(m, "description", ""),
                    status="open",
                    owner_id=owner_id,
                    due_value=due_val,
                    created_at=getattr(m, "created_at", None),
                    extra={
                        "rock_id": str(m.rock_id) if m.rock_id else None,
                        "rock_status": _enum_value(parent.status)
                        if parent is not None
                        else None,
                    },
                    sort_key=bucket,
                )

            # Rocks
            for r, is_off_track, is_stale, last in attention_rocks:
                reason = "off_track" if is_off_track else "stale"
                bucket = (1, 0) if is_off_track else (3, 0)
                _add(
                    kind="rock",
                    entity_kind=EntityKind.ROCK,
                    priority_kind=PriorityKind.EOS_ROCK_ATTENTION,
                    entity_id=r.id,
                    description=r.description,
                    status=_enum_value(r.status),
                    owner_id=r.owner_id,
                    due_value=None,
                    created_at=getattr(r, "created_at", None),
                    extra={
                        "rock_status": _enum_value(r.status),
                        "reason": reason,
                        "stale": bool(is_stale),
                        "last_update_date": last.isoformat()
                        if isinstance(last, date)
                        else None,
                        "quarter_id": str(r.quarter_id) if r.quarter_id else None,
                    },
                    sort_key=bucket,
                )

            # Issues
            for i in issues:
                prio = _enum_value(i.priority)
                rank = {"high": 4, "medium": 5, "low": 6}.get(prio, 6)
                _add(
                    kind="issue",
                    entity_kind=EntityKind.ISSUE,
                    priority_kind=PriorityKind.EOS_ISSUE_OPEN,
                    entity_id=i.id,
                    description=i.description,
                    status=_enum_value(i.status),
                    owner_id=i.owner_id,
                    due_value=None,
                    created_at=getattr(i, "created_at", None),
                    extra={"issue_priority": prio},
                    sort_key=(rank, 0),
                )

            # Highest-urgency first, then earliest deadline, capped.
            records.sort(key=lambda rec: rec["_sort"])
            return records[: self.MAX_SIGNALS_OUT]
        finally:
            db.close()

    # ── parse ──────────────────────────────────────────────────────────────

    def parse_signals(self, raw_items: list[dict]) -> list[ChannelSignal]:
        signals: list[ChannelSignal] = []
        for n, rec in enumerate(raw_items):
            if not isinstance(rec, dict):
                continue
            signals.append(
                ChannelSignal(
                    id=f"eos-{rec['kind']}-{n}",
                    channel=ChannelKind.EOS,
                    title=rec["title"],
                    snippet=rec.get("snippet", ""),
                    link=None,
                    timestamp=rec.get("timestamp"),
                    entity_kind=rec.get("entity_kind"),
                    entity_id=rec.get("entity_id"),
                    entity_label=rec.get("entity_label"),
                    attrs=dict(rec.get("attrs") or {}),
                )
            )
        return signals

    # ── prompt (extractor only) ──────────────────────────────────────────────

    def build_prompt(self, signals: list[ChannelSignal]) -> str:
        lines = []
        for s in signals:
            attrs = s.attrs
            lines.append(
                "- {kind}: {label}\n"
                "    owner={owner}; own={own}; status={status}; due={due}".format(
                    kind=(attrs.get("eos_priority_kind") or "eos"),
                    label=(s.entity_label or s.title)[:80],
                    owner=attrs.get("owner_email") or attrs.get("owner_name") or "-",
                    own=attrs.get("is_own"),
                    status=attrs.get("status"),
                    due=attrs.get("due_date") or "-",
                )
            )
        block = "\n".join(lines) if lines else "(no EOS items)"

        return (
            # Extractor only — no judgment vocabulary.
            "You are the EOS (Traction) channel extractor. Read the Rocks / "
            "To-Dos / Issues / Milestones below and return a structured JSON "
            "observation. You do NOT decide which items matter — the "
            "decision-maker downstream does that.\n\n"
            "## OUTPUT (raw JSON only)\n"
            "{\n"
            '  "summary": "1 factual sentence describing the user\'s (and their '
            "team's) open EOS commitments today.\",\n"
            '  "signal_attrs": {}\n'
            "}\n\n"
            "(``signal_attrs`` may stay empty — EOS rows are already fully "
            "structured. The deterministic Python side has populated every "
            "attr; the LLM call only summarizes.)\n\n"
            f"## EOS ITEMS\n{block}\n"
        )


_KIND_LABEL = {
    "todo": "To-Do",
    "rock": "Rock",
    "issue": "Issue",
    "milestone": "Milestone",
}


def _enum_value(value: Any) -> str | None:
    """Enum → its string value; passes through plain strings; None → None."""
    if value is None:
        return None
    return getattr(value, "value", value)
