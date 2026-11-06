"""Flatten attendee intelligence for the document exports (Word + PDF).

Returns style-tagged lines rather than rendering anything, so the content
decisions — what appears, in what order, with which labels — live here once while
each renderer keeps its own primitives (python-docx paragraphs, FPDF cells). The
alternative was the same layout written twice in two libraries, which is how the
two artifacts drift.

Styles a renderer must handle:

* ``person`` — a bold name/role header starting a new attendee
* ``muted``  — small grey supporting text (emails, URLs, provenance)
* ``label``  — a short bold-ish field label
* ``body``   — a normal paragraph
* ``bullet`` — a bulleted item
"""

from __future__ import annotations

from rubik_core.people.schemas import AttendeeIntel

Block = tuple[str, str]

_CONFIDENCE_LABEL = {
    "high": "Well evidenced",
    "medium": "Partly evidenced",
    "low": "Thin evidence",
}


def _person_blocks(intel: AttendeeIntel) -> list[Block]:
    out: list[Block] = []
    profile, persona = intel.profile, intel.persona

    name = intel.name or profile.name or intel.email
    role = " @ ".join(b for b in (profile.current_title, profile.current_company) if b)
    out.append(("person", f"{name} — {role}" if role else name))

    meta = [intel.email]
    if profile.tenure:
        meta.append(profile.tenure)
    if profile.location:
        meta.append(profile.location)
    out.append(("muted", "  ·  ".join(m for m in meta if m)))

    if profile.linkedin_url:
        out.append(("muted", f"LinkedIn: {profile.linkedin_url}"))

    if not intel.has_content():
        # Named explicitly, so a reader of the exported doc can tell "we looked
        # and found nothing" from "we never looked".
        out.append(
            (
                "body",
                "No public professional presence found. Nothing has been inferred "
                "about this person.",
            )
        )
        return out

    if profile.headline:
        out.append(("body", f"Headline: “{profile.headline}”"))
    if profile.career_summary:
        out.append(("body", profile.career_summary))
    if profile.credentials:
        out.append(("muted", "Credentials: " + "; ".join(profile.credentials)))

    if not persona.is_empty():
        band = _CONFIDENCE_LABEL.get(persona.confidence, "Thin evidence")
        basis = f" — based on {persona.basis}" if persona.basis else ""
        out.append(("label", f"How to approach ({band}{basis})"))
        if persona.communication_style:
            out.append(("body", f"Communication style: {persona.communication_style}"))
        if persona.decision_style:
            out.append(("body", f"How they decide: {persona.decision_style}"))
        if persona.posts_about:
            out.append(("muted", "Themes: " + ", ".join(persona.posts_about)))
        for item in persona.priorities:
            out.append(("bullet", f"Pushes publicly: {item}"))

    if intel.activity:
        out.append(("label", "Recent public activity"))
        for item in intel.activity:
            when = f"[{item.posted_at}] " if item.posted_at else ""
            out.append(("bullet", f"{when}{item.summary}"))
            if item.url:
                out.append(("muted", item.url))

    for opener in persona.conversation_openers:
        out.append(("bullet", f"Opener: {opener}"))
    for item in persona.avoid:
        out.append(("bullet", f"Would land badly: {item}"))

    return out


def attendee_intel_blocks(raw: list[dict] | None) -> list[Block]:
    """Style-tagged lines for the Attendee Intelligence section, or ``[]``.

    Empty means the caller omits the heading entirely — internal meetings, or a
    run where the feature was off.
    """
    people = [AttendeeIntel.from_dict(r) for r in (raw or []) if r]
    people = [p for p in people if p.email]
    if not people:
        return []

    blocks: list[Block] = [
        (
            "muted",
            "Public professional research on the external attendees. Style reads "
            "are labelled with how much evidence supports them — verify before "
            "quoting.",
        )
    ]
    for person in people:
        blocks.extend(_person_blocks(person))
    return blocks
