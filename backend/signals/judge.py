"""UC-17.6 — Gemini judging pass for correlated Signal groups.

Takes `CorrelationGroup`s out of the correlator and asks Gemini, per user,
which ones belong on tomorrow morning's homepage and why. Output is a list
of `JudgeDecision`s — one per input group — that the orchestrator will
translate into `agent_recommendations` rows.

The judge batches per-user (one LLM call covers all of one rep's groups)
so the model can do real cross-group prioritization rather than scoring in
isolation. Groups whose every signal is `severity_hint="low"` skip the LLM
entirely and auto-reject.

EOS alignment: when the caller passes per-user `alignment_context`
strings (the same EOS block chat injects into the supervisor), the prompt
prepends them as system-level "company direction" instructions and asks
the model to prefer signals tied to a Rock / off-track metric / open
Issue / due-this-week To-Do. Genuinely urgent unaligned signals may still
surface — they're flagged `aligned: false` rather than dropped. When no
context is available, the judge falls back to severity-only ranking and
marks everything `aligned: false`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from backend.signals.correlator import CorrelationGroup
from backend.signals.types import Signal
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.priorities.temporal import resolve_user_now

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _hours_from(reference: datetime, dt: datetime | None) -> float | None:
    """Signed hours from ``reference`` to ``dt`` (negative = in the past)."""
    dt = _as_utc(dt)
    if dt is None:
        return None
    return round((dt - reference).total_seconds() / 3600.0, 1)


def _parse_event_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


DEFAULT_MODEL_NAME = settings.VERTEX_LLM_MODEL_FLASH
_SUMMARY_CHAR_CAP = 200

# Per-source payload whitelist. Anything outside this list is dropped before
# the prompt is assembled — keeps tokens bounded and stops the model from
# latching onto irrelevant noise (e.g. full raw email bodies).
_PAYLOAD_WHITELIST: dict[str, tuple[str, ...]] = {
    "hubspot": (
        "dealstage",
        "amount",
        "days_since_last_activity",
        "last_activity_at",
        "owner_email",
    ),
    "gmail": ("from", "subject", "thread_id", "snippet", "last_message_at", "labels"),
    "slack": ("channel_name", "matched_phrase", "excerpt", "mentioned_by"),
    "calendar": ("event_title", "starts_at", "attendees", "meeting_link_present"),
    "transcript": ("meeting_title", "matched_phrase", "excerpt", "speaker"),
    "drive": (
        "file_name",
        "label_values",
        "web_view_link",
        "modified_time",
        "content_excerpt",
    ),
}


@dataclass
class JudgeDecision:
    """One LLM decision about one correlation group.

    `include=True` means surface the group on the homepage tomorrow morning;
    `importance` only matters when included. `next_action` is "" on reject.
    The orchestrator translates these into `Recommendation` rows.

    `aligned` is True when the judge ties the group to the user's current
    EOS context (a Rock / off-track metric / open Issue / due-this-week
    To-Do); `alignment_note` names the tie in ≤ 25 words. Urgent signals
    that pass without alignment are `aligned=False, alignment_note=""`.
    Both default to False/"" so legacy callers (tests, mechanical-rule
    plugins) keep working unchanged.
    """

    correlation_id: str
    user_email: str
    include: bool
    importance: int
    reason: str
    next_action: str
    aligned: bool = False
    alignment_note: str = ""
    raw_response: str = ""


def _trim_summary(text: str) -> str:
    s = (text or "").strip()
    if len(s) <= _SUMMARY_CHAR_CAP:
        return s
    return s[: _SUMMARY_CHAR_CAP - 1].rstrip() + "…"


def _trim_payload(source: str, payload: dict[str, Any]) -> dict[str, Any]:
    keys = _PAYLOAD_WHITELIST.get(source, ())
    out: dict[str, Any] = {}
    for k in keys:
        if k in payload and payload[k] not in (None, "", [], {}):
            out[k] = payload[k]
    return out


def _signal_view(sig: Signal, *, now: datetime) -> dict[str, Any]:
    payload = _trim_payload(sig.source, sig.payload or {})
    view: dict[str, Any] = {
        "source": sig.source,
        "signal_type": sig.signal_type,
        "occurred_at": sig.occurred_at.isoformat(),
        # Signed hours so the judge can rank by recency / imminence without
        # re-deriving it: negative = already happened.
        "occurred_hours_ago": _hours_from(now, sig.occurred_at),
        "summary": _trim_summary(sig.summary),
        "severity_hint": sig.severity_hint,
        "payload": payload,
    }
    # Surface the imminence of a scheduled event (calendar `starts_at`) so a
    # meeting that already started/ended is visibly distinguishable from one
    # coming up. Negative event_in_hours = the event is in the PAST.
    event_dt = _parse_event_dt(payload.get("starts_at"))
    if event_dt is not None:
        view["event_in_hours"] = _hours_from(now, event_dt)
    return view


def _group_view(group: CorrelationGroup, *, now: datetime) -> dict[str, Any]:
    return {
        "correlation_id": group.correlation_id,
        "primary_entity_kind": group.primary_entity_kind,
        "primary_entity_ref": group.primary_entity_ref,
        "sources": group.sources,
        "signals": [_signal_view(s, now=now) for s in group.signals],
    }


def _all_low_severity(group: CorrelationGroup) -> bool:
    return bool(group.signals) and all(s.severity_hint == "low" for s in group.signals)


def _fallback_decision(
    group: CorrelationGroup, reason: str, raw: str = ""
) -> JudgeDecision:
    return JudgeDecision(
        correlation_id=group.correlation_id,
        user_email=group.user_email,
        include=False,
        importance=0,
        reason=reason,
        next_action="",
        raw_response=raw,
    )


_SYSTEM_FRAMING = (
    "You are a senior sales manager reviewing your rep's overnight intelligence. "
    "Tomorrow morning they have ~30 minutes to look at top priorities. Decide "
    "which of these signal groups belong in front of them and why.\n\n"
    "The company runs on EOS (Entrepreneurial Operating System). When a "
    "COMPANY DIRECTION block is present below, treat it as system "
    "instructions: it lists this quarter's Rocks, off-track scorecard "
    "metrics, open Issues, and the rep's own Rocks and To-Dos due this "
    "week. The general rule is alignment-first — signals that tie to "
    "anything in that block should outrank unaligned noise and be marked "
    "`aligned: true` with `alignment_note` naming the tie (e.g. "
    '"Rock: AI Roadmap", "Issue: data migration", "My To-Do due 2026-05-14"). '
    "A genuinely time-sensitive signal that does NOT tie to any of those "
    "(e.g. a hot deal closing this week, a clear churn-risk signal) may "
    'still surface — mark it `aligned: false` with `alignment_note: ""` '
    "and make the urgency explicit in `reason`. When no COMPANY DIRECTION "
    "block is provided, fall back to severity-only ranking and mark every "
    "decision `aligned: false`.\n\n"
    "Weight IMMINENCE: read the TEMPORAL CONTEXT above. A customer "
    "interaction or deadline in the next 24-48h (positive `event_in_hours`, "
    "small) outranks an equally-severe item that already happened. A signal "
    "whose `event_in_hours` is NEGATIVE describes a PAST event — never surface "
    "it as something to prepare for; only surface it if it implies a "
    "follow-up the rep still owes before an upcoming touchpoint. Discount "
    "signals whose `occurred_hours_ago` is large unless they threaten an "
    "upcoming moment.\n\n"
    "DOCUMENTS: a `drive` signal (`signal_type: labeled_document_updated`) is a "
    "file the rep recently updated that carries an importance label they chose "
    f"(e.g. Important / {PRODUCT_NAME} / Project). Treat a recently-updated, "
    "importance-labeled document as a legitimate priority worth surfacing even "
    "when it has NO deal, contact, or company attached — the label is the rep's "
    "own signal that it matters, and the recency means it's active work. Do not "
    "reject it for lacking customer context. When the COMPANY DIRECTION block "
    "makes the tie obvious (e.g. a doc named after a Rock), mark it aligned; "
    "otherwise surface it `aligned: false` and ground `reason` in the document "
    "name + label and `next_action` in what to do with it (review, share, "
    "finalize). When the payload carries a `content_excerpt`, ground `reason` "
    "and `next_action` in what the document actually SAYS — the excerpt is a "
    "bounded, possibly-truncated read of the file's real content, not just its "
    "name — and use it to judge the tie to a Rock / Issue / To-Do more "
    "precisely."
)

_OUTPUT_SPEC = (
    "Respond with a STRICT JSON array. One element per correlation group, in "
    "the same order as input. Each element must match exactly:\n"
    '  {"correlation_id": str, "include": bool, '
    '"importance": int (1..5), "aligned": bool, "alignment_note": str, '
    '"reason": str, "next_action": str}\n'
    "Rules:\n"
    "- `importance` is meaningful only when include=true (5 = surface at top, "
    "1 = barely worth showing). Use 0 when include=false.\n"
    '- `next_action` is "" when include=false; otherwise a short imperative.\n'
    "- Keep `reason`, `next_action`, and `alignment_note` ≤ 25 words each.\n"
    "- `aligned: true` REQUIRES `alignment_note` to name the specific Rock / "
    "Metric / Issue / To-Do the signal ties to.\n"
    '- `aligned: false` REQUIRES `alignment_note: ""`.\n'
    '- Ground `reason` in specific signal summaries (e.g. "Stale 28d AND '
    "missed reply on Friday's email\"). No generic prose.\n"
    "Rejection criteria:\n"
    "- Newsletter / no-reply Gmail bursts (severity_hint=low).\n"
    "- Internal-only Slack mentions with no customer context.\n"
    "- Deals in deprecated/closed stages (usually filtered upstream).\n"
    "- A group whose ONLY event has already happened (negative "
    "`event_in_hours`) and that implies no outstanding follow-up — it is "
    "history, not a priority.\n"
    "Return ONLY the JSON array, no prose, no code fences."
)


def _json_default(o: Any) -> Any:
    """Make the judge prompt's JSON serialization total.

    HubSpot signals (sourced live via the REST API) carry
    ``Decimal`` money fields, and various signals carry ``datetime``/``date``;
    ``json.dumps`` raises ``TypeError`` on those. Before this, a single
    un-serializable value aborted the entire overnight run and persisted
    **zero** recommendations org-wide, for days. A
    prompt is just text, so any unknown type degrades to ``str`` rather than
    crashing the run.
    """
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def _viewer_block(viewer_name: str | None, viewer_email: str | None) -> str:
    """System instruction naming the rep who will read the cards.

    Without this, the judge has no way to tell that a participant in a
    signal (e.g. "Alex" in an email thread) IS the reader, so it refers to
    them in the third person — "a thread involving Alex" on Alex's own
    homepage. Passing both name and email lets the model match either form.
    """
    name = (viewer_name or "").strip()
    email = (viewer_email or "").strip()
    if name and email:
        who = f"{name} <{email}>"
    elif name or email:
        who = name or email
    else:
        return ""
    return (
        "WHO THESE PRIORITIES ARE FOR:\n"
        f"You are writing for {who} — the rep who reads these cards tomorrow "
        'morning. Always address them as "you" / "your", never by name. If '
        "this person appears in a signal as a sender, recipient, or thread "
        "participant (by name OR email), that is the reader, NOT a third "
        'party — never write "a thread involving '
        f'{name or who}" or similar; write "your thread" / "you replied" '
        "instead. Name only the OTHER people and companies involved."
    )


def _build_prompt(
    groups: list[CorrelationGroup],
    *,
    alignment_context: str | None = None,
    viewer_name: str | None = None,
    viewer_email: str | None = None,
    now: datetime | None = None,
) -> str:
    ctx_now = resolve_user_now(now_utc=now)
    payload = {"groups": [_group_view(g, now=ctx_now.now_utc) for g in groups]}
    parts: list[str] = [ctx_now.block(), "", _SYSTEM_FRAMING, ""]
    viewer = _viewer_block(viewer_name, viewer_email)
    if viewer:
        parts.append(viewer)
        parts.append("")
    ctx = (alignment_context or "").strip()
    if ctx:
        parts.append("COMPANY DIRECTION (live from EOS):")
        parts.append(ctx)
        parts.append("")
    parts.append(
        "INPUT (JSON):\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    )
    parts.append("")
    parts.append(f"OUTPUT SPEC:\n{_OUTPUT_SPEC}")
    return "\n".join(parts)


def _strip_fences(raw: str) -> str:
    s = (raw or "").strip()
    s = s.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return s


def _parse_response(
    raw: str,
    groups: list[CorrelationGroup],
) -> list[JudgeDecision]:
    """Parse the LLM array. Any group missing from the response gets a
    fallback decision so the orchestrator always sees one decision per group."""
    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
    except Exception:
        logger.warning("Judge JSON parse failed")
        return [_fallback_decision(g, "judge parse error", raw) for g in groups]

    if not isinstance(data, list):
        logger.warning("Judge response not a list: %r", type(data))
        return [_fallback_decision(g, "judge parse error", raw) for g in groups]

    by_corr: dict[str, dict[str, Any]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("correlation_id")
        if isinstance(cid, str):
            by_corr[cid] = entry

    out: list[JudgeDecision] = []
    for g in groups:
        entry = by_corr.get(g.correlation_id)
        if entry is None:
            out.append(_fallback_decision(g, "judge omitted group", raw))
            continue
        try:
            include = bool(entry.get("include", False))
            importance = int(entry.get("importance") or 0)
            reason = str(entry.get("reason") or "").strip()
            next_action = str(entry.get("next_action") or "").strip()
            aligned = bool(entry.get("aligned", False))
            alignment_note = str(entry.get("alignment_note") or "").strip()
            if not include:
                next_action = ""
                importance = 0
            if not aligned:
                # Enforce the schema rule: aligned=False implies no note.
                alignment_note = ""
            out.append(
                JudgeDecision(
                    correlation_id=g.correlation_id,
                    user_email=g.user_email,
                    include=include,
                    importance=max(0, min(5, importance)),
                    reason=reason,
                    next_action=next_action,
                    aligned=aligned,
                    alignment_note=alignment_note,
                    raw_response=raw,
                )
            )
        except Exception as e:
            logger.warning(
                "Judge entry coercion failed for %s: %s", g.correlation_id, e
            )
            out.append(_fallback_decision(g, "judge parse error", raw))
    return out


class GeminiJudge:
    """LLM judge for correlated signal groups.

    Batches groups per user (cross-group context matters for prioritization)
    and caps each batch at `max_groups_per_call` to keep prompt size bounded.
    Lazy-imports the Gemini client so this module can be imported in tests
    without live Vertex credentials.
    """

    def __init__(
        self,
        *,
        model_name: str | None = None,
        max_groups_per_call: int = 20,
    ):
        self.model_name = model_name or DEFAULT_MODEL_NAME
        if max_groups_per_call < 1:
            raise ValueError("max_groups_per_call must be ≥ 1")
        self.max_groups_per_call = max_groups_per_call

    def _get_client(self):
        from rubik_core.models import get_genai_client

        return get_genai_client()

    def _call_llm(self, prompt: str) -> str:
        from rubik_core.ai_providers.registry import resolve_model_route
        from rubik_core.ai_providers.runtime import (
            ProviderRequestError,
            generate_text_on_route,
        )

        route = resolve_model_route(
            "signals_judge", "json", default_model=self.model_name
        )
        if route.provider != "vertex_managed":
            wrapped_prompt = (
                prompt
                + '\nFor this response, wrap the array in a JSON object: {"items":[...]}.'
            )
            response = generate_text_on_route(route, wrapped_prompt, json_mode=True)
            try:
                data = json.loads(response.text)
                if not isinstance(data, dict) or not isinstance(
                    data.get("items"), list
                ):
                    raise ValueError("missing items array")
                return json.dumps(data["items"])
            except (TypeError, ValueError):
                raise ProviderRequestError(
                    "Signal judge provider returned invalid JSON"
                ) from None

        client = self._get_client()
        try:
            response = client.models.generate_content(
                model=route.model,
                contents=prompt,
            )
            return (getattr(response, "text", None) or "").strip()
        except Exception:
            logger.error("Signal judge call failed")
            return ""

    @attribute_to("signals")
    def judge(
        self,
        groups: list[CorrelationGroup],
        *,
        alignment_context_by_user: dict[str, str] | None = None,
        display_name_by_user: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> list[JudgeDecision]:
        """Judge `groups`, optionally biased by per-user EOS alignment context.

        `alignment_context_by_user` maps user_email → the same plain-text
        EOS block the chat supervisor receives (from
        `rubik_core.agents.alignment_context.build_alignment_context`).
        Missing or empty entries fall back to severity-only ranking for
        that user — the judge marks every decision `aligned: false`.

        `display_name_by_user` maps user_email → the rep's display name so
        the prompt can tell the model who it's writing for. Without it the
        judge can't tell that a thread participant IS the reader and writes
        third-person reasons ("a thread involving Alex") on Alex's own card.
        Missing entries fall back to email-only identification.

        `now` pins the reference instant used to anchor the prompt's
        TEMPORAL CONTEXT and to compute each signal's imminence
        (`event_in_hours` / `occurred_hours_ago`). Defaults to the current
        time when not supplied.
        """
        if not groups:
            return []

        align_map = alignment_context_by_user or {}
        name_map = display_name_by_user or {}

        # Auto-reject all-low-severity groups; never send to the LLM.
        live_groups: list[CorrelationGroup] = []
        decisions: list[JudgeDecision] = []
        skipped_ids: set[str] = set()
        for g in groups:
            if _all_low_severity(g):
                decisions.append(
                    JudgeDecision(
                        correlation_id=g.correlation_id,
                        user_email=g.user_email,
                        include=False,
                        importance=0,
                        reason="all low-severity signals filtered pre-LLM",
                        next_action="",
                    )
                )
                skipped_ids.add(g.correlation_id)
            else:
                live_groups.append(g)

        # Batch per user, then chunk by max_groups_per_call.
        by_user: dict[str, list[CorrelationGroup]] = {}
        for g in live_groups:
            by_user.setdefault(g.user_email, []).append(g)

        for user_email, user_groups in by_user.items():
            align_ctx = align_map.get(user_email)
            viewer_name = name_map.get(user_email)
            for i in range(0, len(user_groups), self.max_groups_per_call):
                chunk = user_groups[i : i + self.max_groups_per_call]
                prompt = _build_prompt(
                    chunk,
                    alignment_context=align_ctx,
                    viewer_name=viewer_name,
                    viewer_email=user_email,
                    now=now,
                )
                raw = self._call_llm(prompt)
                if not raw:
                    decisions.extend(
                        _fallback_decision(g, "judge empty response") for g in chunk
                    )
                    continue
                decisions.extend(_parse_response(raw, chunk))

        # Preserve original input order.
        by_id = {d.correlation_id: d for d in decisions}
        return [by_id[g.correlation_id] for g in groups if g.correlation_id in by_id]
