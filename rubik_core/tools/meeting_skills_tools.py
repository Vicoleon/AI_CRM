"""Chat tools for Meeting Skills: templates, skills and routines over meetings.

Identity comes from the constructor, the same contract as
``ScheduledTaskChatTools``: no tool takes an email, so the model can only
read, run or save the signed-in person's own templates and skills, over
meetings they attended.

Running is free to call (read-only apart from the run log and, for a skill set
to deliver by email, a Gmail DRAFT). Saving, changing and deleting are armed
with the confirmation pause in the coordinator (pinned in
``MEETING_SKILL_CONFIRM_TOOLS``): a saved skill or routine is an instruction
that runs again later, and a routine runs with nobody watching.

Every tool returns a dict with ``status`` and a ``report`` the model relays. A
run also returns ``citations``, which the orchestrator hands to the Sources
panel.
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings

logger = logging.getLogger(__name__)

#: Every tool here that saves or removes something. The coordinator registers
#: each with ``require_confirmation=True``; a test fails if one is ever bare.
MEETING_SKILL_CONFIRM_TOOLS = frozenset(
    {
        "save_meeting_template",
        "delete_meeting_template",
        "save_meeting_skill",
        "delete_meeting_skill",
        "create_meeting_routine",
        "delete_meeting_routine",
    }
)

_MADE_FROM = {"request": "chat", "attachment": "upload", "answer": "output"}
_DELIVER_WORDS = {
    "chat": "here in chat",
    "email_draft_to_me": "here in chat and as a Gmail draft to you",
    "email_draft_to_attendees": (
        "here in chat and as a Gmail draft to the other attendees (you send it)"
    ),
}


class MeetingSkillChatTools:
    """Meeting Skills tools bound to one signed-in user."""

    def __init__(self, user_email: str, source_session_id: str | None = None):
        self.user_email = user_email
        self.source_session_id = source_session_id

    # ── plumbing ────────────────────────────────────────────────────────────

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"status": "error", "report": message}

    def _resolve_sections(self, sections: str, template: str):
        """(sections, template_view) from a saved template name, or written
        sections, or the default outline. Raises ValueError with a message."""
        from rubik_core.meeting_skills import store
        from rubik_core.meeting_skills.sections import (
            DEFAULT_SECTIONS,
            parse_sections,
            sections_from_json,
        )

        if (template or "").strip():
            view = store.get_template(self.user_email, template)
            if view is None:
                names = [t["name"] for t in store.list_templates(self.user_email)]
                known = f" Your templates: {', '.join(names)}." if names else ""
                raise ValueError(f"You have no template called '{template}'.{known}")
            return sections_from_json(view["sections"]), view
        if (sections or "").strip():
            return parse_sections(sections), None
        return list(DEFAULT_SECTIONS), None

    def _refs(self, meeting: str, days: int, many: bool):
        from rubik_core.meeting_skills.meetings import find_meetings

        limit = settings.MEETING_SKILLS_MAX_MEETINGS if (many or days) else 1
        return find_meetings(self.user_email, meeting, days=days or 0, limit=limit)

    @staticmethod
    def _no_meeting(meeting: str, days: int) -> dict[str, Any]:
        if days:
            what = f"in the last {days} days" + (
                f" matching '{meeting}'" if meeting else ""
            )
        elif meeting:
            what = f"matching '{meeting}'"
        else:
            what = "on record"
        return {
            "status": "error",
            "report": (
                f"I found no meeting you attended {what}. {PRODUCT_NAME} can only use "
                "meetings with a Google Meet or Fathom transcript that list you "
                "as a participant."
            ),
        }

    @staticmethod
    def _run_report(result, extra: str = "") -> dict[str, Any]:
        if result.status == "failed":
            return {"status": "error", "report": result.message}
        lines = [result.markdown]
        if result.output_ref.get("email_draft") == "created":
            url = result.output_ref.get("gmail_draft_url", "")
            lines.append(f"A Gmail draft is waiting for you to review and send: {url}")
        elif result.output_ref.get("email_draft") == "failed":
            lines.append("I couldn't create the Gmail draft; the write-up is above.")
        if extra:
            lines.append(extra)
        return {
            "status": "success",
            "report": "\n\n".join(lines),
            "citations": result.citations,
        }

    # ── running ─────────────────────────────────────────────────────────────

    async def summarize_meeting(
        self,
        meeting: str = "",
        sections: str = "",
        template: str = "",
        instructions: str = "",
        tone: str = "",
        detail: str = "standard",
        days: int = 0,
        language: str = "",
    ) -> dict:
        """Write up one of the user's meetings in whatever shape they ask for, with every point cited to the transcript.

        Use this whenever the user wants a meeting written up in a particular
        format or outline: "summarize my 10am as a BANT note", "give me the
        Acme call as pains / budget / next steps", "write my last meeting up
        like a SOAP note", "use my Client Discovery template on the Globex
        call", "turn the weekly sync into a decision record". You design the
        outline yourself from what they asked for: write `sections` as one
        line per section, "Title (layout): what goes in it", where layout is
        paragraph, bullets, topics (a heading per topic) or checklist (tasks
        grouped by owner). Keep the user's own section names when they gave
        them. To use one of their SAVED templates, pass its name as
        `template` instead. Give neither for a general summary.

        For a plain question about a meeting ("what did we decide about
        pricing?") use `search_past_meetings` instead.

        Args:
            meeting: Which meeting — its title or part of it ("Acme weekly"),
                "last meeting", or blank for the most recent one.
            sections: The outline you wrote for this request (see above).
            template: The name of one of the user's saved templates.
            instructions: Anything else the user asked for: what to focus on,
                what to leave out.
            tone: The tone they asked for, e.g. "formal, client-facing".
            detail: "brief", "standard" or "exhaustive".
            days: To cover every meeting in a recent window instead of one,
                the number of days (e.g. 7 for "this week's meetings").
            language: The language to write in, only if they asked for one.

        Returns:
            dict: status, the finished write-up as `report` (show it as is), and citations.
        """
        from rubik_core.meeting_skills import executor
        from rubik_core.meeting_skills.sections import SectionsError

        try:
            parsed, template_view = self._resolve_sections(sections, template)
        except (SectionsError, ValueError) as exc:
            return self._error(str(exc))
        days = max(0, min(int(days or 0), 31))
        refs = self._refs(meeting, days, many=bool(days))
        if not refs:
            return self._no_meeting(meeting, days)
        result = await executor.run(
            user_email=self.user_email,
            refs=refs,
            sections=parsed,
            instructions=instructions,
            tone=tone,
            detail_level=detail
            if detail in ("brief", "standard", "exhaustive")
            else "standard",
            exclude_personal=True,
            heading=template_view["name"] if template_view else "",
            template=template_view,
            language=language,
        )
        extra = ""
        if not template_view and (sections or "").strip() and result.status != "failed":
            extra = (
                "(Offer to save this outline as a template, or this whole request "
                "as a skill, if they will want it again.)"
            )
        return self._run_report(result, extra)

    async def run_meeting_skill(
        self, skill: str, meeting: str = "", days: int = 0
    ) -> dict:
        """Run one of the user's saved meeting skills.

        Use this when the user names a saved skill or types it with a slash
        ("/client-debrief the Acme call", "run my weekly digest", "do the
        debrief on my last meeting").

        Args:
            skill: The skill's name or slash name, e.g. "client-debrief".
            meeting: For a one-meeting skill, which meeting (title words or
                blank for the most recent). For a many-meeting skill, optional
                title words to narrow the window to.
            days: Override the skill's window, in days, for a many-meeting skill.

        Returns:
            dict: status, the finished output as `report` (show it as is), and citations.
        """
        from rubik_core.meeting_skills import executor, store
        from rubik_core.meeting_skills.sections import (
            DEFAULT_SECTIONS,
            sections_from_json,
        )

        view = store.get_skill(self.user_email, skill)
        if view is None and store.get_template(self.user_email, skill) is not None:
            # "/bant-note" from the menu can name a template: run it as one.
            return await self.summarize_meeting(
                meeting=meeting, template=skill, days=days
            )
        if view is None:
            names = [f"/{s['slug']}" for s in store.list_skills(self.user_email)]
            known = (
                f" Your skills: {', '.join(names)}."
                if names
                else " You have no saved skills yet."
            )
            return self._error(f"You have no skill called '{skill}'.{known}")
        many = view["meetings"] == "many"
        window = max(0, min(int(days or 0), 31)) or (view["window_days"] if many else 0)
        refs = self._refs(meeting, window, many=many)
        if not refs:
            return self._no_meeting(meeting, window)
        sections = sections_from_json(view["sections"]) or list(DEFAULT_SECTIONS)
        template_view = (
            {"id": view["template_id"], "version": view["template_version"]}
            if view["template_id"]
            else None
        )
        result = await executor.run(
            user_email=self.user_email,
            refs=refs,
            sections=sections,
            instructions=view["instructions"],
            tone=view["tone"],
            detail_level=view["detail_level"],
            exclude_personal=view["exclude_personal"],
            heading=view["name"],
            skill=view,
            template=template_view,
            deliver_as=view["deliver_as"],
        )
        return self._run_report(result)

    def list_meeting_skills(self) -> dict:
        """List the user's saved meeting skills, templates and routines.

        Use for "what skills / templates do I have?", "show my meeting
        routines", and before changing or deleting one so you have its exact
        name.

        Returns:
            dict: status and a report listing each with its sections or schedule.
        """
        from rubik_core.meeting_skills import routines, store

        skills = store.list_skills(self.user_email)
        templates = store.list_templates(self.user_email)
        mine = routines.list_routines(self.user_email)
        if not skills and not templates and not mine:
            return {
                "status": "success",
                "report": (
                    "You have no saved meeting skills, templates or routines yet. "
                    "Ask for a meeting in any shape and you can save it from there."
                ),
            }
        parts: list[str] = []
        if skills:
            parts.append("**Skills** (type / in the chat box to run one)")
            for s in skills:
                outline = (
                    f" — outline: {s['template_name']}" if s["template_name"] else ""
                )
                scope = (
                    f"meetings from the last {s['window_days']} days"
                    if s["meetings"] == "many"
                    else "one meeting at a time"
                )
                parts.append(
                    f"- /{s['slug']} — {s['name']}: {s['instructions'][:200]} "
                    f"({scope}; delivered {_DELIVER_WORDS.get(s['deliver_as'], 'in chat')}"
                    f"{outline}; version {s['version']})"
                )
        if templates:
            parts.append("**Templates**")
            for t in templates:
                titles = ", ".join(sec.get("title", "") for sec in t["sections"])
                parts.append(f"- {t['name']}: {titles} (version {t['version']})")
        if mine:
            parts.append("**Routines** (run on their own when a transcript is ready)")
            for r in mine:
                parts.append(f"- {routines.describe(r)}")
        return {"status": "success", "report": "\n".join(parts)}

    # ── saving (each armed with the confirmation pause) ─────────────────────

    def save_meeting_template(
        self,
        name: str,
        sections: str,
        description: str = "",
        made_from: str = "request",
    ) -> dict:
        """Save an outline as one of the user's meeting templates, for reuse.

        Use when the user wants to keep an outline: "save that as my BANT
        template", "make this my client discovery template", or attaches a form
        or document and says "use this as a template". For an attached form,
        copy its headings EXACTLY and in their order, one per line. For "save
        the structure of that summary", use the headings of that answer.
        Saving under a name they already have makes a new version.
        This pauses for the user's confirmation; Cubie shows them the sections.

        Args:
            name: The template's name, e.g. "BANT note".
            sections: One line per section, "Title (layout): what goes in it";
                layout is paragraph, bullets, topics or checklist.
            description: One line on when to use it.
            made_from: "request" (designed from what they asked), "attachment"
                (copied from a file they attached) or "answer" (from a past write-up).

        Returns:
            dict: status and a report confirming what was saved.
        """
        from rubik_core.meeting_skills import store
        from rubik_core.meeting_skills.sections import (
            SectionsError,
            describe_sections,
            parse_sections,
        )

        try:
            parsed = parse_sections(sections)
            view, created = store.save_template(
                self.user_email,
                name,
                parsed,
                description=description,
                created_via=_MADE_FROM.get(made_from, "chat"),
                source_session_id=self.source_session_id,
            )
        except (SectionsError, store.StoreError) as exc:
            return self._error(str(exc))
        verb = "Saved" if created else f"Updated (now version {view['version']})"
        return {
            "status": "success",
            "report": (
                f"{verb} your template **{view['name']}**:\n"
                f"{describe_sections(parsed)}\n\n"
                f'Use it any time: "use my {view["name"]} template on <meeting>", '
                "or pick it from the / menu in the chat box."
            ),
        }

    def delete_meeting_template(self, name: str) -> dict:
        """Delete one of the user's meeting templates, for good.

        Skills that used it keep working with the general outline. This pauses
        for the user's confirmation.

        Args:
            name: The template's exact name (from `list_meeting_skills`).

        Returns:
            dict: status and a report.
        """
        from rubik_core.meeting_skills import store

        try:
            view = store.delete_template(self.user_email, name)
        except store.StoreError as exc:
            return self._error(str(exc))
        note = ""
        if view.get("used_by"):
            note = (
                f" These skills used it and now use the general outline: "
                f"{', '.join(view['used_by'])}."
            )
        return {
            "status": "success",
            "report": f"Deleted the template **{view['name']}**.{note}",
        }

    def save_meeting_skill(
        self,
        name: str,
        instructions: str,
        template: str = "",
        sections: str = "",
        description: str = "",
        tone: str = "",
        detail: str = "standard",
        meetings: str = "one",
        window_days: int = 7,
        deliver_as: str = "chat",
        exclude_personal: bool = True,
    ) -> dict:
        """Save a repeatable meeting job as one of the user's skills, run later with /name.

        Use when the user wants to reuse a whole request: "save that as a
        skill", "every client call should get a debrief with X, Y, Z in this
        tone", "make a /weekly-digest that covers my last 7 days of meetings".
        Write `instructions` as the full playbook in plain words (what to look
        for, what to ignore, how to decide), so it stands alone without this
        conversation. For the outline, either name one of their templates as
        `template`, or write `sections` (one line per section, "Title (layout):
        what goes in it"), which is saved as a template of the same name.
        Saving under a name they already have makes a new version.
        This pauses for the user's confirmation.

        Args:
            name: The skill's name; its slash name is made from it ("Client debrief" → /client-debrief).
            instructions: The playbook in plain words.
            template: Name of one of their saved templates for the outline.
            sections: Or the outline itself, as lines.
            description: One line on what it is for.
            tone: e.g. "concise, formal, client-facing".
            detail: "brief", "standard" or "exhaustive".
            meetings: "one" (a meeting at a time) or "many" (every meeting in a window).
            window_days: For "many", how many days back (1-31).
            deliver_as: "chat", "email_draft_to_me" or "email_draft_to_attendees"
                (a Gmail DRAFT the user reviews and sends — never sent by Cubie).
            exclude_personal: Leave personal matters out (default true).

        Returns:
            dict: status and a report confirming what was saved.
        """
        from rubik_core.meeting_skills import store
        from rubik_core.meeting_skills.sections import SectionsError, parse_sections

        try:
            template_id = None
            if (template or "").strip():
                view = store.get_template(self.user_email, template)
                if view is None:
                    return self._error(f"You have no template called '{template}'.")
                template_id = view["id"]
            elif (sections or "").strip():
                view, _ = store.save_template(
                    self.user_email,
                    name,
                    parse_sections(sections),
                    description=f"Outline for the {name} skill",
                    source_session_id=self.source_session_id,
                )
                template_id = view["id"]
            skill, created = store.save_skill(
                self.user_email,
                name,
                instructions,
                template_id=template_id,
                description=description,
                tone=tone,
                detail_level=detail,
                meetings=meetings,
                window_days=window_days,
                deliver_as=deliver_as,
                exclude_personal=exclude_personal,
                source_session_id=self.source_session_id,
            )
        except (SectionsError, store.StoreError) as exc:
            return self._error(str(exc))
        verb = "Saved" if created else f"Updated (now version {skill['version']})"
        scope = (
            f"every meeting from the last {skill['window_days']} days"
            if skill["meetings"] == "many"
            else "one meeting at a time"
        )
        return {
            "status": "success",
            "report": (
                f"{verb} your skill **/{skill['slug']}** ({skill['name']}). It covers "
                f"{scope} and delivers {_DELIVER_WORDS[skill['deliver_as']]}. "
                f"Type /{skill['slug']} in the chat box to run it. It can also run "
                "on its own: on a schedule, or whenever a new transcript of yours is "
                "ready — just ask."
            ),
        }

    def delete_meeting_skill(self, name: str) -> dict:
        """Delete one of the user's meeting skills, for good, with any routine that runs it.

        This pauses for the user's confirmation.

        Args:
            name: The skill's name or slash name (from `list_meeting_skills`).

        Returns:
            dict: status and a report.
        """
        from rubik_core.meeting_skills import store

        try:
            view = store.delete_skill(self.user_email, name)
        except store.StoreError as exc:
            return self._error(str(exc))
        note = ""
        if view.get("routines_removed"):
            note = f" Its routines were removed too: {', '.join(view['routines_removed'])}."
        return {
            "status": "success",
            "report": f"Deleted the skill **/{view['slug']}**.{note}",
        }

    # ── routines ────────────────────────────────────────────────────────────

    def create_meeting_routine(
        self,
        skill: str,
        name: str = "",
        title_contains: str = "",
        external_only: bool = False,
        deliver_by_slack: bool = True,
    ) -> dict:
        """Make a skill run on its own whenever a new transcript of the user's is ready.

        Use for "after every client call, do the debrief", "whenever a
        recording of mine finishes, summarize the action items", "after each
        L10, run /l10-recap". For a CLOCK ("every Monday at 9", "weekdays at
        8:30"), use `create_scheduled_task` instead, with an instruction such
        as "Run my saved meeting skill /weekly-digest over my meetings from the
        last 7 days." If no saved skill fits, offer to save one first.
        This pauses for the user's confirmation.

        Args:
            skill: The skill's name or slash name.
            name: A name for the routine; defaults to the skill's.
            title_contains: Only meetings whose title contains this text
                (e.g. "L10"). Blank for every meeting.
            external_only: Only meetings with someone from outside the company.
            deliver_by_slack: Also send a Slack DM when it runs (default true).

        Returns:
            dict: status and a report confirming the routine.
        """
        from rubik_core.meeting_skills import routines

        try:
            view = routines.create_routine(
                self.user_email,
                skill,
                name=name,
                title_contains=title_contains,
                external_only=external_only,
                deliver_slack=deliver_by_slack,
                source_session_id=self.source_session_id,
            )
        except routines.RoutineError as exc:
            return self._error(str(exc))
        return {
            "status": "success",
            "report": (
                f"Done: {routines.describe(view)}\n\nEach result arrives as its own "
                'conversation in your "Scheduled tasks" chat folder'
                + (" and as a Slack DM" if view["deliver_slack"] else "")
                + ". It starts with meetings from now on, not old ones. Ask me any "
                "time to list, pause, resume or delete it."
            ),
        }

    def set_meeting_routine_paused(self, name: str, paused: bool) -> dict:
        """Pause or resume one of the user's meeting routines.

        Args:
            name: The routine's name (from `list_meeting_skills`).
            paused: true to pause, false to resume.

        Returns:
            dict: status and a report.
        """
        from rubik_core.meeting_skills import routines

        try:
            view = routines.set_paused(self.user_email, name, bool(paused))
        except routines.RoutineError as exc:
            return self._error(str(exc))
        return {"status": "success", "report": routines.describe(view)}

    def delete_meeting_routine(self, name: str) -> dict:
        """Delete one of the user's meeting routines, for good. Pausing can be undone; this cannot.

        This pauses for the user's confirmation.

        Args:
            name: The routine's name (from `list_meeting_skills`).

        Returns:
            dict: status and a report.
        """
        from rubik_core.meeting_skills import routines

        try:
            view = routines.delete_routine(self.user_email, name)
        except routines.RoutineError as exc:
            return self._error(str(exc))
        return {
            "status": "success",
            "report": f"Deleted the routine **{view['name']}**.",
        }
