"""Build the personalization blocks injected into user-facing agents.

Given a user + DB session, return a string suitable for prepending to a system
instruction or user message. Returns "" when the user has no relevant prefs
saved so callers can do `block = build_user_context_block(...); if block: ...`.

Two builders:
  * :func:`build_user_context_block` — email-voice block (tone / writing style /
    greeting + role & focus). Used by `routers/email.py::draft_sales_email`
    (UC-7) and `routers/home.py::generate_meeting_prep_analysis` (UC-6).
  * :func:`build_chat_personalization_context` — Chat block (role & focus +
    answer verbosity + reply language). Used by `routers/chat.py`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from rubik_core.auth.registry import UserProfile
from rubik_core.company.profile import company_name_or_default, get_company_profile
from rubik_core.database.models import UserPreference

# Common reply-language codes → display name for the prompt instruction. An
# unmapped non-empty value is used verbatim (e.g. an IETF tag we don't list).
_LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "pt": "Portuguese",
    "de": "German",
}


def get_user_preferences(db: Session, user_email: str) -> UserPreference | None:
    return (
        db.query(UserPreference)
        .filter(UserPreference.user_email == user_email)
        .one_or_none()
    )


def _member_of(user: UserProfile, db: Session) -> str:
    """``"a member of the Sales team at Acme Corp"``, or ``"a member of the
    team at the company"`` when the department or the company profile is empty,
    so the prompt never says "None" or leaves a blank."""
    department = (getattr(user, "department", None) or "").strip()
    team = f"the {department} team" if department else "the team"
    company = company_name_or_default(get_company_profile(db))
    return f"a member of {team} at {company}"


def build_user_context_block(user: UserProfile, db: Session) -> str:
    """Return a personalization block for the user, or "" if no prefs are set.

    Always includes the user's department for context. Adds whichever pref
    fields the user has filled in. Does NOT instruct the agent to add a
    name/title closing — Gmail's Workspace signature is appended automatically
    on draft creation, so any AI-written name/title would duplicate it.
    """
    prefs = get_user_preferences(db, user.email)
    has_prefs = prefs is not None and any(
        [
            prefs.writing_style,
            prefs.tone,
            prefs.preferred_greeting,
            prefs.additional_instructions,
            prefs.role_title,
            prefs.about_me,
        ]
    )
    if not has_prefs:
        return ""

    role = f" ({prefs.role_title})" if prefs.role_title else ""
    lines: list[str] = [
        "## User personalization",
        f"You are writing on behalf of {_member_of(user, db)}{role}.",
        "Do NOT add a name, title, or signature at the end of any email — "
        "the user's Google Workspace signature is appended automatically.",
    ]
    if prefs.about_me:
        lines.append(f"About the user (their role & focus): {prefs.about_me}")
    if prefs.tone:
        lines.append(f"Tone: {prefs.tone}.")
    if prefs.writing_style:
        lines.append(f"Writing style: {prefs.writing_style}")
    if prefs.preferred_greeting:
        lines.append(f"Preferred greeting: {prefs.preferred_greeting}")
    if prefs.additional_instructions:
        lines.append(
            f"Additional instructions from the user: {prefs.additional_instructions}"
        )

    return "\n".join(lines)


def build_chat_personalization_context(user: UserProfile, db: Session) -> str:
    """Return a Chat personalization block, or "" if nothing relevant is set.

    Distinct from :func:`build_user_context_block` (which is email-specific):
    this carries the user's role & focus plus how they want Cubie to talk to
    them (answer verbosity, reply language). Prepended to the chat coordinator
    instruction. Safe no-op ("") when the user has set none of these fields.
    """
    prefs = get_user_preferences(db, user.email)
    if prefs is None:
        return ""

    verbosity_line = {
        "concise": "Keep answers brief and to the point — lead with the answer, "
        "minimal preamble.",
        "detailed": "Give thorough, well-explained answers with relevant context "
        "and supporting detail.",
    }.get((prefs.chat_verbosity or "").strip())

    language_line = None
    lang = (prefs.response_language or "").strip()
    if lang and lang != "auto":
        name = _LANGUAGE_NAMES.get(lang.lower(), lang)
        language_line = (
            f"Always respond in {name}, regardless of the language of the question."
        )

    role = f" ({prefs.role_title})" if prefs.role_title else ""
    detail_lines: list[str] = []
    if prefs.about_me:
        detail_lines.append(f"About the user (their role & focus): {prefs.about_me}")
    if verbosity_line:
        detail_lines.append(verbosity_line)
    if language_line:
        detail_lines.append(language_line)

    if not detail_lines and not role:
        return ""

    header = (
        f"## About the person you're helping\n"
        f"You are assisting {_member_of(user, db)}{role}."
    )
    return "\n".join([header, *detail_lines])
