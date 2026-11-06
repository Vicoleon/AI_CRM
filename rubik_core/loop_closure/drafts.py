""" "Draft a reply" — a Gmail draft in the client's thread, never a send.

The one action on a "Waiting on your reply" row. It reads the thread from the
AE's OWN mailbox (Domain-Wide Delegation as that AE, in the request the AE
triggered), calls the workspace's selected JSON model for the reply body, and
creates a draft in
the same thread with ``GmailClient.create_draft(..., thread_id=…)`` — which
appends the AE's Gmail signature and the company's disclaimer, and never sends. The
draft carries ``In-Reply-To`` / ``References`` from the client's message, so it
threads on the client's side too, and its subject is ``Re: <subject>``.

The item stays open. It closes when the next scan sees a reply from the AE in
the thread, so a draft that is never sent is still a loose end.

Its own AI Cost category, ``loop_closure`` ("Loose Ends"). Nothing from the
thread is logged; a failure is a :class:`DraftError` whose message is written
for the AE.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import (
    ProviderRequestError,
    generate_text_on_route,
)
from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.profile import company_label, get_company_profile
from rubik_core.cost.usage import attribute_to
from rubik_core.logsafe import safe_error
from rubik_core.loop_closure.constants import COST_FEATURE

logger = logging.getLogger(__name__)

#: The last few messages the model reads, each clipped.
THREAD_TAIL = 6
MESSAGE_CHARS = 1500
MAX_BODY_CHARS = 4000

_WS = re.compile(r"\s+")
# The quoted history Gmail appends to a reply ("On Mon, Oct 5, 2026 at …
# wrote:" and everything after it). The model needs the new words, not five
# copies of the thread.
_QUOTED = re.compile(r"\n(?:On .{5,200}wrote:|-{2,} ?Original Message ?-{2,}).*", re.S)


class DraftError(RuntimeError):
    """A draft could not be made. ``str(exc)`` is safe to show the AE."""


def _headers(payload: dict) -> dict[str, str]:
    return {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in (payload.get("headers") or [])
    }


def _plain_text(payload: dict) -> str:
    """The first text/plain part of a ``format=full`` message, decoded."""
    if (payload.get("mimeType") or "").startswith("text/plain"):
        data = (payload.get("body") or {}).get("data")
        if data:
            try:
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                    "utf-8", "replace"
                )
            except (ValueError, TypeError):
                return ""
    for part in payload.get("parts") or []:
        text = _plain_text(part)
        if text:
            return text
    return ""


def thread_transcript(thread: dict, *, owner_email: str) -> tuple[str, str]:
    """``(subject, transcript)`` from a ``format=full`` thread, oldest first."""
    from email.utils import parseaddr

    owner = (owner_email or "").strip().lower()
    messages = list(thread.get("messages") or [])[-THREAD_TAIL:]
    subject = ""
    lines: list[str] = []
    for m in messages:
        payload = m.get("payload") or {}
        h = _headers(payload)
        subject = subject or (h.get("subject") or "").strip()
        name, addr = parseaddr(h.get("from", ""))
        who = "THE AE" if (addr or "").lower() == owner else (name or addr or "?")
        body = _plain_text(payload) or m.get("snippet") or ""
        body = _QUOTED.sub("", body)
        body = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith(">")
        )
        lines.append(f"[{h.get('date', '')}] {who}:\n{body.strip()[:MESSAGE_CHARS]}")
    return subject, "\n\n".join(lines)


def build_prompt(*, subject: str, transcript: str, client_name: str) -> str:
    return (
        "You draft a reply email for an account executive (THE AE) at "
        f"{company_label(get_company_profile())}. The client is "
        f"waiting on THE AE. Reply to {client_name or 'the client'}'s latest "
        "message in the thread below.\n\n"
        "Rules:\n"
        "- Answer what the latest message asks, in THE AE's voice: warm, brief, "
        "professional, plain text, at most 150 words.\n"
        "- Never invent facts, prices, dates, deliverables or commitments that are "
        "not in the thread. Where an answer needs one, write a clear placeholder "
        "in square brackets, e.g. [confirm the date].\n"
        "- Start with a greeting using the client's first name. End with a short "
        "sign-off line and NO name or signature (Gmail adds the signature).\n"
        "- Do not quote the thread back.\n\n"
        'Return ONLY JSON: {"body": "..."}\n\n'
        f"Subject: {subject}\n\nTHREAD (oldest → newest):\n{transcript}"
    )


def _default_invoke(prompt: str) -> str | dict:
    from google.genai import types as genai_types

    from rubik_core.config import settings
    from rubik_core.gemini_tuning import generation_config_for
    from rubik_core.models import get_genai_client

    model = settings.VERTEX_LLM_MODEL_FLASH
    route = resolve_model_route(COST_FEATURE, "json", default_model=model)
    if route.provider != "vertex_managed":
        response = generate_text_on_route(
            route, prompt, json_mode=True, temperature=0.2
        )
        try:
            parsed = json.loads(response.text)
        except ValueError:
            raise ProviderRequestError(
                "Selected AI provider returned invalid reply JSON"
            ) from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("body"), str):
            raise ProviderRequestError(
                "Selected AI provider returned invalid reply JSON"
            )
        return parsed
    if resolve_model_route(COST_FEATURE, "json", default_model=model) != route:
        raise IncompatibleModelRoute("Workspace AI route changed during Loose Ends")
    response = get_genai_client().models.generate_content(
        model=route.model,
        contents=prompt,
        config=generation_config_for(
            route.model,
            genai_types.GenerateContentConfig(
                temperature=0.2, response_mime_type="application/json"
            ),
        ),
    )
    return response.text or ""


def parse_body(raw: Any) -> str:
    data = raw
    if isinstance(raw, str):
        text = (
            raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        )
        try:
            data = json.loads(text.strip())
        except ValueError:
            data = {"body": text.strip()}
    body = str((data or {}).get("body") or "") if isinstance(data, dict) else ""
    body = body.replace("\r\n", "\n").strip()
    return body[:MAX_BODY_CHARS]


def threading_headers(
    thread: dict, *, message_id: str | None, owner_email: str
) -> tuple[str | None, str | None]:
    """``(In-Reply-To, References)`` for a reply to the client's message.

    The replied-to message is the item's inbound one (``message_id``), else the
    newest message in the thread not from the AE. Its ``Message-ID`` becomes
    ``In-Reply-To``, and ``References`` is its own chain plus that id — what
    the client's mail program threads on. ``(None, None)`` when the thread
    carries no ``Message-ID`` (the draft is still filed in the Gmail thread).
    """
    from email.utils import parseaddr

    owner = (owner_email or "").strip().lower()
    messages = list(thread.get("messages") or [])
    target = next(
        (m for m in messages if message_id and m.get("id") == message_id), None
    )
    if target is None:
        for m in reversed(messages):
            _name, addr = parseaddr(_headers(m.get("payload") or {}).get("from", ""))
            if (addr or "").strip().lower() != owner:
                target = m
                break
    if target is None:
        return None, None
    h = _headers(target.get("payload") or {})
    mid = (h.get("message-id") or "").strip()
    if not mid:
        return None, None
    chain = " ".join((h.get("references") or "").split())
    references = f"{chain} {mid}".strip() if mid not in chain.split() else chain
    return mid, references


def reply_subject(subject: str) -> str:
    subject = _WS.sub(" ", subject or "").strip() or "Following up"
    return subject if re.match(r"(?i)^re:", subject) else f"Re: {subject}"


@attribute_to(COST_FEATURE)
def draft_reply(
    item: Any,
    *,
    owner_email: str,
    gmail: Any = None,
    invoke: Callable[[str], Any] | None = None,
) -> dict:
    """Create the draft; return ``{"draft_id": str, "draft_url": str}``."""
    owner = (owner_email or "").strip().lower()
    if (item.owner_email or "").lower() != owner:
        raise DraftError("This loose end belongs to someone else.")
    if item.kind != "open_loop" or not item.thread_id:
        raise DraftError("Only a 'Waiting on your reply' loose end can be drafted.")
    recipient = (item.counterparty_email or "").strip().lower()
    if not recipient or "@" not in recipient:
        raise DraftError(f"{PRODUCT_NAME} does not know who to reply to on this one.")

    try:
        if gmail is None:
            from rubik_core.tools.gmail import GmailClient

            gmail = GmailClient(user_email=owner)
        thread = gmail.get_thread(item.thread_id)
    except Exception as exc:  # noqa: BLE001 — a sentence, never a traceback
        logger.warning("[loop-closure] draft: thread read failed: %s", safe_error(exc))
        raise DraftError(
            f"{PRODUCT_NAME} could not read that email thread from your mailbox. "
            "Try again in a minute."
        ) from None

    subject, transcript = thread_transcript(thread or {}, owner_email=owner)
    if not transcript.strip():
        raise DraftError(
            "That email thread looks empty, so there is nothing to reply to."
        )

    try:
        raw = (invoke or _default_invoke)(
            build_prompt(
                subject=subject or item.title or "",
                transcript=transcript,
                client_name=item.counterparty_label or "",
            )
        )
        body = parse_body(raw)
    except (IncompatibleModelRoute, ProviderRequestError):
        logger.warning("[loop-closure] draft: selected AI provider unavailable")
        raise DraftError(
            "Your workspace's selected AI provider could not write this reply. "
            "Ask your admin to check its connection, then try again."
        ) from None
    except Exception as exc:  # noqa: BLE001
        # Even managed SDK errors may contain customer mail. Do not log text.
        logger.warning(
            "[loop-closure] draft: model call failed (%s)", type(exc).__name__
        )
        body = ""
    if not body:
        raise DraftError(
            f"{PRODUCT_NAME} could not write a draft just now. Try again in a minute."
        )

    in_reply_to, references = threading_headers(
        thread or {}, message_id=getattr(item, "message_id", None), owner_email=owner
    )
    # Only passed when known, so a client without the threading parameters
    # (an older double) is called exactly as before.
    threading = (
        {"in_reply_to": in_reply_to, "references": references} if in_reply_to else {}
    )
    try:
        created = gmail.create_draft(
            to=[recipient],
            subject=reply_subject(subject or item.title or ""),
            body_markdown=body,
            thread_id=item.thread_id,
            **threading,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[loop-closure] draft: Gmail draft failed: %s", safe_error(exc))
        raise DraftError(
            "Gmail did not accept the draft. Try again in a minute."
        ) from None
    draft_id = str((created or {}).get("draft_id") or "")
    return {
        "draft_id": draft_id,
        "draft_url": (created or {}).get("gmail_draft_url")
        or f"https://mail.google.com/mail/u/0/#drafts?compose={draft_id}",
    }
