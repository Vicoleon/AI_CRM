"""The five adversarial auditors.

Where the source LLM-council skill convenes five *opinionated advisors* to fight
a model's agreeableness on strategic questions, we convene five *adversarial
auditors* to fight the way ingestions pass silently. Each owns one failure class
and is told to assume something is broken and prove it from the evidence — never
to reassure. The deterministic cross-checks are handed to them as ground truth
they may cite but must not contradict.
"""

from __future__ import annotations

from dataclasses import dataclass

from rubik_core.brand import PRODUCT_NAME
from rubik_core.ingestion_council.llm import AUDITOR_MODEL, council_llm
from rubik_core.ingestion_council.models import AuditorFinding, Finding, Health


@dataclass(frozen=True)
class Auditor:
    id: str
    name: str
    lens: str
    mandate: str


AUDITORS: list[Auditor] = [
    Auditor(
        id="freshness",
        name="The Freshness Auditor",
        lens="recency & completeness of capture",
        mandate=(
            "Did each run actually fire and capture the latest data? Hunt runs "
            "that produced nothing (did the job even fire?), stale lookback "
            "windows, and gaps between when content changed and when it was "
            "ingested. A genuinely quiet night is acceptable; a silent no-run is "
            "not."
        ),
    ),
    Auditor(
        id="coverage",
        name="The Coverage Critic",
        lens="silent drops & missing population",
        mandate=(
            "What SHOULD have been ingested but wasn't? Users missing from the "
            "briefing, plugins that emitted zero, source tables holding content "
            "that never flowed through. Assume something was dropped and make the "
            "evidence prove it wasn't."
        ),
    ),
    Auditor(
        id="quality",
        name="The Data-Quality Skeptic",
        lens="content integrity",
        mandate=(
            "Is the ingested content actually usable? Blank/empty text, "
            "zero-confidence recommendations, failed or stuck ingestions, "
            "duplicates, truncation. Assume the payload is corrupt until the "
            "numbers say otherwise."
        ),
    ),
    Auditor(
        id="retrieval",
        name="The Retrieval Realist",
        lens="retrievability & correctness once ingested",
        mandate=(
            "Even if it landed, will it retrieve usefully and be correctly "
            "scoped? Chunks/embeddings present, topic/department metadata right, "
            "corpus routing correct. Flag anything that would make a document "
            "unfindable or scoped to the wrong audience. Remember corpus writes "
            "to Vertex are not directly verified by this evidence."
        ),
    ),
    Auditor(
        id="acl_cost",
        name="The ACL & Cost Watchdog",
        lens="access control & volume anomalies",
        mandate=(
            "Who can now see what they should not — or not see what they should? "
            "Missing transcript ACL rows, department stamps that over-grant, VTO "
            "/exec confidentiality. Also flag volume anomalies that imply runaway "
            "cost or a stuck loop (e.g. an order-of-magnitude jump in row counts)."
        ),
    ),
]


def _auditor_prompt(auditor: Auditor, evidence_block: str, scope: str) -> str:
    return f"""You are {auditor.name}, an adversarial QA auditor reviewing {PRODUCT_NAME}'s \
{scope} data-ingestion runs. {PRODUCT_NAME} is an internal RAG assistant; these runs pull \
documents and signals into its knowledge base overnight and weekly.

YOUR SINGLE LENS: {auditor.lens}.
YOUR MANDATE: {auditor.mandate}

You are not here to reassure. Assume something is wrong and try to prove it from \
the evidence below. Cite specific numbers. But do not invent problems the \
evidence does not support, and NEVER contradict a deterministic check: lines \
marked "[OK]" passed and "[FAIL]" failed — those are computed facts, not \
opinions. A "[FAIL]" relevant to your lens is at least a "warning"; an \
access-control, over-grant, or unqueryable-data "[FAIL]" is "critical".

EVIDENCE
{evidence_block}

Return ONLY a JSON object of this exact shape:
{{
  "health": "pass" | "warn" | "fail",
  "summary": "one or two sentences — your bottom line for this lens",
  "confidence": 0.0,
  "findings": [
    {{"severity": "critical" | "warning" | "info",
      "title": "short label",
      "detail": "specific; cite the number and which run",
      "run_kind": "overnight_sales | priorities_daily | transcript_sync | eos_weekly | null"}}
  ]
}}

Health rubric: "fail" = a critical problem within your lens; "warn" = a real but \
non-blocking issue; "pass" = clean for your lens. Use an empty findings list if \
genuinely clean. Output nothing but the JSON."""


def _clamp_conf(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5


def _parse_auditor(auditor: Auditor, raw: object) -> AuditorFinding:
    data = raw if isinstance(raw, dict) else {}
    findings = [
        Finding.from_dict(f)
        for f in (data.get("findings") or [])
        if isinstance(f, dict) and str(f.get("title") or "").strip()
    ]
    summary = str(data.get("summary") or "").strip()
    # Empty summary means the LLM call fell back (a failure) — surface that
    # rather than pretending the lens passed clean.
    if not summary:
        summary = "Auditor produced no assessment (LLM call failed or returned empty)."
    return AuditorFinding(
        auditor_id=auditor.id,
        lens=auditor.lens,
        health=Health.coerce(data.get("health"), Health.WARN),
        summary=summary,
        findings=findings,
        confidence=_clamp_conf(data.get("confidence")),
    )


def run_auditor(auditor: Auditor, evidence_block: str, scope: str) -> AuditorFinding:
    """Run one auditor over the shared evidence. Never raises: a failed LLM call
    degrades (via the ``fallback``) to a WARN "no assessment" finding."""
    raw = council_llm(
        _auditor_prompt(auditor, evidence_block, scope),
        model_key=AUDITOR_MODEL,
        temperature=0.3,
        fallback={},
    )
    return _parse_auditor(auditor, raw)
