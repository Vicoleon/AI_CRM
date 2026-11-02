"""Plain-text normalization for CRM-sourced strings (UC-45).

HubSpot stores note bodies (``hs_note_body``) as rich text, so a real one comes
back as ``<div style="font-family: -apple-system, …">Signed the VM contract
amendment</div>`` while every other engagement title (call/email/task/meeting)
is already plain. The timeline renders titles as *text* — React escapes them —
so that markup showed up verbatim on screen and in the Word/PDF export, and it
also went into the synthesis prompt, where it burned tokens and let style
attributes bleed into the model's own wording. Notes were the only place it was
ever seen, which is exactly the one HubSpot field that is HTML.

Every CRM string goes through :func:`plain_text` on the way in (fetchers) and
again on the way out (router serialization), so snapshots persisted before this
fix render clean without a backfill.
"""

from __future__ import annotations

import html
import re

# Whole elements whose *content* is markup noise, not prose.
_DROP_ELEMENT_RE = re.compile(r"(?is)<(script|style|head)\b.*?</\1\s*>")
# Structural breaks — become a space, so words either side don't fuse.
_BREAK_RE = re.compile(r"(?i)<br\s*/?>|</(?:p|div|li|tr|td|h[1-6]|blockquote)\s*>")
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def plain_text(value: object, *, max_len: int | None = None) -> str:
    """Best-effort rich text → single-line plain text.

    Drops script/style blocks, turns structural tags into spaces, strips the
    remaining tags, unescapes entities (a second tag pass catches the
    double-escaped bodies HubSpot sometimes stores), and collapses whitespace.
    ``max_len`` truncates on a word boundary with an ellipsis.
    """
    if value in (None, ""):
        return ""
    s = str(value)
    if "<" in s or "&" in s:
        s = _DROP_ELEMENT_RE.sub(" ", s)
        s = _BREAK_RE.sub(" ", s)
        s = _TAG_RE.sub(" ", s)
        s = html.unescape(s)
        s = _TAG_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s.replace("\xa0", " ")).strip()
    if max_len is not None and len(s) > max_len:
        cut = s[:max_len]
        # Prefer a word boundary, but never throw away most of the string.
        space = cut.rfind(" ")
        if space > max_len * 0.6:
            cut = cut[:space]
        s = cut.rstrip(" ,.;:-") + "…"
    return s
