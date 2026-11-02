"""Error text that is safe to log: never a credential.

httpx copies an illegal header value into its exception message verbatim, so a
token saved with a trailing newline reaches the logs whole. Where an
authenticated call can fail, log ``safe_error(exc)`` instead of the traceback.
"""

from __future__ import annotations

import re

# A refused header, quoted whole: h11 (under httpx) says "Illegal header value
# b'...'", requests says "in header value: '...'". Masked as one unit, because a
# header without a "Bearer " prefix (X-Api-Key, X-DD-Token) has nothing else to
# match on.
_REFUSED_HEADER = re.compile(r"(header value:?\s+)b?(['\"])(?:\\.|(?!\2).)*\2")
_CREDENTIAL = re.compile(
    r"(Bearer\s+|token\s+)\S+|github_pat_\w+|gh[pousr]_\w+|\bpat-[a-z]{2}\d-[\w-]+",
    re.IGNORECASE,
)
_LIMIT = 300


def safe_error(exc: BaseException) -> str:
    """``Type: message`` with anything token-shaped masked, capped in length."""
    text = _REFUSED_HEADER.sub(r"\1[redacted]", f"{type(exc).__name__}: {exc}")
    masked = _CREDENTIAL.sub(lambda m: (m.group(1) or "") + "[redacted]", text)
    return masked[:_LIMIT]
