"""The judge — one Flash call per AE per morning (README §4.6).

Code finds the candidates; the model only answers the two questions code
cannot: does this unanswered client email really expect a reply (and is it a
business request), and does this Slack message really report a client
conversation, call or meeting. Everything else — who, when, which record, how
old — was decided before the call and is rendered after it.

Rules the call keeps, after ``backend/services/open_loops.py``:

* **Tags are the only handle.** Each email is ``E1..`` and each Slack message
  ``S1..``; a verdict for a tag it was not handed, or a second verdict for one
  it was, is dropped. The model cannot add an item, only decline one.
* **Headlines are plain text, at most 120 characters**, and owner-only: they
  are stored in ``loop_items.headline`` and never reach a team surface.
* **A failed call is None, never "nothing to report".** The scan then creates
  no NEW open-loop or Slack item for that AE (existing ones are still
  closure-checked) and reports coverage ``ai: unavailable``.

Model: the workspace's ``loop_closure`` JSON route, defaulting to the managed
Flash model, with ``temperature=0``. A selected-provider failure leaves coverage
``ai: unavailable``. Its own AI Cost category is ``loop_closure`` ("Loose Ends").
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import (
    ProviderRequestError,
    generate_text_on_route,
)
from rubik_core.company.profile import company_possessive, get_company_profile
from rubik_core.cost.usage import attribute_to
from rubik_core.loop_closure.constants import COST_FEATURE

logger = logging.getLogger(__name__)

#: Caps per call, oldest first. Declined candidates are remembered
#: (``loop_judgements``) and never sent again, so the cap rotates through a
#: backlog; the over-cap rest wait for the next run and are counted in coverage.
MAX_EMAILS = 40
MAX_SLACK = 30
MESSAGES_PER_EMAIL = 4
SNIPPET_CHARS = 300
SLACK_CHARS = 400
HEADLINE_CHARS = 120

_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class EmailInput:
    tag: str
    sender_name: str
    sender_domain: str
    hubspot_label: (
        str  # "HubSpot contact at Acme Corp" | "Client domain …" | "Not in HubSpot"
    )
    subject: str
    # (from, date, snippet), oldest first, at most MESSAGES_PER_EMAIL.
    messages: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class SlackInput:
    tag: str
    channel: str
    date: str
    text: str
    company: str


@dataclass(frozen=True)
class EmailVerdict:
    tag: str
    expects_reply: bool
    business_request: bool
    headline: str


@dataclass(frozen=True)
class SlackVerdict:
    tag: str
    reports_client_touch: bool
    headline: str


@dataclass
class JudgeResult:
    emails: dict[str, EmailVerdict] = field(default_factory=dict)
    slack: dict[str, SlackVerdict] = field(default_factory=dict)


# ── Inputs ───────────────────────────────────────────────────────────────────


def _clip(text: str, n: int) -> str:
    return _WS.sub(" ", text or "").strip()[:n]


def _when(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if dt else "?"


def email_input(tag: str, candidate: Any, *, owner: str) -> EmailInput:
    """Build an ``E#`` block from an open-loop candidate and its thread."""
    thread = candidate.context
    msgs = list(getattr(thread, "messages", []) or [])[-MESSAGES_PER_EMAIL:]
    rows = tuple(
        (
            "the AE" if m.from_email == owner else (m.from_name or m.from_email),
            _when(m.at),
            _clip(m.snippet, SNIPPET_CHARS),
        )
        for m in msgs
    )
    match = (candidate.detail or {}).get("match")
    company = candidate.company_label
    if match == "contact":
        label = f"HubSpot contact{f' at {company}' if company else ''}"
    elif match == "domain":
        label = f"From a client company{f' ({company})' if company else ''}"
    else:
        label = "Not found in HubSpot"
    sender = candidate.counterparty_email or ""
    return EmailInput(
        tag=tag,
        sender_name=candidate.counterparty_label or sender,
        sender_domain=sender.rsplit("@", 1)[1] if "@" in sender else "",
        hubspot_label=label,
        subject=_clip(candidate.title, 200),
        messages=rows,
    )


def slack_input(tag: str, candidate: Any) -> SlackInput:
    msg = candidate.context
    channel = getattr(msg, "channel_name", None)
    return SlackInput(
        tag=tag,
        channel=f"#{channel}" if channel else "a direct message",
        date=_when(candidate.opened_at),
        text=_clip(getattr(msg, "text", "") or candidate.title, SLACK_CHARS),
        company=candidate.company_label or "",
    )


# ── Prompt ───────────────────────────────────────────────────────────────────


def build_prompt(
    emails: list[EmailInput],
    slack: list[SlackInput],
    *,
    owner_name: str,
    today: str,
) -> str:
    blocks: list[str] = []
    for e in emails:
        lines = "\n".join(
            f"  - {who} · {when}: {text}" for who, when, text in e.messages
        )
        blocks.append(
            f"[{e.tag}] EMAIL from {e.sender_name} ({e.sender_domain}) — {e.hubspot_label}\n"
            f"  Subject: {e.subject}\n{lines}"
        )
    for s in slack:
        blocks.append(
            f"[{s.tag}] SLACK in {s.channel} on {s.date} — names {s.company}\n"
            f"  {s.text}"
        )
    return (
        "You review an account executive's (AE's) mail and Slack for loose ends. "
        f"The AE is {owner_name or 'the AE'}. Today is {today}.\n\n"
        "EMAIL items are threads where a person outside the company wrote LAST, "
        "directly to the AE, and the AE has not answered in two business days. "
        "For each one decide:\n"
        "- expects_reply: true only if the newest message asks the AE something, "
        "asks for something, or plainly waits on the AE. False for thank-yous, "
        "FYIs, confirmations, out-of-office or automated mail, newsletters, "
        "receipts, and threads that are already settled.\n"
        "- business_request: true if it is a client or prospect asking about "
        f"{company_possessive(get_company_profile())} services, pricing, a "
        "proposal, a contract, a deliverable or a meeting. False for vendors "
        "selling to the company, recruiters, personal mail.\n\n"
        "SLACK items are messages the AE wrote that name a client company. For "
        "each one decide:\n"
        "- reports_client_touch: true only if the AE says they actually spoke, "
        'met, called or emailed with that client ("just got off a call with…", '
        '"met with … today"). False for plans, questions, internal '
        "discussion about the account, or the company named in passing.\n\n"
        "For every item write a headline: plain text, at most 120 characters, "
        'what it is about ("Asks for updated onboarding pricing before Friday"). '
        "No markdown, no quotes around it.\n\n"
        "Rules:\n"
        "- Use ONLY the tags given (E1, S1, ...). Never invent a tag.\n"
        "- When unsure whether an email expects a reply, answer false: a missed "
        "flag costs less than a false one.\n\n"
        "Return ONLY JSON of this shape:\n"
        '{"emails": [{"tag": "E1", "expects_reply": true, "business_request": '
        'true, "headline": "..."}], "slack": [{"tag": "S1", '
        '"reports_client_touch": false, "headline": "..."}]}\n\n' + "\n\n".join(blocks)
    )


# ── Parsing ──────────────────────────────────────────────────────────────────


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "1"}


def _headline(value: Any) -> str:
    text = _WS.sub(" ", str(value or "")).strip().strip("\"'`*#").strip()
    return text[:HEADLINE_CHARS]


def parse_verdicts(
    raw: str | dict | None, email_tags: set[str], slack_tags: set[str]
) -> JudgeResult:
    """Model output → verdicts for the tags it was handed. Unknown and
    duplicate tags are dropped; anything malformed is simply absent."""
    data: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
        data = json.loads(text.strip()) if text.strip() else {}
    # JSON mode without a schema sometimes wraps the object in a list.
    if (
        isinstance(data, list)
        and len(data) == 1
        and isinstance(data[0], dict)
        and ("emails" in data[0] or "slack" in data[0])
    ):
        data = data[0]
    if not isinstance(data, dict):
        data = {}
    result = JudgeResult()
    for row in data.get("emails") or []:
        if not isinstance(row, dict):
            continue
        tag = str(row.get("tag") or "").strip().upper()
        if tag not in email_tags or tag in result.emails:
            continue
        result.emails[tag] = EmailVerdict(
            tag=tag,
            expects_reply=_bool(row.get("expects_reply")),
            business_request=_bool(row.get("business_request")),
            headline=_headline(row.get("headline")),
        )
    for row in data.get("slack") or []:
        if not isinstance(row, dict):
            continue
        tag = str(row.get("tag") or "").strip().upper()
        if tag not in slack_tags or tag in result.slack:
            continue
        result.slack[tag] = SlackVerdict(
            tag=tag,
            reports_client_touch=_bool(row.get("reports_client_touch")),
            headline=_headline(row.get("headline")),
        )
    return result


# ── The call ─────────────────────────────────────────────────────────────────


def _default_invoke(prompt: str) -> str:
    from google.genai import types as genai_types

    from rubik_core.config import settings
    from rubik_core.gemini_tuning import generation_config_for
    from rubik_core.models import get_genai_client

    model = settings.VERTEX_LLM_MODEL_FLASH
    route = resolve_model_route(COST_FEATURE, "json", default_model=model)
    if route.provider != "vertex_managed":
        return generate_text_on_route(route, prompt, json_mode=True, temperature=0).text
    # The managed path uses its native tuning, but must still honor a route
    # changed between resolution and model I/O.
    if resolve_model_route(COST_FEATURE, "json", default_model=model) != route:
        raise IncompatibleModelRoute("Workspace AI route changed during Loose Ends")
    response = get_genai_client().models.generate_content(
        model=route.model,
        contents=prompt,
        config=generation_config_for(
            route.model,
            genai_types.GenerateContentConfig(
                temperature=0, response_mime_type="application/json"
            ),
        ),
    )
    return response.text or ""


@attribute_to(COST_FEATURE)
def judge(
    emails: list[EmailInput],
    slack: list[SlackInput],
    *,
    owner_name: str = "",
    now: datetime | None = None,
    invoke: Callable[[str], Any] | None = None,
) -> JudgeResult | None:
    """One call for one AE. ``None`` when the call fails; an empty result when
    there was nothing to ask (no call is made)."""
    emails = emails[:MAX_EMAILS]
    slack = slack[:MAX_SLACK]
    if not emails and not slack:
        return JudgeResult()
    when = (now or datetime.now(timezone.utc)).strftime("%A, %B %d, %Y")
    prompt = build_prompt(emails, slack, owner_name=owner_name, today=when)
    try:
        raw = (invoke or _default_invoke)(prompt)
        result = parse_verdicts(raw, {e.tag for e in emails}, {s.tag for s in slack})
    except (IncompatibleModelRoute, ProviderRequestError):
        logger.warning("[loop-closure] selected AI provider unavailable for judge")
        return None
    except Exception as exc:  # noqa: BLE001 — "could not judge" is a coverage state
        # Provider parsers and SDKs can echo client mail. Only log the type.
        logger.warning("[loop-closure] judge call failed (%s)", type(exc).__name__)
        return None
    if not result.emails and not result.slack:
        # Questions were asked and not one tagged answer came back: a reply in
        # the wrong shape, not "everything declined". Treated as a failed call
        # (coverage ``ai: unavailable``), so nothing is silently dropped.
        logger.warning("[loop-closure] judge reply had no usable verdicts")
        return None
    return result
