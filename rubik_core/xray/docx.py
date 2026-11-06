"""Render a Customer X-Ray brief into an editable, branded Word document.

The .docx twin of ``xray/pdf.py`` — same section order, plain editable text
instead of drawn tiles/badges (the point is tweaking before sharing, not visual
fidelity). Reuses the shared Word helpers from ``deep_prep/docx.py`` exactly
like the PDF reuses ``deep_prep/pdf.py``. Pure: never hits the network.
"""

from __future__ import annotations

from typing import Any

from docx import Document
from docx.shared import Pt, RGBColor

from rubik_core.deep_prep.docx import (
    MUTED,
    _body,
    _bullets,
    _docx_bytes,
    _heading,
    _muted_para,
    _new_doc,
)

_BAND_RGB = {
    "Healthy": RGBColor(0x15, 0x80, 0x3D),
    "Watch": RGBColor(0xB4, 0x53, 0x09),
    "At-risk": RGBColor(0xB9, 0x1C, 0x1C),
}


def _band_color(band: str) -> RGBColor:
    return _BAND_RGB.get(band or "", MUTED)


def _fact_table(doc: Document, rows: list[tuple[str, str]]) -> None:
    """Signal facts as a simple 2-column table — the editable stand-in for the
    PDF's tile grid."""
    if not rows:
        return
    table = doc.add_table(rows=len(rows), cols=2)
    table.style = "Table Grid"
    for i, (label, value) in enumerate(rows):
        lc, vc = table.rows[i].cells
        run = lc.paragraphs[0].add_run(label)
        run.font.size = Pt(9)
        run.font.color.rgb = MUTED
        run = vc.paragraphs[0].add_run(str(value))
        run.font.bold = True


def render_xray_docx(
    *,
    company_name: str,
    signals: dict[str, Any],
    sections: dict[str, Any],
    prepared_for: str,
    generated_at: str = "",
) -> bytes:
    """Return .docx bytes for an X-Ray snapshot's signals + sections."""
    signals = signals or {}
    sections = sections or {}
    churn = signals.get("churn") or {}
    title = company_name or "Customer"

    meta = f"Prepared for {prepared_for}"
    if generated_at:
        meta += f"   |   {generated_at}"
    doc = _new_doc("CUSTOMER X-RAY", title, meta)
    doc.core_properties.title = f"Customer X-Ray - {title}"

    # ── Churn score + rationale ─────────────────────────────────────────────
    if churn:
        band = str(churn.get("band") or "")
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(8)
        run = p.add_run(
            f"Churn risk: {churn.get('score', '-')}/100 — {band or 'Unscored'}"
        )
        run.font.bold = True
        run.font.size = Pt(14)
        run.font.color.rgb = _band_color(band)
        if (churn.get("rationale_md") or "").strip():
            _body(doc, churn["rationale_md"])

    # ── Signal facts ────────────────────────────────────────────────────────
    facts: list[tuple[str, str]] = []
    if signals.get("client_status"):
        facts.append(("Client status", signals["client_status"]))
    if signals.get("days_since_last_touch") is not None:
        facts.append(("Days since last touch", f"{signals['days_since_last_touch']}d"))
    if signals.get("renewal_countdown_days") is not None:
        facts.append(("Renewal in", f"{signals['renewal_countdown_days']}d"))
    if signals.get("open_pipeline_value"):
        facts.append(("Open pipeline", f"${round(signals['open_pipeline_value']):,}"))
    if signals.get("active_products_count") is not None:
        facts.append(("Products", str(signals["active_products_count"])))
    trend = signals.get("activity_trend") or {}
    if trend:
        facts.append(
            ("Activity 90d/prior", f"{trend.get('last_90d')}/{trend.get('prior_90d')}")
        )
    if signals.get("csm"):
        facts.append(("CSM", signals["csm"]))
    _fact_table(doc, facts)

    # ── Narrative sections (same order as the PDF) ──────────────────────────
    if (sections.get("executive_summary") or "").strip():
        _heading(doc, "Executive summary")
        _body(doc, sections["executive_summary"])

    highlights = [
        h
        for h in (sections.get("relationship_timeline_highlights") or [])
        if str(h).strip()
    ]
    if highlights:
        _heading(doc, "Relationship timeline — moments that matter")
        _bullets(doc, highlights)

    rh = sections.get("relationship_health") or {}
    if isinstance(rh, dict) and (rh.get("narrative_md") or "").strip():
        _heading(doc, "Relationship health")
        _body(doc, rh["narrative_md"])
        chips = [
            f"{k.replace('_', ' ').title()}: {rh[k]}"
            for k in ("cadence", "breadth", "responsiveness", "sentiment_trend")
            if rh.get(k)
        ]
        if chips:
            _bullets(doc, chips)

    if (sections.get("commercial_health") or "").strip():
        _heading(doc, "Commercial health")
        _body(doc, sections["commercial_health"])

    opps = sections.get("opportunities") or []
    if opps:
        _heading(doc, "Expansion opportunities")
        for o in opps:
            o = o if isinstance(o, dict) else {}
            p = doc.add_paragraph()
            entity = str(o.get("entity") or "").strip()
            if entity:
                p.add_run(f"[{entity}] ").font.color.rgb = MUTED
            p.add_run(str(o.get("title") or "")).font.bold = True
            if (o.get("why_now") or "").strip():
                _body(doc, str(o["why_now"]))
            if (o.get("play") or "").strip():
                _muted_para(doc, f"Play: {o['play']}")

    factors = churn.get("factors") or []
    if factors:
        _heading(doc, "Churn factors")
        for f in factors:
            f = f if isinstance(f, dict) else {}
            pts = round(float(f.get("points", 0) or 0))
            name = str(f.get("name", "")).replace("_", " ").title()
            p = doc.add_paragraph()
            p.add_run(f"{pts}  {name} — {f.get('value', '')}").font.bold = True
            if str(f.get("evidence") or "").strip():
                _muted_para(doc, str(f["evidence"]), italic=False)

    news = sections.get("recent_news") or []
    if news:
        _heading(doc, "Recent news")
        for n in news:
            n = n if isinstance(n, dict) else {}
            p = doc.add_paragraph()
            date = str(n.get("date") or "").strip()
            if date:
                p.add_run(f"{date} — ").font.color.rgb = MUTED
            p.add_run(str(n.get("headline") or "(untitled)")).font.bold = True
            if (n.get("why_it_matters") or "").strip():
                _body(doc, str(n["why_it_matters"]))

    sources = [s for s in (sections.get("source_evidence") or []) if str(s).strip()]
    if sources:
        _heading(doc, "Sources")
        _bullets(doc, sources)

    return _docx_bytes(doc)
