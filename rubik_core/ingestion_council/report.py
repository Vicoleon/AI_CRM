"""Rendering for a :class:`CouncilVerdict`.

Two outputs, both pure (no I/O):

* :func:`render_html` — a self-contained briefing document (inline CSS, Cubie
  violet) persisted with the verdict and served to operators.
* :func:`build_slack_blocks` — the Slack DM: verdict banner, top risks, the one
  action, per-run health, and a link to the full report.

All LLM/DB-sourced strings are HTML-escaped; Slack text is length-capped so one
oversized section can't get the whole message rejected as ``invalid_blocks``.
"""

from __future__ import annotations

import html
from typing import Any

from rubik_core.brand import BRAND_HEX, PRODUCT_NAME
from rubik_core.ingestion_council.models import CouncilVerdict, Health, RunKind

_HEALTH_COLOR = {"pass": "#1f9d55", "warn": "#b7791f", "fail": "#c53030"}
_HEALTH_WORD = {"pass": "HEALTHY", "warn": "NEEDS ATTENTION", "fail": "ACTION REQUIRED"}
_SEV_DOT = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
_BRAND = BRAND_HEX
_SLACK_SECTION_CAP = 2900  # Slack rejects a section's mrkdwn over 3000 chars


# ── HTML report ────────────────────────────────────────────────────────────────


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _risk_rows(verdict: CouncilVerdict) -> str:
    if not verdict.top_risks:
        return (
            '<p class="empty">No risks surfaced — the council found this run clean.</p>'
        )
    out = []
    for r in verdict.top_risks:
        dot = _SEV_DOT.get(r.severity, "•")
        kind = f' <span class="tag">{_esc(r.run_kind)}</span>' if r.run_kind else ""
        out.append(
            f'<li><span class="sev">{dot}</span> <strong>{_esc(r.title)}</strong>{kind}'
            f'<div class="detail">{_esc(r.detail)}</div></li>'
        )
    return f'<ul class="risks">{"".join(out)}</ul>'


def _kind_grid(verdict: CouncilVerdict) -> str:
    if not verdict.per_kind_health:
        return ""
    cells = []
    for kind, h in verdict.per_kind_health.items():
        color = _HEALTH_COLOR.get(str(h), "#666")
        try:
            label = RunKind(kind).label
        except ValueError:
            label = kind
        cells.append(
            f'<div class="kind"><span class="pill" style="background:{color}">'
            f"{_esc(str(h).upper())}</span><div>{_esc(label)}</div></div>"
        )
    return f'<div class="kinds">{"".join(cells)}</div>'


def _auditor_sections(verdict: CouncilVerdict) -> str:
    out = []
    for a in verdict.auditor_findings:
        color = _HEALTH_COLOR.get(a.health.value, "#666")
        items = "".join(
            f'<li><span class="sev">{_SEV_DOT.get(f.severity, "•")}</span> '
            f"<strong>{_esc(f.title)}</strong>: {_esc(f.detail)}</li>"
            for f in a.findings
        )
        items_html = f"<ul>{items}</ul>" if items else '<p class="empty">No issues.</p>'
        out.append(
            f'<div class="auditor"><h4><span class="pill" style="background:{color}">'
            f"{_esc(a.health.value.upper())}</span> {_esc(a.lens)}</h4>"
            f'<p class="summary">{_esc(a.summary)}</p>{items_html}</div>'
        )
    return "".join(out)


def _peer_html(verdict: CouncilVerdict) -> str:
    if not verdict.peer_reviews:
        return ""
    rows = "".join(
        f"<li><strong>{_esc(p.reviewer_id)}</strong> — strongest: "
        f"{_esc(p.strongest or '?')}; blind spot: {_esc(p.biggest_blind_spot or '—')}; "
        f"all missed: {_esc(p.what_all_missed or '—')}</li>"
        for p in verdict.peer_reviews
    )
    return (
        f'<details><summary>Peer review</summary><ul class="peer">{rows}</ul></details>'
    )


def _evidence_html(verdict: CouncilVerdict) -> str:
    blocks = []
    for e in verdict.evidence:
        stat_rows = "".join(
            f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in e.stats.items()
        )
        check_rows = "".join(
            f'<li class="{"ok" if c.ok else "bad"}">'
            f"{'✓' if c.ok else '✗'} {_esc(c.name)}"
            f"{f' — {_esc(c.detail)}' if c.detail else ''}</li>"
            for c in e.cross_checks
        )
        blocks.append(
            f"<details><summary>{_esc(e.kind.label)} "
            f"(lookback {e.window_hours}h)</summary>"
            f'<ul class="checks">{check_rows}</ul>'
            f'<table class="stats">{stat_rows}</table></details>'
        )
    return "".join(blocks)


def render_html(verdict: CouncilVerdict) -> str:
    """Self-contained HTML briefing for one council verdict."""
    hv = verdict.overall_health.value
    color = _HEALTH_COLOR.get(hv, "#666")
    word = _HEALTH_WORD.get(hv, hv.upper())
    action = (
        f'<div class="action"><span>Do this first</span><p>{_esc(verdict.one_action)}</p></div>'
        if verdict.one_action
        else ""
    )
    agreements = "".join(f"<li>{_esc(x)}</li>" for x in verdict.agreements)
    clashes = "".join(f"<li>{_esc(x)}</li>" for x in verdict.clashes)
    blinds = "".join(f"<li>{_esc(x)}</li>" for x in verdict.blind_spots)

    def _opt(title: str, items: str) -> str:
        return (
            f"<details><summary>{title}</summary><ul>{items}</ul></details>"
            if items
            else ""
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ingestion Council — {_esc(verdict.scope)} — {_esc(hv)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
         color:#1a1a1a; background:#f6f5f4; line-height:1.5; }}
  .wrap {{ max-width:820px; margin:0 auto; padding:24px; }}
  .banner {{ background:{color}; color:#fff; border-radius:12px; padding:20px 24px; }}
  .banner .word {{ font-size:13px; letter-spacing:.12em; opacity:.9; }}
  .banner h1 {{ margin:.2em 0 0; font-size:24px; }}
  .banner .meta {{ font-size:13px; opacity:.85; margin-top:6px; }}
  .action {{ background:#fff; border-left:4px solid {_BRAND}; border-radius:8px;
             padding:14px 18px; margin:18px 0; }}
  .action span {{ font-size:11px; text-transform:uppercase; letter-spacing:.1em; color:{_BRAND}; }}
  .action p {{ margin:.3em 0 0; font-weight:600; }}
  h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.06em; color:#555;
        margin:26px 0 10px; }}
  ul.risks {{ list-style:none; padding:0; }}
  ul.risks li {{ background:#fff; border-radius:8px; padding:12px 14px; margin-bottom:8px; }}
  ul.risks .detail {{ color:#555; font-size:14px; margin-top:4px; }}
  .tag {{ font-size:11px; background:#eee; border-radius:4px; padding:1px 6px; color:#666; }}
  .kinds {{ display:flex; gap:10px; flex-wrap:wrap; }}
  .kind {{ background:#fff; border-radius:8px; padding:12px; text-align:center; flex:1; min-width:130px; font-size:13px; }}
  .pill {{ display:inline-block; color:#fff; font-size:11px; font-weight:700;
           border-radius:20px; padding:2px 10px; }}
  .auditor {{ background:#fff; border-radius:8px; padding:12px 16px; margin-bottom:10px; }}
  .auditor h4 {{ margin:0 0 6px; font-size:15px; }}
  .auditor .summary {{ color:#333; margin:0 0 8px; }}
  .auditor ul, .peer, .checks {{ margin:0; padding-left:18px; }}
  .empty {{ color:#1f9d55; font-size:14px; }}
  details {{ background:#fff; border-radius:8px; padding:8px 14px; margin-bottom:8px; }}
  summary {{ cursor:pointer; font-weight:600; }}
  table.stats {{ border-collapse:collapse; width:100%; margin-top:8px; font-size:13px; }}
  table.stats td {{ border-top:1px solid #eee; padding:4px 6px; vertical-align:top; }}
  table.stats td:first-child {{ color:#666; width:45%; }}
  .checks {{ list-style:none; padding:0; }}
  .checks li.ok {{ color:#1f7a43; }}
  .checks li.bad {{ color:#c53030; font-weight:600; }}
  footer {{ color:#999; font-size:12px; margin-top:28px; text-align:center; }}
</style></head>
<body><div class="wrap">
  <div class="banner">
    <div class="word">INGESTION COUNCIL · {word}</div>
    <h1>{_esc(verdict.headline)}</h1>
    <div class="meta">scope: {_esc(verdict.scope)} · generated {_esc(verdict.generated_at)}</div>
  </div>
  {action}
  <h2>Top risks</h2>
  {_risk_rows(verdict)}
  <h2>Per-run health</h2>
  {_kind_grid(verdict)}
  <h2>The council</h2>
  {_auditor_sections(verdict)}
  {_peer_html(verdict)}
  {_opt("Where the council agreed", agreements)}
  {_opt("Where the council clashed", clashes)}
  {_opt("Blind spots caught in peer review", blinds)}
  <h2>Evidence</h2>
  {_evidence_html(verdict)}
  <footer>{PRODUCT_NAME} Ingestion Council · adversarial multi-agent QA over overnight &amp; weekly ingestions</footer>
</div></body></html>"""


# ── Slack Block Kit ─────────────────────────────────────────────────────────────


def _cap(text: str, limit: int = _SLACK_SECTION_CAP) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_slack_blocks(
    verdict: CouncilVerdict, report_url: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Render the verdict as a Slack DM. Returns ``(fallback_text, blocks)``."""
    hv = verdict.overall_health
    header = f"{hv.emoji} Ingestion Council — {verdict.scope} — {hv.value.upper()}"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _cap(header, 150)}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _cap(f"*{verdict.headline}*")},
        },
    ]
    fallback = [header, verdict.headline]

    if verdict.per_kind_health:
        line = "  ".join(
            f"{Health.coerce(h).emoji} {k}" for k, h in verdict.per_kind_health.items()
        )
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": _cap(line)}]}
        )

    if verdict.top_risks:
        risk_lines = []
        for r in verdict.top_risks[:6]:
            dot = _SEV_DOT.get(r.severity, "•")
            where = f" _({r.run_kind})_" if r.run_kind else ""
            risk_lines.append(f"{dot} *{r.title}*{where}\n{r.detail}")
            fallback.append(f"- {r.title}: {r.detail}")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _cap("\n".join(risk_lines))},
            }
        )

    if verdict.one_action:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _cap(f"*Do this first:* {verdict.one_action}"),
                },
            }
        )
        fallback.append(f"Do this first: {verdict.one_action}")

    if report_url and report_url.startswith("https://"):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View full report"},
                        "url": report_url,
                    }
                ],
            }
        )

    return "\n".join(fallback), blocks
