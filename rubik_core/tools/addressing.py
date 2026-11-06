"""How was the reader addressed on a message? (To / Cc / distribution list)

Lives in ``rubik_core`` — NOT in ``backend`` — because two independent surfaces
need the same answer and must never disagree about it:

  * the Email view (``backend/routers/home.py``), which hides bulk mail and
    ranks direct above cc, and
  * the AI-Priorities Gmail extractor
    (``rubik_core/priorities/agents/gmail_agent.py``), which must not turn an
    "ACTION NEEDED" email addressed to somebody else into the reader's own
    deadline.

``backend.services.gmail_service`` re-exports these names, so the older import
path keeps working.
"""

from __future__ import annotations

from email.utils import getaddresses
from typing import Literal

# The Email surface is a personal-attention view. We classify each message by
# how the requesting user was addressed and DROP distribution-list / bulk mail:
# a user on a busy list (e.g. compliance@) does not want every list message in
# their Cubie inbox — only mail aimed at them. Within the kept set, mail sent
# directly To: the user outranks Cc:.
Addressing = Literal["direct", "cc", "list"]

# RFC 3834 / common bulk markers. Their presence means "broadcast / automated"
# even when the user happens to be a named recipient (e.g. a marketing blast).
_BULK_PRECEDENCE = {"bulk", "list", "junk", "auto_reply", "auto_replied", "autoreply"}

_ADDRESSING_RANK: dict[str, int] = {"direct": 0, "cc": 1, "list": 2}


def _address_set(header_value: str) -> set[str]:
    """Lower-cased set of email addresses in a To/Cc header value."""
    return {addr.lower() for _name, addr in getaddresses([header_value or ""]) if addr}


def classify_addressing(headers: dict[str, str], user_email: str) -> Addressing:
    """How was ``user_email`` addressed on this message?

    - ``"direct"``: the user's own address is in ``To:``.
    - ``"cc"``:     the user's own address is in ``Cc:`` (and not ``To:``).
    - ``"list"``:   the user is not individually addressed (received via a
      distribution list / bcc expansion), OR the message carries bulk/list
      markers (``List-Id``, ``List-Unsubscribe``, ``Precedence: bulk|list``).

    Expects ``headers`` keyed by lower-cased header name.
    """
    me = (user_email or "").strip().lower()
    has_bulk_markers = bool(
        headers.get("list-id")
        or headers.get("list-unsubscribe")
        or headers.get("precedence", "").strip().lower() in _BULK_PRECEDENCE
    )
    if me and me in _address_set(headers.get("to", "")):
        return "list" if has_bulk_markers else "direct"
    if me and me in _address_set(headers.get("cc", "")):
        return "list" if has_bulk_markers else "cc"
    # Not individually in To/Cc → reached the user via a distribution list / bcc.
    return "list"


def focus_sort_key(addressing: str, is_unread: bool) -> tuple[int, int]:
    """Stable-sort key for a focused inbox view: direct above cc, unread first.

    Relies on Gmail returning newest-first; a stable sort then preserves recency
    within each (addressing, unread) group without re-parsing Date headers.
    """
    return (_ADDRESSING_RANK.get(addressing, 2), 0 if is_unread else 1)


def strongest_addressing(values: list[str]) -> Addressing:
    """The most-direct addressing seen across a thread's messages.

    A thread where the reader was ``To:``'d on the first message and only
    ``Cc:``'d on the last is still *their* thread — reading only the newest
    message would misfile it as somebody else's. ``"list"`` wins only when no
    message ever addressed the reader individually.
    """
    best: Addressing = "list"
    for v in values:
        if _ADDRESSING_RANK.get(v, 2) < _ADDRESSING_RANK[best]:
            best = v  # type: ignore[assignment]  # ranked keys are Addressing
    return best
