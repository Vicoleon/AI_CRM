"""Light PII redaction for L10 session notes before they hit the RAG corpus.

The risk: L10 notes carry PII, so email and phone patterns are redacted
before chunking. This module is intentionally conservative —
it replaces email addresses and (US-style) phone numbers with placeholder
tokens. It does NOT attempt to redact names, addresses, or anything more
nuanced. Notes are still reviewable in the EOS UI; this only protects
data that lands in the retrieval index.

Future spec (15+): consider routing notes through a proper PII service
(e.g. Cloud DLP) if the company ever ingests sensitive personal information.
"""

from __future__ import annotations

import re

# Conservative patterns. Matching too broadly risks corrupting legitimate
# scorecard numbers; under-matching is preferred over over-matching.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\d)"  # not preceded by a digit (avoids matching part of a longer number)
    r"(?:\+?1[-.\s]?)?"
    r"\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}"
    r"(?!\d)",  # not followed by a digit
)


def redact_pii(text: str) -> str:
    """Return `text` with email addresses and phone numbers replaced by
    placeholder tokens. Idempotent."""
    if not text:
        return text
    text = _EMAIL_RE.sub("[redacted-email]", text)
    text = _PHONE_RE.sub("[redacted-phone]", text)
    return text
