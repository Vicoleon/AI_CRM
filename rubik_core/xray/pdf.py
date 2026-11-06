"""Render a Customer X-Ray brief to PDF bytes.

Designed to echo the on-screen X-Ray layout — a band-colored churn score badge,
a grid of signal tiles, violet section-heading chips, risk pills on the churn
factors, and opportunity mini-cards — using fpdf2 primitives only (pure-Python,
no system deps, Cloud-Run-safe; we deliberately do NOT pull weasyprint/etc.).
Pure: never hits the network.
"""

from __future__ import annotations

from typing import Any

from fpdf.enums import MethodReturnValue, XPos, YPos

# Reuse the shared masthead + text helpers + brand palette for a consistent look.
from rubik_core.deep_prep.pdf import (
    BRAND,
    INK,
    _body,
    _bullets,
    _latin1,
    _masthead,
    _PrepPDF,
    _strip_md,
)

WHITE = (255, 255, 255)
SLATE = (100, 116, 139)  # muted label
CARD = (247, 249, 252)  # slate-50 tile fill
BORDER = (226, 232, 240)  # slate-200
_BAND_RGB = {
    "Healthy": (21, 128, 61),
    "Watch": (180, 83, 9),
    "At-risk": (185, 28, 28),
}


def _band_rgb(band: str) -> tuple[int, int, int]:
    return _BAND_RGB.get(band or "", (100, 116, 139))


def _risk_rgb(points: float) -> tuple[int, int, int]:
    if points >= 70:
        return (185, 28, 28)
    if points >= 40:
        return (180, 83, 9)
    return (21, 128, 61)


def _content_w(pdf: _PrepPDF) -> float:
    return pdf.w - pdf.l_margin - pdf.r_margin


def _ensure(pdf: _PrepPDF, needed_h: float) -> None:
    """Page-break before a fixed-height block so a drawn rect never splits."""
    if pdf.get_y() + needed_h > pdf.h - pdf.b_margin:
        pdf.add_page()


def _flatten(text: str) -> str:
    """Collapse light-markdown multi-line prose into one latin-1-safe string."""
    parts = [_strip_md(ln) for ln in (text or "").splitlines()]
    return _latin1(" ".join(p for p in parts if p))


def _section(pdf: _PrepPDF, label: str) -> None:
    """A violet heading chip with a hairline rule — clear separation between
    sections without the dead vertical voids of an all-whitespace gap."""
    pdf.ln(4.5)
    pdf.set_font("Helvetica", "B", 10.5)
    w = pdf.get_string_width(_latin1(label)) + 9
    h = 6.8
    _ensure(pdf, h + 8)
    x, y = pdf.l_margin, pdf.get_y()
    pdf.set_fill_color(*BRAND)
    pdf.rect(x, y, w, h, style="F", round_corners=True, corner_radius=1.8)
    pdf.set_xy(x + 3.5, y)
    pdf.set_text_color(*WHITE)
    pdf.cell(w - 3.5, h, _latin1(label))
    # hairline rule trailing the chip to the right margin
    pdf.set_draw_color(*BORDER)
    pdf.set_line_width(0.3)
    pdf.line(x + w + 3, y + h / 2, pdf.w - pdf.r_margin, y + h / 2)
    pdf.set_xy(pdf.l_margin, y + h + 3)
    pdf.set_text_color(*INK)


def _score_badge(pdf: _PrepPDF, score: Any, band: str, rationale: str) -> None:
    """Full-width hero card: band-colored churn-score badge on the left, a
    'why this score' rationale on the right. The card auto-sizes to the text so
    it always reads as a deliberate, filled block (never a floating badge in a
    void), and the badge is vertically centred within it."""
    cw = _content_w(pdf)
    pad = 5.0
    badge = 27.0
    tx = pdf.l_margin + pad + badge + 6
    tw = cw - pad - badge - 6 - pad
    body = _flatten(rationale) or (
        "Churn risk is computed deterministically from the weighted signals "
        "below; the narrative explains the drivers, not the number."
    )
    # Measure the rationale so the card grows to fit (label + body + padding),
    # but never shorter than the badge it must enclose.
    pdf.set_font("Helvetica", "", 9.5)
    text_h = pdf.multi_cell(
        tw, 4.7, body, dry_run=True, output=MethodReturnValue.HEIGHT
    )
    card_h = max(badge + 2 * pad, text_h + 6 + 2 * pad)
    _ensure(pdf, card_h + 4)
    x, y = pdf.l_margin, pdf.get_y()

    # Card shell.
    pdf.set_draw_color(*BORDER)
    pdf.set_line_width(0.3)
    pdf.set_fill_color(*CARD)
    pdf.rect(x, y, cw, card_h, style="DF", round_corners=True, corner_radius=3)

    # Score badge, vertically centred.
    by = y + (card_h - badge) / 2
    bx = x + pad
    pdf.set_fill_color(*_band_rgb(band))
    pdf.rect(bx, by, badge, badge, style="F", round_corners=True, corner_radius=3)
    pdf.set_text_color(*WHITE)
    pdf.set_font("Helvetica", "B", 23)
    pdf.set_xy(bx, by + 4.0)
    pdf.cell(badge, 11, _latin1(str(score if score is not None else "-")), align="C")
    pdf.set_font("Helvetica", "B", 6.6)
    pdf.set_xy(bx, by + 15.0)
    pdf.cell(badge, 3.4, "/ 100 CHURN RISK", align="C")
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_xy(bx, by + 19.0)
    pdf.cell(badge, 3.6, _latin1((band or "").upper()), align="C")

    # Rationale, top-aligned inside the card.
    pdf.set_xy(tx, y + pad)
    pdf.set_font("Helvetica", "B", 7.2)
    pdf.set_text_color(*SLATE)
    pdf.cell(tw, 3.8, "WHY THIS SCORE")
    pdf.set_xy(tx, y + pad + 5)
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*INK)
    pdf.multi_cell(tw, 4.7, body, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_xy(pdf.l_margin, y + card_h + 3)
    pdf.set_text_color(*INK)


def _tiles(pdf: _PrepPDF, tiles: list[tuple[str, str]]) -> None:
    """A 3-column grid of light signal tiles (label + bold value)."""
    if not tiles:
        return
    cols, gap, th = 3, 4.0, 14.5
    tw = (_content_w(pdf) - gap * (cols - 1)) / cols
    x0 = pdf.l_margin
    pdf.ln(3)
    _ensure(pdf, th + 4)
    y = pdf.get_y()
    pdf.set_line_width(0.3)
    for i, (label, value) in enumerate(tiles):
        col = i % cols
        if col == 0 and i > 0:
            y += th + gap
            if y + th > pdf.h - pdf.b_margin:
                pdf.add_page()
                y = pdf.get_y()
        x = x0 + col * (tw + gap)
        pdf.set_fill_color(*CARD)
        pdf.set_draw_color(*BORDER)
        pdf.rect(x, y, tw, th, style="DF", round_corners=True, corner_radius=2.4)
        pdf.set_xy(x + 3.5, y + 3.0)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(*SLATE)
        pdf.cell(tw - 7, 3.4, _latin1(label.upper()))
        pdf.set_xy(x + 3.5, y + 7.0)
        pdf.set_font("Helvetica", "B", 12.5)
        pdf.set_text_color(*INK)
        pdf.cell(tw - 7, 6, _latin1(str(value)))
    pdf.set_xy(pdf.l_margin, y + th + 2)


def _minichips(pdf: _PrepPDF, items: list[str]) -> None:
    """A row of small light chips (relationship-health sub-metrics)."""
    x = pdf.l_margin
    y = pdf.get_y() + 1
    pdf.set_font("Helvetica", "", 8)
    for it in items:
        label = _latin1(it)
        w = pdf.get_string_width(label) + 6
        if x + w > pdf.w - pdf.r_margin:
            x = pdf.l_margin
            y += 7
        pdf.set_fill_color(*CARD)
        pdf.set_draw_color(*BORDER)
        pdf.rect(x, y, w, 5.5, style="DF", round_corners=True, corner_radius=2.5)
        pdf.set_xy(x, y + 0.4)
        pdf.set_text_color(*SLATE)
        pdf.cell(w, 4.7, label, align="C")
        x += w + 3
    pdf.set_xy(pdf.l_margin, y + 8)
    pdf.set_text_color(*INK)


def _factor_rows(pdf: _PrepPDF, factors: list[dict[str, Any]]) -> None:
    for f in factors:
        pts = round(float(f.get("points", 0) or 0))
        _ensure(pdf, 12)
        x, y = pdf.l_margin, pdf.get_y()
        pill_w = 11.0
        pdf.set_fill_color(*_risk_rgb(pts))
        pdf.rect(
            x, y + 0.4, pill_w, 5.2, style="F", round_corners=True, corner_radius=2.6
        )
        pdf.set_xy(x, y + 0.5)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*WHITE)
        pdf.cell(pill_w, 5, str(pts), align="C")

        tx = x + pill_w + 3
        tw = _content_w(pdf) - pill_w - 3
        name = str(f.get("name", "")).replace("_", " ").title()
        pdf.set_xy(tx, y)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*INK)
        pdf.multi_cell(
            tw,
            4.6,
            _latin1(f"{name} - {f.get('value', '')}"),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
        ev = str(f.get("evidence", "")).strip()
        if ev:
            pdf.set_x(tx)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(*SLATE)
            pdf.multi_cell(tw, 4.2, _latin1(ev), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(0.8)


def _opportunity_cards(pdf: _PrepPDF, opps: list[dict[str, Any]]) -> None:
    for o in opps:
        title = str(o.get("title", "")).strip()
        why = _flatten(str(o.get("why_now", "")))
        play = _flatten(str(o.get("play", "")))
        entity = str(o.get("entity") or "").strip()
        _ensure(pdf, 14)
        y0 = pdf.get_y()
        # entity badge (the product's catalog category), when there is one
        bw = 0.0
        if entity:
            pdf.set_font("Helvetica", "B", 7.5)
            bw = pdf.get_string_width(_latin1(entity)) + 5
            pdf.set_fill_color(*SLATE)
            pdf.rect(
                pdf.l_margin,
                y0 + 0.3,
                bw,
                5,
                style="F",
                round_corners=True,
                corner_radius=2.5,
            )
            pdf.set_xy(pdf.l_margin, y0 + 0.4)
            pdf.set_text_color(*WHITE)
            pdf.cell(bw, 4.6, _latin1(entity), align="C")
        gap = 3 if bw else 0
        # title
        pdf.set_xy(pdf.l_margin + bw + gap, y0)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*INK)
        pdf.multi_cell(
            _content_w(pdf) - bw - gap,
            5,
            _latin1(title),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
        if why:
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*SLATE)
            pdf.multi_cell(
                _content_w(pdf), 4.4, why, new_x=XPos.LMARGIN, new_y=YPos.NEXT
            )
        if play:
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(*INK)
            pdf.multi_cell(
                _content_w(pdf),
                4.4,
                _latin1(f"Play: {play}"),
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )
        pdf.ln(1.2)


def _news(pdf: _PrepPDF, items: list[dict[str, Any]]) -> None:
    """Recent-news items — a date-stamped card with headline + why-it-matters.

    We deliberately do NOT render or link the source URL: web research comes
    back as Vertex grounding-redirect links that both run hundreds of characters
    (an unreadable wall of text) and expire within hours (a dead 404 link is
    worse than none). The date + headline + rationale carry the citation.
    """
    for n in items:
        date = _latin1(str(n.get("date", "")).strip())
        headline = _flatten(str(n.get("headline", ""))) or "(untitled)"
        why = _flatten(str(n.get("why_it_matters", "")))
        cw = _content_w(pdf)
        # Measure so the card encloses all of its text.
        pdf.set_font("Helvetica", "B", 10)
        head_h = pdf.multi_cell(
            cw - 8, 4.8, headline, dry_run=True, output=MethodReturnValue.HEIGHT
        )
        why_h = 0.0
        if why:
            pdf.set_font("Helvetica", "", 9)
            why_h = pdf.multi_cell(
                cw - 8, 4.4, why, dry_run=True, output=MethodReturnValue.HEIGHT
            )
        card_h = 4.5 + head_h + (why_h + 1.5 if why else 0) + 3.5
        _ensure(pdf, card_h + 3)
        x, y = pdf.l_margin, pdf.get_y()
        # Left violet accent bar + light card.
        pdf.set_fill_color(*CARD)
        pdf.set_draw_color(*BORDER)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, cw, card_h, style="DF", round_corners=True, corner_radius=2.4)
        pdf.set_fill_color(*BRAND)
        pdf.rect(x, y, 1.6, card_h, style="F")

        inner_x = x + 5
        inner_w = cw - 8
        pdf.set_xy(inner_x, y + 3)
        if date:
            pdf.set_font("Helvetica", "B", 7.2)
            pdf.set_text_color(*SLATE)
            pdf.cell(inner_w, 3.6, date, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_x(inner_x)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*INK)
        pdf.multi_cell(inner_w, 4.8, headline, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if why:
            pdf.set_x(inner_x)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*SLATE)
            pdf.multi_cell(inner_w, 4.4, why, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_xy(pdf.l_margin, y + card_h + 2.5)
    pdf.set_text_color(*INK)


def render_xray_pdf(
    *,
    company_name: str,
    signals: dict[str, Any],
    sections: dict[str, Any],
    prepared_for: str,
    generated_at: str = "",
) -> bytes:
    """Return PDF bytes for a Customer X-Ray snapshot's signals + sections."""
    signals = signals or {}
    sections = sections or {}
    churn = signals.get("churn") or {}
    title = company_name or "Customer"

    pdf = _PrepPDF(orientation="P", unit="mm", format="A4")
    pdf._doc_title = f"Customer X-Ray - {title}"
    pdf.set_title(pdf._doc_title)  # /Title metadata → viewers suggest this on Save-As
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()

    _masthead(pdf, "CUSTOMER X-RAY", title)

    meta = f"Prepared for {prepared_for}"
    if generated_at:
        meta += f"   |   {generated_at}"
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*SLATE)
    pdf.multi_cell(0, 5, _latin1(meta), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)

    # ── Churn hero + signal tiles ────────────────────────────────────────────
    if churn:
        _score_badge(
            pdf, churn.get("score"), churn.get("band"), churn.get("rationale_md", "")
        )

    tiles: list[tuple[str, str]] = []
    if signals.get("client_status"):
        tiles.append(("Client status", signals["client_status"]))
    if signals.get("days_since_last_touch") is not None:
        tiles.append(("Days since last touch", f"{signals['days_since_last_touch']}d"))
    if signals.get("renewal_countdown_days") is not None:
        tiles.append(("Renewal in", f"{signals['renewal_countdown_days']}d"))
    if signals.get("open_pipeline_value"):
        tiles.append(("Open pipeline", f"${round(signals['open_pipeline_value']):,}"))
    if signals.get("active_products_count") is not None:
        tiles.append(("Products", str(signals["active_products_count"])))
    trend = signals.get("activity_trend") or {}
    if trend:
        arrow = {"up": "^", "down": "v"}.get(trend.get("direction", ""), "-")
        tiles.append(
            (
                "Activity 90d/prior",
                f"{trend.get('last_90d')}/{trend.get('prior_90d')} {arrow}",
            )
        )
    if signals.get("csm"):
        tiles.append(("CSM", signals["csm"]))
    _tiles(pdf, tiles)

    # ── Narrative sections ───────────────────────────────────────────────────
    if (sections.get("executive_summary") or "").strip():
        _section(pdf, "Executive summary")
        _body(pdf, sections["executive_summary"])

    highlights = [
        h
        for h in (sections.get("relationship_timeline_highlights") or [])
        if str(h).strip()
    ]
    if highlights:
        _section(pdf, "Relationship timeline - moments that matter")
        _bullets(pdf, highlights)

    rh = sections.get("relationship_health") or {}
    if isinstance(rh, dict) and (rh.get("narrative_md") or "").strip():
        _section(pdf, "Relationship health")
        _body(pdf, rh["narrative_md"])
        chips = [
            f"{k.replace('_', ' ').title()}: {rh[k]}"
            for k in ("cadence", "breadth", "responsiveness", "sentiment_trend")
            if rh.get(k)
        ]
        if chips:
            _bullets(pdf, chips)

    if (sections.get("commercial_health") or "").strip():
        _section(pdf, "Commercial health")
        _body(pdf, sections["commercial_health"])

    opps = sections.get("opportunities") or []
    if opps:
        _section(pdf, "Expansion opportunities")
        _opportunity_cards(pdf, opps)

    factors = churn.get("factors") or []
    if factors:
        _section(pdf, "Churn factors")
        _factor_rows(pdf, factors)

    news = sections.get("recent_news") or []
    if news:
        _section(pdf, "Recent news")
        _news(pdf, news)

    sources = [s for s in (sections.get("source_evidence") or []) if str(s).strip()]
    if sources:
        _section(pdf, "Sources")
        _bullets(pdf, sources)

    return bytes(pdf.output())
