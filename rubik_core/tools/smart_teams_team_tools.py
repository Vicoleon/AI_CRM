"""Team Builder chat tool (ST-9) — "get me a team who can do X".

One read-only tool on the Cubie coordinator: the user describes a piece of work in
skills, a window, a size and a style mix, and gets back a group with what it
covers, who is around, and what is missing. It is the chat half of
``backend/routers/smart_teams_teams.py`` and it goes through exactly the same
matcher, so the page and chat cannot drift into two different answers.

Five properties are load-bearing, and each one is a bug shipped somewhere before:

1. **Identity is a constructor field, never a tool parameter.** ``user_email`` is
   not in the signature, so it is not in the JSON schema the model sees and the
   model cannot pass it. A Smart Teams surface that accepted an address would be
   a plain impersonation primitive over people's private interview answers.
2. **No principal, no answer.** The email is resolved to a real ``UserProfile``
   through ``UserRegistry`` and put through ``user_can_access_full_app`` — the
   pilot gate plan.md Decision #4 puts Team Builder behind — before anything is
   read. A chat tool has no FastAPI dependency to do this for it, and this
   container can be constructed with any string: ``adk_agent/agent.py`` builds
   ``AgentTools(user_email="example@example.com")`` at module scope for
   ``adk web`` discovery, and an address like that ON the Workspace domain
   makes ``authz.is_employee`` say True for a person who does not exist. Hence a
   separate container that is registered ONLY on the coordinator, and a lookup
   that fails closed with an explicit refusal rather than an empty result.
3. **Every refusal is written for the model, not for the user.** Nothing this
   returns reaches a person directly — ``adk_orchestrator`` hands the dict back to
   the model and drops ``error_message`` into a debug payload. So "nobody has
   confirmed that skill" carries the instruction not to turn it into "the company has
   nobody who can do that".
4. **An unrecognised skill is reported, never dropped.** ``skill_taxonomy.resolve``
   is exact; a phrase that misses is echoed back with the closest names Cubie
   does track. Silently dropping it would answer with a group matched on fewer
   skills than were asked for, which reads as a complete answer.
5. **The answer cannot rank anybody.** That is
   ``rubik_core.smart_teams.team_contracts`` (no type can hold a per-person
   score) and ``team_prompts`` (no sentence may evaluate one). This module adds
   the third layer that matters in chat: the ``report`` tells the model, in
   words, that the group is one combination and not a shortlist — because "get me
   the BEST team" is how the request is actually phrased.

Async, with the database work on a worker thread: a chat turn runs on the event
loop and this does several indexed queries plus one Gemini call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from rubik_core.brand import PRODUCT_NAME

logger = logging.getLogger(__name__)

#: Free text → a list of skill phrases. Commas, semicolons, newlines, " and ",
#: " plus ", "/" and "&". Deliberately dumb: every fragment is then resolved
#: EXACTLY against the taxonomy, so a bad split produces a reported
#: "Cubie does not track a skill called X" rather than a wrong match.
_SPLIT = re.compile(r"\s*(?:,|;|\n|/|&|\band\b|\bplus\b)\s*", re.IGNORECASE)

_NO_PRINCIPAL = {
    "status": "error",
    "error_message": (
        f"{PRODUCT_NAME} could not confirm who is asking, so it did not run the Team "
        "Builder. This tool reads colleagues' confirmed skills and published "
        "working guides, which is never done without a signed-in person to "
        "attribute it to. Tell the user Team Builder needs them signed in, and "
        "do NOT report that nobody matched — nothing was searched."
    ),
}

_NOT_IN_PILOT = {
    "status": "error",
    "error_message": (
        "Team Builder comes with the copilot, which is not turned on for this "
        "user. Say exactly that, and that their admin can turn it on. Do NOT say "
        "the search found nobody, and do not offer to look again — a permissions "
        "answer is not an empty result. Everyone at the company can still take "
        "the Work Style Interview and publish their own guide."
    ),
}

_OFF_DOMAIN = {
    "status": "error",
    "error_message": (
        "Smart Teams is limited to the company's Workspace accounts and this identity is "
        "not one, so nothing was read. Say that plainly and do not report an "
        "empty result."
    ),
}

_NO_SKILLS = {
    "status": "error",
    "error_message": (
        "No skill was named. Team Builder matches against the skills people have "
        "CONFIRMED in Smart Teams, so it needs at least one. Ask the user which "
        "capabilities the work actually needs — do not guess skills for them, and "
        "do not run this tool again with invented ones."
    ),
}


def _phrases(raw: str) -> list[str]:
    """Split one free-text skill string into candidate phrases."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    seen: list[str] = []
    for fragment in _SPLIT.split(raw):
        phrase = fragment.strip(" .\t")
        if phrase and phrase.lower() not in {p.lower() for p in seen}:
            seen.append(phrase)
    return seen


def _parse_date(value: str, *, label: str) -> tuple[date | None, dict | None]:
    """``(date, refusal)`` — exactly one is non-``None``. ISO only, never guessed.

    A date Cubie guessed wrong silently filters real people out of a staffing
    answer, so an unparseable one is a refusal the model has to relay rather than
    a window quietly reset to today.
    """
    if not isinstance(value, str) or not value.strip():
        return None, None
    try:
        return date.fromisoformat(value.strip()), None
    except ValueError:
        return None, {
            "status": "error",
            "error_message": (
                f"'{value}' is not a date {PRODUCT_NAME} can read for {label}. Ask the "
                "user for the window as YYYY-MM-DD. Do not run Team Builder "
                "with a date you inferred — a wrong window drops real people "
                "out of the answer for time off they are not taking."
            ),
        }


class TeamBuilderChatTools:
    """The Team Builder chat tool, bound to one signed-in user.

    Mirrors ``AgentTools``: identity comes from the constructor, so no tool
    signature exposes it and the model cannot redirect it. Its own container
    rather than a method on ``AgentTools`` because ``AgentTools`` is constructed
    with a caller-supplied address in several non-chat paths (``routers/home.py``,
    ``nightly_pipeline``, the X-Ray agents) — sharing it is how an unprincipled
    call would eventually reach this read.
    """

    def __init__(self, user_email: str):
        self.user_email = (user_email or "").strip().lower()

    # ── plumbing ────────────────────────────────────────────────────────────

    def _principal(self) -> tuple[object | None, dict | None]:
        """``(profile, refusal)`` — exactly one is non-``None``.

        Three ways to fail, three different sentences, because they need three
        different actions from the user: not a real account, not on the Workspace
        domain, not on the pilot. All of them fail CLOSED — this container can be
        built with any string, and "we could not tell who you are" must never
        become "nobody matched your ask".
        """
        from backend.auth import user_can_access_full_app
        from rubik_core.auth.registry import UserRegistry
        from rubik_core.smart_teams import authz

        if not authz.is_employee(self.user_email) or authz.is_impersonation_identity(
            self.user_email
        ):
            return None, _OFF_DOMAIN
        try:
            profile = UserRegistry().get_user(self.user_email)
        except Exception:  # noqa: BLE001 — a lookup outage is not an empty result
            logger.warning(
                "[smart-teams] team builder: principal lookup failed for %s",
                self.user_email,
                exc_info=True,
            )
            return None, _NO_PRINCIPAL
        if profile is None:
            return None, _NO_PRINCIPAL
        if not user_can_access_full_app(profile):
            return None, _NOT_IN_PILOT
        return profile, None

    # ── the tool ────────────────────────────────────────────────────────────

    async def build_project_team(
        self,
        skills: str = "",
        nice_to_have: str = "",
        size: int = 0,
        starts_on: str = "",
        ends_on: str = "",
        style_mix: str = "",
        ask: str = "",
    ) -> dict:
        """Suggest a group of colleagues for a piece of work, by skills needed.

        Use this for "get me a team who can do X", "who could staff a GDPR
        review in September", "put together a group for the quality assurance
        project", "I need three people for an external audit", "who can cover
        data privacy and financial reporting while Sam is out".

        It matches ONLY against: the skills people have CONFIRMED about
        themselves in Smart Teams, the working-style layers of people who have
        PUBLISHED their own guide, and scheduled time off. It is a suggestion
        about coverage and availability.

        NOT A RANKING — this is the most important thing about this tool. It does
        not assess, rate, rank or compare colleagues, and there is no "best"
        team. When more than one person has confirmed a skill, the choice between
        them is alphabetical. Present the result as one group that covers the ask.
        Never say somebody is the best, the strongest, better suited, or the
        obvious choice, and never say who should lead it.

        OUT OF SCOPE — this tool CANNOT do the following, do not call it for them
        and never imply Smart Teams was consulted for them: how busy anybody is,
        anybody's workload, capacity or utilisation (Cubie deliberately holds no
        such data); performance, seniority, promotion-readiness or salary; who
        reports to whom; anybody's interview answers or scores; skills somebody
        has NOT confirmed. Personnel decisions are not what this is for.

        GROUNDING: pass only skills the user actually named. Never invent one to
        make a team look fuller. A phrase Cubie does not track comes back as
        unrecognised — relay that, and offer the names it suggests.

        Args:
            skills: The capabilities the work needs, as the user said them —
                comma-separated or "X and Y" ("data privacy, contract negotiation").
                Required.
            nice_to_have: Capabilities that would help but are not required.
            size: How many people they asked for. 0 means they did not say, and 0
                is the right value then — do not invent a size.
            starts_on: Project start as YYYY-MM-DD. Empty if they gave no dates.
            ends_on: Project end as YYYY-MM-DD.
            style_mix: "complementary" if they asked for a spread of working
                styles, "aligned" if they want people who work the same way,
                empty if they did not say.
            ask: The user's request in their own words, for the write-up.

        Returns:
            dict: status and a report giving the group, what each person has
            confirmed, who is out, what the group's working styles add up to,
            what is not covered and what would fix it — or an explicit refusal
            saying what was not read and why.
        """
        return await asyncio.to_thread(
            self._build_project_team,
            skills,
            nice_to_have,
            size,
            starts_on,
            ends_on,
            style_mix,
            ask,
        )

    def _build_project_team(
        self,
        skills: str,
        nice_to_have: str,
        size: int,
        starts_on: str,
        ends_on: str,
        style_mix: str,
        ask: str,
    ) -> dict:
        from rubik_core.database.db import SessionLocal
        from rubik_core.smart_teams import team_builder, team_contracts

        _profile, refusal = self._principal()
        if refusal:
            return refusal

        required = _phrases(skills)
        optional = _phrases(nice_to_have)
        if not required and not optional:
            return _NO_SKILLS
        if len(required) + len(optional) > team_builder.MAX_REQUESTED_SKILLS:
            return {
                "status": "error",
                "error_message": (
                    "That names more skills than one group could cover — at least "
                    "one person per skill. Ask the user which of them actually "
                    f"gate the work and pass at most "
                    f"{team_builder.MAX_REQUESTED_SKILLS}."
                ),
            }

        start, bad_start = _parse_date(starts_on, label="the start date")
        if bad_start:
            return bad_start
        end, bad_end = _parse_date(ends_on, label="the end date")
        if bad_end:
            return bad_end

        wanted_size: int | None = None
        if isinstance(size, int) and size > 0:
            if size > team_contracts.MAX_TEAM_SIZE:
                return {
                    "status": "error",
                    "error_message": (
                        f"Team Builder proposes at most "
                        f"{team_contracts.MAX_TEAM_SIZE} people. Ask the user to "
                        "narrow the ask; do not retry with a smaller number you "
                        "chose for them."
                    ),
                }
            wanted_size = size

        mix = (style_mix or "").strip().lower()
        if mix not in team_contracts.STYLE_MIX_VALUES:
            # "They did not say" is the default and must stay the default:
            # guessing "complementary" invents a requirement and then reports a
            # shortfall against it.
            mix = team_contracts.STYLE_MIX_UNSPECIFIED

        try:
            request = team_contracts.TeamRequest.from_phrases(
                requested_by=self.user_email,
                required_phrases=required,
                optional_phrases=optional,
                size=wanted_size,
                starts_on=start,
                ends_on=end,
                style_mix=mix,
                raw_ask=(ask or "").strip(),
            )
        except team_contracts.TeamError as exc:
            return {
                "status": "error",
                "error_message": (
                    f"{PRODUCT_NAME} could not read that ask: {exc} Relay the reason and "
                    "ask the user to restate it."
                ),
            }

        db = SessionLocal()
        try:
            answer = team_builder.answer_for(db, request)
            report = team_builder.render_report(answer)
            suggestions = _suggestions_for(db, request)
        except Exception:  # noqa: BLE001 — a failure is not an empty team
            logger.exception("[smart-teams] team builder chat tool failed")
            return {
                "status": "error",
                "error_message": (
                    "Team Builder could not complete this run. Say that it "
                    "failed and offer to try again. Do NOT report that nobody "
                    "matched — nothing was searched."
                ),
            }
        finally:
            db.close()

        if suggestions:
            report += (
                f"\n\nClosest skill names {PRODUCT_NAME} does track: " + suggestions
            )

        if not answer.result.candidates:
            return {"status": "not_found", "report": report}
        return {"status": "success", "report": report}


def _suggestions_for(db, request) -> str:
    """Taxonomy names close to each unrecognised phrase, for the user to pick.

    Offered to the REQUESTER, never applied: ``skill_taxonomy.resolve`` is exact
    and a near-miss coerced onto a neighbouring skill is somebody staffed onto
    work they have never touched.
    """
    from rubik_core.smart_teams import skill_taxonomy

    parts: list[str] = []
    for unresolved in request.unresolved_skills:
        rows = skill_taxonomy.search(db, query=unresolved.phrase, limit=3)
        if not rows:
            words = [w for w in re.split(r"\s+", unresolved.phrase) if len(w) > 3]
            for word in words[:2]:
                rows = skill_taxonomy.search(db, query=word, limit=3)
                if rows:
                    break
        if rows:
            names = ", ".join(row.name for row in rows)
            parts.append(f"for '{unresolved.phrase}' — {names}")
    return "; ".join(parts)
