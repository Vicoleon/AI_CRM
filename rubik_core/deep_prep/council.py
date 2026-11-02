"""The Deep Prep council: agree what to deliver → write grounded → verify.

Three background Gemini passes (latency is free off the request path):

  1. PLAN   — a panel agrees which sections THIS meeting's prep warrants, from a
              role-aware menu. Depth comes from choosing the right sections, not
              from always emitting the same fixed fields.
  2. WRITE  — each agreed section is written, grounded strictly in the supplied
              context, with per-section source citations.
  3. VERIFY — a skeptic re-reads every factual claim against the sources and
              removes/hedges anything unsupported — especially deal/bid/
              engagement outcomes — before release.

An unsupported claim is removed, not softened into something plausible; that is
the whole point of the council existing (it is the structural answer to the
"previous unsuccessful bid" fabrication). Each stage is one call for a bounded
first version and is extensible to multiple advisors later. Every stage degrades
gracefully so a single model hiccup still yields a usable brief.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging

from rubik_core.cost.usage import attribute_to
from rubik_core.deep_prep.models import DeepPrepResult, DeepPrepSection

logger = logging.getLogger(__name__)

# The menu the council chooses from. Keys are stable; headings are suggestions
# the council may refine. Role decides emphasis, not a separate menu.
SECTION_MENU: list[tuple[str, str]] = [
    (
        "relationship_history",
        "What we know about this client/relationship and its history",
    ),
    ("engagement_scope", "The engagement or opportunity and its current scope"),
    ("objectives", "What we want out of THIS meeting"),
    ("stakeholders", "Who is in the room and what each cares about"),
    (
        "risks_open_issues",
        "Risks and open issues (blockers, dependencies, unresolved concerns)",
    ),
    ("open_commitments", "Outstanding to-dos, promises, and follow-ups on record"),
    ("landmines", "Sensitive topics or risks to handle carefully"),
    ("recommended_plan", "Recommended approach, positioning, or talk track"),
    ("open_questions", "Key questions to get answered"),
]

_GROUNDING = (
    "GROUNDING RULES (non-negotiable — a false fact is worse than a thin brief):\n"
    "- Use ONLY the CONTEXT provided. Never assert the outcome or status of a "
    "past bid, deal, proposal, or engagement (won/lost/renewed/active/pending) "
    "unless the CONTEXT explicitly states it.\n"
    "- Do NOT infer history or outcomes from tone or phrasing. If the CONTEXT "
    "doesn't state it, do not claim it.\n"
    "- Transcript excerpts are dated; treat them as historical, not the current "
    "state.\n"
    "- Prefer 'not enough in the records to say' over inventing. Cite the source "
    "for every factual claim."
)


def _persona(is_sales: bool, is_internal: bool, department: str) -> str:
    if is_sales and not is_internal:
        return (
            "This is a SALES meeting with a prospect/client. Emphasize the "
            "opportunity, pain points, and how our offering fits — but keep every factual "
            "claim about the relationship grounded."
        )
    dept = (department or "the team").strip() or "the team"
    return (
        f"This is a {dept} meeting (not sales). Frame it for that team's work — "
        "operational status, engagement scope, commitments, and risks. It is "
        "prep for the work itself, not a sales pitch."
    )


def _context_block(ctx: dict) -> str:
    """Assemble the CONTEXT the council reasons over, from primitives on ctx."""
    parts: list[str] = []
    if ctx.get("title"):
        parts.append(f"MEETING: {ctx['title']}")
    if ctx.get("description"):
        parts.append(f"DESCRIPTION: {ctx['description']}")
    attendees = ctx.get("attendees") or []
    if attendees:
        who = ", ".join(
            f"{a.get('display_name') or a.get('email', '?')} <{a.get('email', '')}>"
            for a in attendees
        )
        parts.append(f"ATTENDEES: {who}")
    # UC-47 — the researched public record on the external attendees. Same block
    # the on-screen brief gets, so the two never form different impressions of
    # the same person from the same evidence.
    if ctx.get("attendee_intel"):
        from rubik_core.people.prompt import format_intel_block_from_dicts

        intel_block = format_intel_block_from_dicts(ctx["attendee_intel"])
        if intel_block:
            parts.append(intel_block)
    # Meeting Prep v2 signal blocks (rubik_core/meeting_prep/signals.py) — the
    # same blocks the on-screen brief got, with how to read each one. The
    # ambiguity-safe CRM ACCOUNT RECORD supersedes the older first-match
    # CLIENT RELATIONSHIP block, so only one CRM picture reaches the council.
    signal_blocks = ctx.get("signal_blocks") or {}
    if ctx.get("crm_relationships") and not signal_blocks.get("crm"):
        parts.append(ctx["crm_relationships"])
    if signal_blocks:
        from rubik_core.meeting_prep.signals import context_notes

        notes = context_notes(signal_blocks)
        if notes:
            parts.append(notes)
    # Phase 0 relationship graph — attendance-record facts ("when did we
    # actually last meet"). The block carries its own heading and ground-truth
    # label, so it drops in verbatim, before the semantic transcript excerpts.
    if ctx.get("prior_meetings_block"):
        parts.append(ctx["prior_meetings_block"])
    if ctx.get("past_meetings_report"):
        parts.append(
            f"PAST MEETING TRANSCRIPTS (dated):\n{ctx['past_meetings_report']}"
        )
    emails = ctx.get("recent_emails") or []
    if emails:
        lines = []
        for t in emails[:5]:
            msgs = t.get("messages") or []
            if not msgs:
                continue
            headers = {
                h.get("name"): h.get("value")
                for h in msgs[-1].get("payload", {}).get("headers", [])
            }
            lines.append(
                f"[{(headers.get('Date') or '')[:16]}] {headers.get('From', '?')} — "
                f"{headers.get('Subject', '(no subject)')}: {msgs[-1].get('snippet', '')[:300]}"
            )
        if lines:
            parts.append("RECENT EMAILS:\n" + "\n".join(lines))
    if ctx.get("drive_context"):
        parts.append(f"DRIVE DOCUMENTS:\n{ctx['drive_context']}")
    products = ctx.get("matched_products") or []
    if products:
        names = ", ".join(p.get("name", "") for p in products if p.get("name"))
        if names:
            parts.append(f"RELEVANT PRODUCTS AND SERVICES: {names}")
    return (
        "\n\n".join(parts) if parts else "(No context was retrieved for this meeting.)"
    )


async def _call_json(client, model_id: str, system: str, prompt: str):
    """One JSON-returning Gemini call. Raises on transport/parse failure.

    ``model_id`` is the admin's choice and may pin a thinking level
    (``"gemini-4-argon@high"``); the level is split off here, the one place the
    council talks to Gemini. With no level the model keeps its own default,
    as it always has.
    """
    from google.genai import types

    from rubik_core.gemini_tuning import split_model_choice

    model, level = split_model_choice(model_id)
    res = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            thinking_config=(
                types.ThinkingConfig(thinking_level=level) if level else None
            ),
            # JSON mode. Every prompt here already asks for raw JSON, but asking
            # is not enforcing: replayed on real inputs,
            # gemini-4-argon left the quotes around a quoted name ("Morgan")
            # unescaped in 3 of 14 drafts, json.loads failed, and the job fell
            # back to the one-call brief. The fence-stripping below stays as a
            # harmless guard.
            response_mime_type="application/json",
        ),
    )
    raw = (
        (res.text or "")
        .strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )
    return _json.loads(raw)


def _default_plan(is_sales: bool, is_internal: bool) -> list[dict]:
    keys = (
        [
            "relationship_history",
            "engagement_scope",
            "objectives",
            "stakeholders",
            "recommended_plan",
            "open_commitments",
            "open_questions",
        ]
        if (is_sales and not is_internal)
        else [
            "engagement_scope",
            "objectives",
            "open_commitments",
            "risks_open_issues",
            "stakeholders",
            "open_questions",
        ]
    )
    menu = dict(SECTION_MENU)
    return [{"key": k, "heading": menu[k].split(" (")[0], "why": ""} for k in keys]


async def _plan(client, model_id, context_block, persona) -> dict:
    menu = "\n".join(f"- {k}: {desc}" for k, desc in SECTION_MENU)
    system = (
        "You are a small panel preparing a colleague for a meeting. Your job in "
        "this step is only to AGREE what the prep should contain — which sections "
        "genuinely help for THIS meeting, and in what order. Choose from the menu; "
        "skip sections the context can't support. " + _GROUNDING
    )
    prompt = (
        f"{persona}\n\nSECTION MENU (key: description):\n{menu}\n\n"
        f"CONTEXT:\n{context_block}\n\n"
        'Return raw JSON only: {"sections":[{"key":"...","heading":"...",'
        '"why":"one line"}],"rationale":"one line"}. Order sections by usefulness.'
    )
    return await _call_json(client, model_id, system, prompt)


async def _write(client, model_id, context_block, persona, plan_sections) -> dict:
    wanted = "\n".join(
        f"- {s.get('key')}: {s.get('heading')} ({s.get('why', '')})"
        for s in plan_sections
    )
    system = (
        "You are writing a deep, decision-ready meeting prep. Write each requested "
        "section using ONLY the context. Cite the source of each factual claim in "
        "that section's sources list. " + _GROUNDING
    )
    prompt = (
        f"{persona}\n\nWRITE THESE SECTIONS (in order):\n{wanted}\n\n"
        f"CONTEXT:\n{context_block}\n\n"
        'Return raw JSON only: {"headline":"one grounded sentence on what this '
        'meeting is and what is at stake, per the records",'
        '"sections":[{"heading":"...","body":"markdown, grounded","sources":["..."]}],'
        '"data_gaps":["what we do NOT know from the records"],'
        '"open_questions":["questions to ask"]}.'
    )
    return await _call_json(client, model_id, system, prompt)


async def _verify(client, model_id, context_block, draft) -> dict:
    system = (
        "You are a skeptical fact-checker. For every factual claim in the draft, "
        "check it is directly supported by the CONTEXT. REMOVE or explicitly hedge "
        "any claim that is not — especially any statement about whether a past "
        "bid/deal/engagement was won, lost, renewed, active, or pending. Do not add "
        "new facts. Return the corrected brief. " + _GROUNDING
    )
    prompt = (
        f"CONTEXT:\n{context_block}\n\nDRAFT (JSON):\n{_json.dumps(draft)}\n\n"
        "Return raw JSON only, same shape as the draft plus a verdict: "
        '{"headline":"...","sections":[{"heading":"...","body":"...","sources":[...]}],'
        '"data_gaps":[...],"open_questions":[...],'
        '"removed_claims":["claims you stripped as unsupported"],'
        '"verdict":"pass" or "revised"}.'
    )
    return await _call_json(client, model_id, system, prompt)


def _sections_from(data: dict) -> list[DeepPrepSection]:
    out: list[DeepPrepSection] = []
    for s in data.get("sections") or []:
        if not isinstance(s, dict):
            continue
        heading = (s.get("heading") or "").strip()
        body = (s.get("body") or "").strip()
        if not (heading or body):
            continue
        srcs = [str(x).strip() for x in (s.get("sources") or []) if str(x).strip()]
        out.append(DeepPrepSection(heading=heading or "Notes", body=body, sources=srcs))
    return out


@attribute_to("deep_prep")
async def generate_deep_prep(
    ctx: dict,
    *,
    user_email: str,
    is_sales_user: bool,
    is_internal: bool,
    department: str = "",
    model_id: str | None = None,
    client=None,
) -> DeepPrepResult:
    """Run the plan → write → verify council and return a DeepPrepResult.

    The plan and verify stages degrade gracefully (a default section plan; the
    pre-verify draft). A hard failure of the write stage propagates, so the
    caller (``_run_deep_prep``) can fall back to the simpler grounded brief
    rather than email an empty council result.

    ``model_id`` defaults to the admin's Deep Prep choice (Admin → Sales Models,
    role ``deep_prep``), which itself defaults to the PRO tier; ``client`` to the
    shared cost-instrumented client. A caller may pass its own of each to run
    this exact council on another model through an uninstrumented client,
    without patching global settings from worker threads.
    """
    from rubik_core.ai_providers.registry import resolve_model_route
    from rubik_core.ai_providers.runtime import generate_text_on_route

    if not model_id:
        from rubik_core.sales.model_config import model_for

        model_id = model_for("deep_prep")
    route = await asyncio.to_thread(
        resolve_model_route,
        "deep_prep_council",
        "json",
        default_model=model_id,
    )
    if route.provider != "vertex_managed":
        # All three council stages share one selected model. Revalidation in
        # generate_text_on_route catches a key revocation or route change
        # between stages without falling back to the supplied Vertex client.
        from types import SimpleNamespace

        class _PinnedModels:
            async def generate_content(self, *, model, contents, config):
                if model != route.model or not isinstance(contents, str):
                    raise RuntimeError("Deep Prep model route changed")
                answer = await asyncio.to_thread(
                    generate_text_on_route,
                    route,
                    contents,
                    json_mode=True,
                    system_instruction=config.system_instruction,
                    temperature=config.temperature,
                )
                return SimpleNamespace(text=answer.text)

        client = SimpleNamespace(aio=SimpleNamespace(models=_PinnedModels()))
        model_id = route.model
    else:
        model_id = route.model
        if client is None:
            from rubik_core.models import get_genai_client

            client = get_genai_client()
    context_block = _context_block(ctx)
    persona = _persona(is_sales_user, is_internal, department)
    title = ctx.get("title") or "Meeting Prep"

    # 1) PLAN
    try:
        plan = await _plan(client, model_id, context_block, persona)
        plan_sections = plan.get("sections") or _default_plan(
            is_sales_user, is_internal
        )
    except Exception:
        if route.provider != "vertex_managed":
            from rubik_core.ai_providers.runtime import ProviderRequestError

            raise ProviderRequestError(
                "Deep Prep provider failed. Check this workspace's model route."
            ) from None
        logger.exception("deep_prep council: plan stage failed; using default plan")
        plan = {"rationale": "default (plan stage failed)"}
        plan_sections = _default_plan(is_sales_user, is_internal)

    # 2) WRITE
    try:
        draft = await _write(client, model_id, context_block, persona, plan_sections)
    except Exception:
        if route.provider != "vertex_managed":
            from rubik_core.ai_providers.runtime import ProviderRequestError

            raise ProviderRequestError(
                "Deep Prep provider failed. Check this workspace's model route."
            ) from None
        raise

    # 3) VERIFY (best-effort — keep the draft if the checker fails)
    verdict: dict = {}
    final = draft
    try:
        verified = await _verify(client, model_id, context_block, draft)
        if verified.get("sections"):
            final = verified
        verdict = {
            "verdict": verified.get("verdict", "pass"),
            "removed_claims": verified.get("removed_claims", []),
        }
    except Exception:
        if route.provider != "vertex_managed":
            from rubik_core.ai_providers.runtime import ProviderRequestError

            raise ProviderRequestError(
                "Deep Prep provider failed. Check this workspace's model route."
            ) from None
        logger.exception("deep_prep council: verify stage failed; releasing draft")
        verdict = {"verdict": "unverified", "removed_claims": []}

    sections = _sections_from(final)
    all_sources = sorted({s for sec in sections for s in sec.sources if s})
    return DeepPrepResult(
        title=title,
        headline=(final.get("headline") or "").strip(),
        sections=sections,
        data_gaps=[
            str(x).strip() for x in (final.get("data_gaps") or []) if str(x).strip()
        ],
        open_questions=[
            str(x).strip()
            for x in (final.get("open_questions") or [])
            if str(x).strip()
        ],
        all_sources=all_sources,
        plan=plan if isinstance(plan, dict) else {},
        verdict=verdict,
        is_sales_user=is_sales_user,
        is_internal=is_internal,
    )
