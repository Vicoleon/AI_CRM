"""Routines: run a skill on its own when a new transcript of the owner's is ready.

A routine on a CLOCK is not here: it is an ordinary scheduled task whose
instruction runs a saved skill ("Run my saved meeting skill /weekly-digest
over my meetings from the last 7 days"), so the scheduled-task machinery —
structured schedule, claim, worker, auto-pause — already covers it.

The event kind works like this:

1. ``on_transcript_ready(doc_id)`` is called by every transcript ingestion path
   the moment a transcript turns ``approved``. It only WRITES ROWS: one
   ``meeting_routine_fires`` row per active routine of each participant whose
   filter matches. The pair (routine, transcript) is unique, so a transcript
   announced twice (a retried sync, two ingestion paths) fires once.
2. The scheduled-tasks tick (every ~5 minutes) calls ``dispatch_queued_fires``,
   which launches one ``cubie-worker`` execution per queued fire.
3. ``execute_fire`` claims the fire (``queued → running``, a compare-and-swap,
   so a retried worker exits quietly), re-checks the person and their Feature
   Access, runs the skill over that one meeting, writes the result as a new
   conversation in the "Scheduled tasks" chat folder, and DMs it on Slack.

Nothing a routine does needs approval at run time, by construction: a skill
run reads and, at most, leaves a Gmail draft. After
``MEETING_SKILLS_ROUTINE_MAX_FAILURES`` failures in a row the routine pauses
itself and tells its owner.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.meeting_skills import (
    MeetingRoutine,
    MeetingRoutineFire,
    MeetingSkill,
)

logger = logging.getLogger(__name__)

FIRE_JOB_MODULE = "backend.jobs.meeting_routine_fire_job"
# A fire still "running" this long after it started died with its worker.
_REAP_AFTER = timedelta(minutes=30)


class RoutineError(ValueError):
    """A routine change the person needs to hear about. The message is for them."""


def _session():
    from rubik_core.database import db as _db

    return _db.SessionLocal()


def _owner(email: str) -> str:
    return (email or "").strip().lower()


def view(row: MeetingRoutine, skill: MeetingSkill | None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "skill_id": row.skill_id,
        "skill_slug": skill.slug if skill else "",
        "filter": dict(row.filter or {}),
        "deliver_slack": bool(row.deliver_slack),
        "status": row.status,
        "paused_reason": row.paused_reason or "",
        "fire_count": row.fire_count or 0,
        "last_fired_at": row.last_fired_at,
        "last_status": row.last_status or "",
    }


def describe(v: dict) -> str:
    conditions = []
    flt = v.get("filter") or {}
    if flt.get("title_contains"):
        conditions.append(f"titled with “{flt['title_contains']}”")
    if flt.get("external_only"):
        conditions.append("with someone from outside the company")
    when = "whenever a transcript of yours is ready"
    if conditions:
        when = "whenever a transcript of yours " + " and ".join(
            ["is ready"] + [f"for a meeting {c}" for c in conditions]
        )
    status = {
        "active": "Active",
        "paused": "Paused",
        "error_paused": "Paused after repeated failures",
    }.get(v["status"], v["status"])
    return f"**{v['name']}** — runs /{v['skill_slug']} {when}. {status}."


def _find(db, owner: str, name: str) -> MeetingRoutine | None:
    ref = " ".join((name or "").split()).lower()
    if not ref:
        return None
    rows = db.query(MeetingRoutine).filter(MeetingRoutine.owner_email == owner).all()
    for row in rows:
        if row.id == name or row.name.lower() == ref:
            return row
    return None


def create_routine(
    owner_email: str,
    skill_ref: str,
    *,
    name: str = "",
    title_contains: str = "",
    external_only: bool = False,
    deliver_slack: bool = True,
    source_session_id: str | None = None,
) -> dict:
    """Raises :class:`RoutineError`."""
    from rubik_core.meeting_skills.store import _find_skill

    owner = _owner(owner_email)
    with _session() as db:
        skill = _find_skill(db, owner, skill_ref)
        if skill is None:
            raise RoutineError(
                f"You have no skill called '{skill_ref}'. Save the skill first, "
                "then make it a routine."
            )
        name = " ".join((name or "").split())[:80] or skill.name
        if _find(db, owner, name) is not None:
            raise RoutineError(f"You already have a routine called '{name}'.")
        count = (
            db.query(MeetingRoutine).filter(MeetingRoutine.owner_email == owner).count()
        )
        if count >= settings.MEETING_SKILLS_MAX_ROUTINES_PER_USER:
            raise RoutineError(
                f"You already have {count} meeting routines, the most {PRODUCT_NAME} keeps "
                "per person. Delete one first."
            )
        flt = {}
        if (title_contains or "").strip():
            flt["title_contains"] = " ".join(title_contains.split())[:80]
        if external_only:
            flt["external_only"] = True
        row = MeetingRoutine(
            owner_email=owner,
            name=name,
            skill_id=skill.id,
            trigger_kind="transcript_ready",
            filter=flt,
            deliver_slack=bool(deliver_slack),
            status="active",
            consecutive_failures=0,
            fire_count=0,
            active_since=datetime.utcnow(),
            source_session_id=source_session_id,
        )
        db.add(row)
        db.commit()
        return view(row, skill)


def list_routines(owner_email: str) -> list[dict]:
    owner = _owner(owner_email)
    with _session() as db:
        rows = (
            db.query(MeetingRoutine, MeetingSkill)
            .join(MeetingSkill, MeetingSkill.id == MeetingRoutine.skill_id)
            .filter(MeetingRoutine.owner_email == owner)
            .order_by(MeetingRoutine.created_at)
            .all()
        )
        return [view(r, s) for r, s in rows]


def set_paused(owner_email: str, name: str, paused: bool) -> dict:
    owner = _owner(owner_email)
    with _session() as db:
        row = _find(db, owner, name)
        if row is None:
            raise RoutineError(f"You have no routine called '{name}'.")
        row.status = "paused" if paused else "active"
        row.paused_reason = None
        if not paused:
            row.consecutive_failures = 0
        db.commit()
        skill = db.query(MeetingSkill).filter(MeetingSkill.id == row.skill_id).first()
        return view(row, skill)


def delete_routine(owner_email: str, name: str) -> dict:
    owner = _owner(owner_email)
    with _session() as db:
        row = _find(db, owner, name)
        if row is None:
            raise RoutineError(f"You have no routine called '{name}'.")
        out = view(row, None)
        db.query(MeetingRoutineFire).filter(
            MeetingRoutineFire.routine_id == row.id
        ).delete(synchronize_session=False)
        db.delete(row)
        db.commit()
        return out


# ── the event: a transcript is ready ────────────────────────────────────────


def _matches(flt: dict, title: str, participants: list[str]) -> bool:
    wanted = (flt.get("title_contains") or "").strip().lower()
    if wanted and wanted not in (title or "").lower():
        return False
    if flt.get("external_only"):
        from rubik_core.orchestration.confirmation_render import external_addresses

        if not external_addresses(participants):
            return False
    return True


def on_transcript_ready(doc_id: str) -> int:
    """Queue a fire for every routine this transcript triggers. Returns how
    many were queued. Never raises: an ingestion path must not fail over it."""
    if not settings.MEETING_SKILLS_ENABLED or not doc_id:
        return 0
    try:
        from rubik_core.database.models import Asset, MeetingParticipant

        with _session() as db:
            asset = db.query(Asset).filter(Asset.doc_id == doc_id).first()
            if asset is None or asset.status != "approved":
                return 0
            participants = [
                r.participant_email
                for r in db.query(MeetingParticipant)
                .filter(MeetingParticipant.doc_id == doc_id)
                .all()
            ]
            if not participants:
                return 0
            meeting_at = asset.created_at or datetime.utcnow()
            routines = (
                db.query(MeetingRoutine)
                .filter(
                    MeetingRoutine.owner_email.in_(participants),
                    MeetingRoutine.status == "active",
                    MeetingRoutine.active_since <= meeting_at,
                )
                .all()
            )
            already = {
                r.routine_id
                for r in db.query(MeetingRoutineFire.routine_id)
                .filter(MeetingRoutineFire.doc_id == doc_id)
                .all()
            }
            queued = 0
            for routine in routines:
                if routine.id in already:
                    continue
                if not _matches(
                    routine.filter or {}, asset.original_filename, participants
                ):
                    continue
                db.add(
                    MeetingRoutineFire(
                        routine_id=routine.id,
                        doc_id=doc_id,
                        owner_email=routine.owner_email,
                        status="queued",
                    )
                )
                try:
                    db.commit()
                    queued += 1
                except Exception:  # noqa: BLE001 — the unique pair: already queued
                    db.rollback()
            if queued:
                logger.info(
                    "[meeting-routines] doc=%s queued %d fire(s)", doc_id[:12], queued
                )
            return queued
    except Exception:  # noqa: BLE001
        logger.exception("[meeting-routines] on_transcript_ready failed for %s", doc_id)
        return 0


# ── dispatch (called by the scheduled-tasks tick) ───────────────────────────


def _default_launcher(module: str, args: list[str]) -> str | None:
    """One ``cubie-worker`` execution on GCP; inline everywhere else (the same
    split as the scheduled-task dispatcher's launcher)."""
    from rubik_core.config import AppMode

    if settings.APP_MODE == AppMode.GCP:
        from backend.jobs.launcher import launch_module_execution

        execution = launch_module_execution(module, args)
        return execution.get("name") or execution.get("metadata", {}).get("name")
    execute_fire(args[-1])
    return None


def dispatch_queued_fires(*, launcher=None, limit: int | None = None) -> dict:
    """Reap dead fires, then launch a worker for each queued one. Never raises."""
    report = {"queued": 0, "dispatched": 0, "dispatch_failed": 0, "reaped": 0}
    if not settings.MEETING_SKILLS_ENABLED:
        return {**report, "status": "disabled"}
    launch = launcher or _default_launcher
    cap = limit or settings.SCHEDULED_TASKS_TICK_MAX_DISPATCH
    try:
        from rubik_core.scheduled_tasks.store import ensure_results_folder

        with _session() as db:
            stale = datetime.utcnow() - _REAP_AFTER
            for fire in (
                db.query(MeetingRoutineFire)
                .filter(
                    MeetingRoutineFire.status == "running",
                    MeetingRoutineFire.started_at < stale,
                )
                .all()
            ):
                fire.status = "failed"
                fire.error = "The worker stopped without reporting back."
                fire.finished_at = datetime.utcnow()
                report["reaped"] += 1
            db.commit()
            fires = (
                db.query(MeetingRoutineFire)
                .filter(MeetingRoutineFire.status == "queued")
                .order_by(MeetingRoutineFire.created_at)
                .limit(cap)
                .all()
            )
            report["queued"] = len(fires)
            pending = [(f.id, f.owner_email) for f in fires]
            # Created here, sequentially, so two fires finishing at once cannot
            # race a second "Scheduled tasks" folder into existence.
            for owner in {o for _, o in pending}:
                ensure_results_folder(db, _profile_email(owner))
        for fire_id, _owner_email in pending:
            try:
                launch(FIRE_JOB_MODULE, ["--fire-id", fire_id])
                report["dispatched"] += 1
            except Exception:  # noqa: BLE001 — one bad launch must not stall the rest
                logger.exception(
                    "[meeting-routines] launch failed for fire=%s", fire_id
                )
                _finish(
                    fire_id, "failed", error=f"{PRODUCT_NAME} could not start the run."
                )
                report["dispatch_failed"] += 1
    except Exception:  # noqa: BLE001
        logger.exception("[meeting-routines] dispatch failed")
    return report


def _profile_email(owner: str) -> str:
    """The address as the profile spells it: chat's lookups are case-exact."""
    profile = _resolve_profile(owner)
    return profile.email if profile is not None else owner


# ── executing one fire (inside the worker) ──────────────────────────────────


@dataclass
class FireOutcome:
    fire_id: str
    status: str
    reason: str = ""
    chat_session_id: str | None = None


def _resolve_profile(email: str):
    try:
        from rubik_core.auth.registry import UserRegistry

        return UserRegistry().get_user(email)
    except Exception:  # noqa: BLE001
        logger.exception("[meeting-routines] profile lookup failed for %s", email)
        return None


def _allowed(profile) -> bool:
    if not settings.MEETING_SKILLS_ENABLED:
        return False
    try:
        from rubik_core.features import feature_allows

        return feature_allows("meeting_skills", profile)
    except Exception:  # noqa: BLE001 — fail closed
        logger.exception("[meeting-routines] gate check failed")
        return False


def _claim(fire_id: str) -> bool:
    with _session() as db:
        claimed = (
            db.query(MeetingRoutineFire)
            .filter(
                MeetingRoutineFire.id == fire_id, MeetingRoutineFire.status == "queued"
            )
            .update(
                {"status": "running", "started_at": datetime.utcnow()},
                synchronize_session=False,
            )
        )
        db.commit()
        return claimed == 1


def _finish(fire_id: str, status: str, **fields) -> None:
    with _session() as db:
        fire = (
            db.query(MeetingRoutineFire)
            .filter(MeetingRoutineFire.id == fire_id)
            .first()
        )
        if fire is None:
            return
        fire.status = status
        fire.finished_at = datetime.utcnow()
        for key, value in fields.items():
            setattr(fire, key, value)
        db.commit()


def _record_outcome(routine_id: str, status: str) -> bool:
    """Update the routine's counters; return True when it just auto-paused."""
    with _session() as db:
        routine = (
            db.query(MeetingRoutine).filter(MeetingRoutine.id == routine_id).first()
        )
        if routine is None:
            return False
        routine.last_fired_at = datetime.utcnow()
        routine.last_status = status
        routine.fire_count = (routine.fire_count or 0) + 1
        paused = False
        if status == "succeeded":
            routine.consecutive_failures = 0
        elif status == "failed":
            routine.consecutive_failures = (routine.consecutive_failures or 0) + 1
            if (
                routine.consecutive_failures
                >= settings.MEETING_SKILLS_ROUTINE_MAX_FAILURES
                and routine.status == "active"
            ):
                routine.status = "error_paused"
                routine.paused_reason = "It failed several times in a row."
                paused = True
        db.commit()
        return paused


def _save_conversation(
    profile, title: str, prompt: str, answer: str, citations, meta
) -> str:
    from rubik_core.database.models import ChatMessage, ChatSession
    from rubik_core.scheduled_tasks.store import ensure_results_folder

    session_id = str(uuid.uuid4())
    with _session() as db:
        folder_id = ensure_results_folder(db, profile.email)
        db.add(
            ChatSession(
                id=session_id,
                user_email=profile.email,
                title=title[:200],
                folder_id=folder_id,
                created_at=datetime.utcnow(),
            )
        )
        db.add(
            ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="user",
                content=prompt,
                created_at=datetime.utcnow(),
            )
        )
        db.add(
            ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="assistant",
                content=answer,
                metadata_json={"citations": citations, **meta},
                created_at=datetime.utcnow(),
            )
        )
        db.commit()
    return session_id


def execute_fire(fire_id: str) -> FireOutcome:
    """Run one queued fire end to end. Never raises."""
    from rubik_core.meeting_skills import executor, store
    from rubik_core.meeting_skills.meetings import find_meetings
    from rubik_core.meeting_skills.sections import DEFAULT_SECTIONS, sections_from_json

    if not _claim(fire_id):
        logger.warning("[meeting-routines] fire=%s already claimed; exiting", fire_id)
        return FireOutcome(fire_id, "already_claimed")
    with _session() as db:
        fire = (
            db.query(MeetingRoutineFire)
            .filter(MeetingRoutineFire.id == fire_id)
            .first()
        )
        routine = (
            db.query(MeetingRoutine)
            .filter(MeetingRoutine.id == fire.routine_id)
            .first()
            if fire
            else None
        )
        if fire is None or routine is None:
            _finish(fire_id, "skipped", error="The routine is gone.")
            return FireOutcome(fire_id, "skipped", "routine_missing")
        routine_id, routine_name = routine.id, routine.name
        owner, doc_id, deliver_slack = (
            routine.owner_email,
            fire.doc_id,
            routine.deliver_slack,
        )
        skill_id, active = routine.skill_id, routine.status == "active"
    if not active:
        _finish(fire_id, "skipped", error="The routine is paused.")
        return FireOutcome(fire_id, "skipped", "paused")

    profile = _resolve_profile(owner)
    if profile is None or not _allowed(profile):
        _finish(
            fire_id, "skipped", error="The owner cannot currently use meeting skills."
        )
        return FireOutcome(fire_id, "skipped", "access_denied")

    skill = store.get_skill(owner, skill_id)
    refs = find_meetings(owner, doc_id)
    if skill is None or not refs:
        _finish(
            fire_id, "skipped", error="The skill or the meeting is no longer available."
        )
        return FireOutcome(fire_id, "skipped", "missing")

    sections = sections_from_json(skill["sections"]) or list(DEFAULT_SECTIONS)
    try:
        result = asyncio.run(
            asyncio.wait_for(
                executor.run(
                    user_email=owner,
                    refs=refs,
                    sections=sections,
                    instructions=skill["instructions"],
                    tone=skill["tone"],
                    detail_level=skill["detail_level"],
                    exclude_personal=skill["exclude_personal"],
                    heading=skill["name"],
                    skill=skill,
                    template={
                        "id": skill["template_id"],
                        "version": skill["template_version"],
                    }
                    if skill["template_id"]
                    else None,
                    trigger="routine",
                    routine_id=routine_id,
                    deliver_as=skill["deliver_as"],
                ),
                timeout=settings.SCHEDULED_TASKS_RUN_TIMEOUT_SECONDS,
            )
        )
    except Exception as exc:  # noqa: BLE001 — includes the timeout
        logger.exception("[meeting-routines] fire=%s failed", fire_id)
        _finish(fire_id, "failed", error=f"The run failed: {exc}"[:2000])
        _after(routine_id, routine_name, profile, "failed")
        return FireOutcome(fire_id, "failed", "exception")

    if result.status == "failed":
        _finish(
            fire_id, "failed", error=result.message, skill_run_id=result.run_id or None
        )
        _after(routine_id, routine_name, profile, "failed")
        return FireOutcome(fire_id, "failed", "run_failed")

    meeting_title = result.meeting_titles[0] if result.meeting_titles else "a meeting"
    title = f"{routine_name} — {meeting_title}"
    answer = result.markdown
    if result.output_ref.get("gmail_draft_url"):
        answer += (
            "\n\nA Gmail draft is waiting for you to review and send: "
            f"{result.output_ref['gmail_draft_url']}"
        )
    session_id = _save_conversation(
        profile,
        title,
        f"Routine “{routine_name}”: run /{skill['slug']} on “{meeting_title}”.",
        answer,
        result.citations,
        {"meeting_routine_id": routine_id, "meeting_skill_run_id": result.run_id},
    )
    channels = {"chat": "sent"}
    if deliver_slack:
        from rubik_core.scheduled_tasks import delivery

        channels.update(
            delivery.deliver(
                task=SimpleNamespace(delivery_channels=["slack"]),
                run_id=fire_id,
                recipient_email=profile.email,
                title=title,
                answer_md=answer,
                chat_session_id=session_id,
            )
        )
    _finish(
        fire_id,
        "succeeded",
        skill_run_id=result.run_id or None,
        chat_session_id=session_id,
        delivery=channels,
    )
    _after(routine_id, routine_name, profile, "succeeded")
    return FireOutcome(fire_id, "succeeded", chat_session_id=session_id)


def _after(routine_id: str, routine_name: str, profile, status: str) -> None:
    if _record_outcome(routine_id, status):
        from rubik_core.scheduled_tasks import delivery

        delivery.notify(
            recipient_email=profile.email,
            title=f"Meeting routine paused: {routine_name}",
            chat_session_id=None,
            body=(
                f"“{routine_name}” failed several times in a row, so {PRODUCT_NAME} paused it. "
                f"Ask {PRODUCT_NAME} to resume it once the cause is fixed."
            ),
            severity="warn",
        )
