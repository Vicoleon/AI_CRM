"""Run a skill (or a one-off template) over meetings. The single run contract.

1. **Load** the meetings, re-checking that the person attended each one.
2. **Extract** in one structured model call: a plan, then cited items per section.
3. **Verify** without a model: uncited or misquoted items are dropped, unproven
   owners, dates and figures are flagged or removed (``verify``).
4. **Render** in the template's order (``render``).
5. **Deliver**: the answer always goes back to chat; a skill set to deliver by
   email leaves a Gmail DRAFT — never a sent message.
6. **Log** one ``meeting_skill_runs`` row: what was read, the plan, the model's
   answer before verification, what was dropped, where it went.

Never raises: a failure comes back as ``RunResult(status="failed")`` with a
sentence the person can act on.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.meeting_skills import store
from rubik_core.meeting_skills.extract import ExtractFailed, build_prompt, extract
from rubik_core.meeting_skills.meetings import MeetingRef, load_meeting
from rubik_core.meeting_skills.normalize import NormalizedMeeting
from rubik_core.meeting_skills.render import render
from rubik_core.meeting_skills.sections import Section, sections_to_json
from rubik_core.meeting_skills.verify import verify

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    status: str  # ok | partial | failed
    markdown: str = ""
    citations: list[dict] = field(default_factory=list)
    run_id: str = ""
    message: str = ""
    dropped: int = 0
    flagged: int = 0
    meeting_titles: list[str] = field(default_factory=list)
    output_ref: dict = field(default_factory=dict)


def _email_body(markdown: str, citations: list[dict]) -> str:
    if not citations:
        return markdown
    sources = "\n".join(f"- {c['snippet']}" for c in citations)
    return f"{markdown}\n\n---\nSources (from the meeting transcript):\n{sources}"


def _deliver_email(
    user_email: str,
    deliver_as: str,
    subject: str,
    markdown: str,
    citations: list[dict],
    meetings: list[NormalizedMeeting],
) -> dict:
    """A Gmail DRAFT for the person to review and send themselves."""
    from rubik_core.tools.gmail import GmailClient

    me = user_email.strip().lower()
    if deliver_as == "email_draft_to_attendees":
        to = sorted({a for m in meetings for a in m.attendees if a != me})
        if not to:
            to = [me]
    else:
        to = [me]
    try:
        result = GmailClient(user_email=user_email).create_draft(
            to=to, subject=subject, body_markdown=_email_body(markdown, citations)
        )
    except Exception as exc:  # noqa: BLE001 — the chat copy still stands
        logger.warning("[meeting-skills] draft failed for %s: %s", user_email, exc)
        return {"email_draft": "failed"}
    return {
        "email_draft": "created",
        "gmail_draft_url": result.get("gmail_draft_url", ""),
        "to": to,
    }


async def run(
    *,
    user_email: str,
    refs: list[MeetingRef],
    sections: list[Section],
    instructions: str = "",
    tone: str = "",
    detail_level: str = "standard",
    exclude_personal: bool = True,
    heading: str = "",
    skill: dict | None = None,
    template: dict | None = None,
    trigger: str = "chat",
    routine_id: str | None = None,
    deliver_as: str = "chat",
    language: str = "",
) -> RunResult:
    loaded = await asyncio.gather(
        *(asyncio.to_thread(load_meeting, user_email, ref) for ref in refs)
    )
    meetings = [m for m in loaded if m is not None and m.utterances]
    log_base = {
        "owner_email": user_email,
        "skill_id": skill["id"] if skill else None,
        "skill_version": skill["version"] if skill else None,
        "template_id": template["id"] if template else None,
        "template_version": template["version"] if template else None,
        "sections": sections_to_json(sections),
        "doc_ids": [r.doc_id for r in refs],
        "trigger": trigger,
        "routine_id": routine_id,
    }
    if not meetings:
        message = (
            "I couldn't read a transcript for that meeting. It may still be "
            "processing, or it has no spoken lines to work from."
        )
        run_id = await asyncio.to_thread(
            store.record_run, **log_base, status="failed", error=message
        )
        return RunResult(status="failed", message=message, run_id=run_id)

    prompt = build_prompt(
        person=user_email,
        meetings=meetings,
        sections=sections,
        instructions=instructions,
        tone=tone,
        detail_level=detail_level,
        exclude_personal=exclude_personal,
        language=language,
    )
    log_base["transcript_hashes"] = {m.doc_id: m.content_hash for m in meetings}
    # A live chat turn has ~90 seconds end to end, and the model still has to
    # relay the write-up after this returns; an unattended run has minutes.
    budget = (
        settings.MEETING_SKILLS_CHAT_EXTRACT_TIMEOUT_SECONDS
        if trigger == "chat"
        else None
    )
    try:
        extraction = await asyncio.wait_for(
            extract(prompt, user_email=user_email), timeout=budget
        )
    except asyncio.TimeoutError:
        message = (
            "That took longer than a chat answer allows. Ask for a brief version, "
            f"or for fewer meetings — or have {PRODUCT_NAME} run it on its own and send you "
            "the result (a routine or a scheduled task has more time)."
        )
        run_id = await asyncio.to_thread(
            store.record_run, **log_base, status="failed", error="chat time budget"
        )
        return RunResult(status="failed", message=message, run_id=run_id)
    except ExtractFailed as exc:
        message = f"{PRODUCT_NAME} couldn't finish reading the meeting just now. Try again in a minute."
        run_id = await asyncio.to_thread(
            store.record_run, **log_base, status="failed", error=str(exc)[:2000]
        )
        return RunResult(status="failed", message=message, run_id=run_id)

    verified = verify(extraction.sections, meetings, [s.key for s in sections])
    rendered = render(verified, sections, meetings, heading=heading)
    status = "partial" if (verified.dropped or verified.flagged) else "ok"

    output_ref: dict = {}
    if deliver_as in ("email_draft_to_me", "email_draft_to_attendees"):
        subject = heading or (skill["name"] if skill else "Meeting notes")
        if len(meetings) == 1:
            subject = f"{subject} — {meetings[0].title}"
        output_ref = await asyncio.to_thread(
            _deliver_email,
            user_email,
            deliver_as,
            subject[:200],
            rendered.markdown,
            rendered.citations,
            meetings,
        )

    run_id = await asyncio.to_thread(
        store.record_run,
        **log_base,
        plan=extraction.plan,
        intermediate={"sections": extraction.sections},
        verification=verified.report(),
        output_md=rendered.markdown,
        citations=rendered.citations,
        output_ref=output_ref or None,
        status=status,
        model=extraction.model,
    )
    if skill:
        await asyncio.to_thread(store.record_invocation, skill["id"])
    return RunResult(
        status=status,
        markdown=rendered.markdown,
        citations=rendered.citations,
        run_id=run_id,
        dropped=len(verified.dropped),
        flagged=verified.flagged,
        meeting_titles=[m.title for m in meetings],
        output_ref=output_ref,
    )
