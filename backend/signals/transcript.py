"""Transcript SignalSource — reshapes meeting transcripts into UC-17 Signals.

Reuses `backend.services.transcript_service.TranscriptService` for DB + GCS
access; no DB/GCS calls happen in this module. Two signal types are emitted:

  - `action_item_assigned_to_rep` — substring/regex match on patterns like
    "<rep first name> will <verb>" or "action item: <rep> to ...".
  - `risk_language` — case-insensitive phrase match on a fixed risk-phrase
    list (budget freeze, on hold, competitor, etc.). `payload.matched_phrase`
    records which phrase triggered.

Everything subtler (positive_signal, buying_intent, next_step_mentioned) is
deliberately left to the Gemini judge (UC-17.6) — we keep the source lean.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from backend.signals.types import Signal

logger = logging.getLogger(__name__)


# ─── Heuristic phrase lists ──────────────────────────────────────────────────

# Risk-language phrases. Case-insensitive substring match. The judge adjudicates
# false positives; we want decent recall.
RISK_PHRASES: tuple[str, ...] = (
    "budget freeze",
    "budget cut",
    "reconsidering",
    "competitor",
    "evaluating other",
    "not sure we'll move forward",
    "not sure we will move forward",
    "pause this",
    "on hold",
    "put a pin in",
    "deprioritize",
    "deprioritise",
    "pushing back",
    "pushed back",
    "concerned about",
    "blocker",
)

# Action-item cue phrases (case-insensitive). Patterns built per-rep at fetch
# time so we can include the rep's first name in "<name> will ..." matches.
_ACTION_ITEM_GENERIC_CUES: tuple[str, ...] = (
    "action item:",
    "action items:",
    "next steps:",
    "next step:",
    "follow up:",
    "follow-up:",
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _first_name(email: str) -> str:
    if not email or "@" not in email:
        return ""
    local = email.split("@", 1)[0]
    head = re.split(r"[._\-]", local, maxsplit=1)[0]
    return head.lower()


def _user_domain(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def _truncate(text: str, limit: int = 140) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _excerpt_around(text: str, span: tuple[int, int], window: int = 100) -> str:
    start = max(0, span[0] - window)
    end = min(len(text), span[1] + window)
    chunk = text[start:end].replace("\n", " ").strip()
    return chunk if len(chunk) <= 200 else chunk[:197] + "..."


def _company_name_hint(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[1].lower().split(".", 1)[0]


def _pick_external_contact(participants: list[str], rep_domain: str) -> str:
    for email in participants:
        if not email or "@" not in email:
            continue
        if email.split("@", 1)[1] == rep_domain:
            continue
        return email
    return ""


def _find_action_item(text: str, rep_first_name: str) -> tuple[str, str] | None:
    """Return (matched_pattern, excerpt) if an action item assigned to the rep
    is detected. Heuristic / regex — the judge resolves ambiguity downstream."""
    if not text:
        return None

    if rep_first_name:
        # "<Name> will <verb>" / "<Name> to <verb>" / "<Name> is going to <verb>"
        pat = re.compile(
            rf"\b{re.escape(rep_first_name)}\b\s+(?:will|to|is\s+going\s+to|'ll)\s+\w+",
            re.IGNORECASE,
        )
        m = pat.search(text)
        if m:
            return (m.group(0), _excerpt_around(text, m.span()))

    # "Action item: <rep first name> ..." style — look for the cue then check
    # the next ~120 chars contain the rep's first name.
    if rep_first_name:
        for cue in _ACTION_ITEM_GENERIC_CUES:
            idx = text.lower().find(cue)
            while idx != -1:
                window = text[idx : idx + 200]
                if re.search(
                    rf"\b{re.escape(rep_first_name)}\b", window, re.IGNORECASE
                ):
                    return (cue, _excerpt_around(text, (idx, idx + len(cue))))
                idx = text.lower().find(cue, idx + 1)
    return None


def _find_risk_phrase(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    lower = text.lower()
    for phrase in RISK_PHRASES:
        idx = lower.find(phrase)
        if idx != -1:
            return (phrase, _excerpt_around(text, (idx, idx + len(phrase))))
    return None


# ─── Signal builders ─────────────────────────────────────────────────────────


def _entity_refs(
    *,
    record: Any,
    contact_email: str,
    company_name: str,
) -> dict[str, Any]:
    refs: dict[str, Any] = {"transcript_id": record.doc_id}
    if record.meeting_id:
        refs["event_id"] = record.meeting_id
    if contact_email:
        refs["contact_email"] = contact_email
    if company_name:
        refs["company_name"] = company_name
    return refs


def _meeting_title(record: Any) -> str:
    name = record.original_filename or "untitled meeting"
    # Strip trailing extension and common Gemini/Fathom suffixes for prettier summaries.
    for suf in (" - Notes by Gemini", " Notes by Gemini", ".txt", ".docx"):
        if name.endswith(suf):
            name = name[: -len(suf)]
    return name


def _action_item_signal(
    *,
    user_email: str,
    record: Any,
    excerpt: str,
    rep_domain: str,
) -> Signal:
    title = _meeting_title(record)
    contact = _pick_external_contact(record.participants, rep_domain)
    company = _company_name_hint(contact)
    summary = _truncate(f"Action item assigned in '{title}' transcript: '{excerpt}'")
    return Signal(
        user_email=user_email,
        source="transcript",
        signal_type="action_item_assigned_to_rep",
        summary=summary,
        occurred_at=record.created_at,
        entity_refs=_entity_refs(
            record=record,
            contact_email=contact,
            company_name=company,
        ),
        severity_hint="high",
        payload={
            "transcript_id": record.doc_id,
            "meeting_title": title,
            "meeting_at": record.created_at.isoformat(),
            "participants": list(record.participants),
            "excerpt": excerpt,
        },
    )


def _risk_signal(
    *,
    user_email: str,
    record: Any,
    matched_phrase: str,
    excerpt: str,
    rep_domain: str,
) -> Signal:
    title = _meeting_title(record)
    contact = _pick_external_contact(record.participants, rep_domain)
    company = _company_name_hint(contact)
    summary = _truncate(f"Risk language in '{title}' transcript: '{matched_phrase}'")
    return Signal(
        user_email=user_email,
        source="transcript",
        signal_type="risk_language",
        summary=summary,
        occurred_at=record.created_at,
        entity_refs=_entity_refs(
            record=record,
            contact_email=contact,
            company_name=company,
        ),
        severity_hint="high",
        payload={
            "transcript_id": record.doc_id,
            "meeting_title": title,
            "meeting_at": record.created_at.isoformat(),
            "participants": list(record.participants),
            "matched_phrase": matched_phrase,
            "excerpt": excerpt,
        },
    )


# ─── SignalSource ────────────────────────────────────────────────────────────


class TranscriptSignalSource:
    """Implements `SignalSource`. Pulls recent transcripts the rep
    participated in (or owns) via `TranscriptService`, scans up to 50KB of
    text per transcript, and emits action-item + risk-language Signals.
    """

    name = "transcript"

    def __init__(
        self,
        transcript_service: Any | None = None,
    ):
        self._svc = transcript_service

    def _service_for(self, user_email: str) -> Any | None:
        if self._svc is not None:
            return self._svc
        try:
            from backend.services.transcript_service import TranscriptService

            return TranscriptService(user_email=user_email)
        except Exception as e:
            logger.warning(
                "[signals.transcript] TranscriptService init failed for %s: %s",
                user_email,
                e,
            )
            return None

    def fetch(self, user_email: str, *, since: datetime) -> list[Signal]:
        svc = self._service_for(user_email)
        if svc is None:
            return []

        rep = (user_email or "").lower()
        rep_domain = _user_domain(rep)
        rep_first = _first_name(rep)

        try:
            records = svc.list_transcripts_for_signals(since=since)
        except Exception as e:
            logger.warning(
                "[signals.transcript] list_transcripts_for_signals failed: %s", e
            )
            return []

        signals: list[Signal] = []
        for record in records:
            try:
                text = svc.read_transcript_text(record)
            except Exception as e:
                logger.warning(
                    "[signals.transcript] read_transcript_text failed for %s: %s",
                    record.doc_id,
                    e,
                )
                continue
            if not text:
                continue

            ai = _find_action_item(text, rep_first)
            if ai:
                _, excerpt = ai
                signals.append(
                    _action_item_signal(
                        user_email=rep,
                        record=record,
                        excerpt=excerpt,
                        rep_domain=rep_domain,
                    )
                )

            risk = _find_risk_phrase(text)
            if risk:
                phrase, excerpt = risk
                signals.append(
                    _risk_signal(
                        user_email=rep,
                        record=record,
                        matched_phrase=phrase,
                        excerpt=excerpt,
                        rep_domain=rep_domain,
                    )
                )

        logger.info(
            "[signals.transcript] user=%s transcripts=%d emitted=%d",
            rep,
            len(records),
            len(signals),
        )
        return signals
