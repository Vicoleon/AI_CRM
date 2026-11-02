"""The newer data sources a Meeting Prep brief is built from.

Contract shared by the generation path, the version chain and the one-section
rewrite (do not change the shapes without updating all three):

* ``ctx["signal_blocks"]`` — ``dict[str, str]``: block name → the formatted
  prompt text exactly as it went into the brief prompt. Empty / missing blocks
  are simply absent (the "no block → no clause → byte-identical prompt" rule).
* ``ctx["account_snapshot"]`` — the client card (see ``MeetingPrepResponse``),
  or ``None``.
* ``ctx["signals_used"]`` — ``[{"key", "label", "detail"}]`` for the
  "Built from" strip.

``bundle_fields`` copies those into the persisted ``context_bundle`` so a later
section rewrite sees the same evidence the brief was built from.

The sources (each behind its own ``CUBIE_PREP_*_ENABLED`` kill switch, each
fail-soft, each read through the access rule that already governs it):

* ``crm`` — HubSpot's live account record for the EXTERNAL companies on the
  invite. Domains resolve through ``call_capture.domain_match`` (never the
  ``candidates[0]`` pattern: an ambiguous domain is reported, not picked), deals
  are restricted to this workspace's configured pipelines (the portal also hosts a
  client's pipeline), and the deal and company last-contact dates are shown side
  by side (the company's date BESIDE the deal's, never instead of it).
* ``xray`` — the latest Customer X-Ray already on file for each matched company,
  only for readers the ``customer_xray`` feature admits.
* ``last_meeting_notes`` — the summary / action-items part of the two shared
  meetings that best fit this invite (same people, same title; never a much
  bigger group meeting), read only where the reader holds a
  ``meeting_participants`` row (or owns the transcript).
* ``promises`` — the reader's OWN open to-dos owed to the people on the invite,
  only when the reader has My To-Dos or promise follow-through.
* ``availability`` — time off on file for INTERNAL attendees.
* ``flagged`` — today's AI Priorities cards (the reader's own) that involve this
  meeting, an attendee or the client's domain.

Everything that reaches the prompt or the client card is copied from the
records verbatim; nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import partial
from hashlib import sha256
from typing import Any

from rubik_core.brand import PRODUCT_NAME
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.secrets import workspace_secret

logger = logging.getLogger(__name__)

# Per-stage budget for one source. The stages run inside the same gather as the
# attendee research (budget 60s), so a slow source never adds latency on its own.
SIGNAL_TIMEOUT_S = 15.0

MAX_CRM_DOMAINS = 3
MAX_OPEN_DEALS = 10  # per company, on the card
MAX_OPEN_DEALS_IN_PROMPT = 5
LAST_MEETING_NOTES_LIMIT = 2
LAST_MEETING_NOTES_MAX_CHARS = 2500
LAST_MEETING_SECTION_MAX_CHARS = 1200
# Fathom appends its action items and summary AFTER the transcript body, so the
# read has to reach past a typical hour-long transcript to find them.
TRANSCRIPT_READ_BYTES = 300_000
AVAILABILITY_HORIZON_DAYS = 14
MAX_PROMISES = 8
MAX_FLAGGED = 5
XRAY_CARD_SUMMARY_CHARS = 600

# Block order inside RESEARCH DATA / CONTEXTUAL DATA (and Deep Prep's context).
BLOCK_ORDER: tuple[str, ...] = (
    "crm",
    "xray",
    "last_meeting_notes",
    "promises",
    "availability",
    "flagged",
)

_DIR_TTL_S = 1800
_DIR_CACHE: dict[str, dict[str, Any]] = {}


def bundle_fields(ctx: dict[str, Any]) -> dict[str, Any]:
    """The signal keys to store in ``MeetingPrepRun.context_bundle``."""
    return {
        "signal_blocks": dict(ctx.get("signal_blocks") or {}),
        "account_snapshot": ctx.get("account_snapshot"),
        "signals_used": list(ctx.get("signals_used") or []),
        "crm_provenance": ctx.get("crm_provenance"),
        "flagged_provenance_stamped": bool(ctx.get("flagged_provenance_stamped")),
    }


# ── Plumbing ──────────────────────────────────────────────────────────────────


async def run_bounded(fn, *args, timeout: float | None = None, **kwargs):
    """Run a sync source read in a worker thread, abandoned after ``timeout``
    (default ``SIGNAL_TIMEOUT_S``). Raises ``asyncio.TimeoutError`` on expiry.

    ``abandon_on_cancel`` is what makes the bound real: without it anyio shields
    the thread and ``wait_for`` waits for a hung HubSpot call anyway.
    """
    import anyio

    return await asyncio.wait_for(
        anyio.to_thread.run_sync(partial(fn, *args, **kwargs), abandon_on_cancel=True),
        SIGNAL_TIMEOUT_S if timeout is None else timeout,
    )


def _settings():
    from rubik_core.config import settings

    return settings


def _session():
    # Resolved at call time: tests rebind ``db.SessionLocal`` to a throwaway
    # sqlite, and a name bound at import would keep pointing at the old one.
    from rubik_core.database import db as db_module

    return db_module.SessionLocal()


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _short_date(d: date) -> str:
    return f"{d:%b} {d.day}"


def _day_label(d: date) -> str:
    return f"{d:%a} {d:%b} {d.day}"


def _parse_iso_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ── The reader (for the per-feature gates) ───────────────────────────────────


def resolve_reader(user_email: str) -> Any | None:
    """The reader's profile from the users table, or None.

    Used when the caller had no ``UserProfile`` to hand (the Deep Prep job). None
    means every reader-gated source is skipped — fail closed.
    """
    try:
        from rubik_core.database.repository import SQLUserRegistry

        return SQLUserRegistry().get_user(user_email)
    except Exception:  # noqa: BLE001 — a missing profile skips gated sources
        logger.info("meeting-prep signals: no reader profile for %s", user_email)
        return None


def reader_allows(key: str, reader: Any | None) -> bool:
    """``feature_allows`` for the reader, fail-closed."""
    if reader is None:
        return False
    try:
        from rubik_core.features import feature_allows

        return bool(feature_allows(key, reader))
    except Exception:  # noqa: BLE001 — an unreadable policy is a "no"
        logger.warning("meeting-prep signals: feature check %s failed", key)
        return False


def promises_allowed(reader: Any | None) -> bool:
    """The reader may see their ledger: My To-Dos or promise follow-through,
    each with its own env switch, plus Meeting Prep's own switch."""
    settings = _settings()
    if reader is None or not settings.CUBIE_PREP_PROMISES_ENABLED:
        return False
    if getattr(settings, "TODOS_ENABLED", False) and reader_allows("todos", reader):
        return True
    return bool(
        getattr(settings, "PROMISE_ACCOUNTABILITY_ENABLED", False)
    ) and reader_allows("promise_accountability", reader)


def reader_gated_sources_enabled() -> bool:
    settings = _settings()
    return bool(
        settings.CUBIE_PREP_XRAY_ENABLED or settings.CUBIE_PREP_PROMISES_ENABLED
    )


# ── Who is on the invite ─────────────────────────────────────────────────────


def crm_domains(attendees: list[dict], user_email: str) -> tuple[list[str], list[str]]:
    """``(domains to look up, domains skipped for the cap)``.

    Matchable external company domains only — consumer mail hosts, the company's own
    domain, placeholders and calendar pseudo-domains never reach HubSpot (the
    rules live in ``call_capture.domain_match``; nothing is re-listed here).
    """
    try:
        from rubik_core.call_capture.domain_match import external_domains_for_event

        domains, _ = external_domains_for_event(
            {"attendees": attendees or []}, ae_email=user_email
        )
    except Exception:  # noqa: BLE001 — no domains means no CRM block
        logger.warning("meeting-prep signals: domain filter failed", exc_info=True)
        return [], []
    return domains[:MAX_CRM_DOMAINS], domains[MAX_CRM_DOMAINS:]


def internal_attendee_emails(attendees: list[dict], user_email: str) -> list[str]:
    """Colleagues on any verified workspace domain, reader excluded."""
    from rubik_core.company.domain import workspace_domains

    me = (user_email or "").strip().lower()
    internal_domains = workspace_domains()
    out: list[str] = []
    for a in attendees or []:
        if not isinstance(a, dict) or a.get("resource") is True:
            continue
        email = (a.get("email") or "").strip().lower()
        if not email or "@" not in email or email == me:
            continue
        if email.split("@")[-1] in internal_domains and email not in out:
            out.append(email)
    return out


def _attendee_names(attendees: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in attendees or []:
        if not isinstance(a, dict):
            continue
        email = (a.get("email") or "").strip().lower()
        if email:
            out[email] = (
                a.get("display_name") or a.get("displayName") or a.get("email") or ""
            ).strip() or email
    return out


# ── 1. HubSpot account record ────────────────────────────────────────────────


@dataclass
class AccountRecords:
    """What HubSpot said about the invite's companies.

    ``companies`` is the client-card list (``account_snapshot["companies"]``
    shape, ``xray`` still None) except that the two last-contact fields are
    still UTC instants (ISO-8601): ``assemble`` turns them into the reader's
    calendar dates. ``deal_ids`` / ``company_ids`` feed the to-do matching;
    ``errors`` are reader-safe partial-error lines.
    """

    companies: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    deal_ids: set[str] = field(default_factory=set)
    company_ids: list[str] = field(default_factory=list)
    skipped_domains: list[str] = field(default_factory=list)


def crm_configured() -> bool:
    return bool((workspace_secret("hubspot", "read_token") or "").strip())


def resolve_companies(domains: list[str], user_email: str) -> dict[str, Any]:
    """``domain → DomainMatch`` (cached, blocklist-gated, never raises)."""
    from rubik_core.call_capture.domain_match import resolve_domain_matches

    return resolve_domain_matches(domains, ae_email=user_email)


def _directories(client) -> tuple[dict[str, str], dict[str, str]]:
    """Stage labels and owner names, cached per workspace for 30 minutes.

    Both lookups are best-effort and return {} on failure; an empty answer is
    not cached, so the next brief tries again.
    """
    now = datetime.utcnow()
    slug = current_workspace()
    token = getattr(client, "token", None)
    token_hash = sha256(token.encode()).hexdigest() if isinstance(token, str) else None
    entry = _DIR_CACHE.get(slug)
    if (
        entry is not None
        and entry["token_hash"] == token_hash
        and (now - entry["at"]).total_seconds() < _DIR_TTL_S
    ):
        return entry["stages"], entry["owners"]
    stages = client.deal_stage_labels() or {}
    owners = client.owner_name_map() or {}
    if stages and owners:
        _DIR_CACHE[slug] = {
            "at": now,
            "stages": stages,
            "owners": owners,
            "token_hash": token_hash,
        }
    return stages, owners


def _hs_instant(value: Any) -> datetime | None:
    """A HubSpot datetime → an aware UTC instant, dropping the epoch junk HubSpot
    returns for unset rollups (``1970-01-21``). ``notes_last_contacted`` is a
    datetime property, so a value without an offset is read as UTC."""
    from rubik_core.retrieval.hubspot_api import _parse_hs_datetime, _plausible_date

    plausible = _plausible_date(value)
    dt = _parse_hs_datetime(plausible) if plausible else None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _reader_tz(tz_name: str | None):
    """The reader's zone, else HubSpot's default (Central), else UTC."""
    from zoneinfo import ZoneInfo

    try:
        if tz_name:
            return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — an unknown zone name falls back
        pass
    from rubik_core.retrieval.hubspot_api import _tz

    return _tz()


def _local_day(value: Any, tz) -> str | None:
    """A last-contact instant (ISO-8601) → the reader's calendar date, so 8 PM
    Central activity is not dated the next (UTC) day. A bare date is kept."""
    text = str(value or "").strip()
    if len(text) <= 10:
        return text or None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return text[:10]
    return (dt.astimezone(tz) if dt.tzinfo else dt).date().isoformat()


def _amount(props: dict) -> float | None:
    try:
        raw = props.get("amount")
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


_DEAL_PROPS = [
    "dealname",
    "pipeline",
    "dealstage",
    "amount",
    "closedate",
    "hubspot_owner_id",
    "hs_is_closed",
    "hs_is_closed_won",
    "notes_last_contacted",
]
_COMPANY_PROPS = ["name", "domain", "lifecyclestage", "notes_last_contacted"]


def _blank_company(domain: str, *, name: str | None = None) -> dict[str, Any]:
    return {
        "name": name or domain,
        "domain": domain,
        "hubspot_company_id": None,
        "hubspot_url": None,
        "lifecycle_stage": None,
        "open_deals": [],
        "won_deals_count": None,
        "last_won": None,
        "deal_last_contact": None,
        "company_last_contact": None,
        "xray": None,
        "ambiguous": False,
        "note": None,
    }


def fetch_account_records(
    domains: list[str], matches: dict[str, Any], *, client=None
) -> AccountRecords:
    """Read the matched companies' deals and contact dates from HubSpot.

    Sync (HTTP); call through ``run_bounded``. Per-company failures become a
    partial-error line and an entry without deal facts — never a guess.
    """
    from rubik_core.sales.rhythm_checks import record_url
    from rubik_core.tenancy.hubspot_config import get_hubspot_config
    from rubik_core.xray.fetchers import _with_backoff

    allowed = set(get_hubspot_config().pipeline_ids)
    out = AccountRecords()

    matched: list[tuple[str, Any]] = []
    for dom in domains:
        m = matches.get(dom)
        if m is None:
            continue
        if m.error:
            out.errors.append(f"HubSpot couldn't be checked for {dom}.")
        elif m.ambiguous:
            entry = _blank_company(dom)
            entry["ambiguous"] = True
            entry["note"] = (
                f"Multiple HubSpot companies match {dom} — "
                f"{PRODUCT_NAME} did not pick one."
            )
            out.companies.append(entry)
        elif m.unmatched:
            entry = _blank_company(dom)
            entry["note"] = "No HubSpot company is available for this domain."
            out.companies.append(entry)
        elif m.company is not None:
            matched.append((dom, m.company))
            out.companies.append(_blank_company(dom))

    if not matched or not allowed:
        return out

    from rubik_core.retrieval.hubspot_api import HubSpotAPIClient

    client = client or HubSpotAPIClient()
    ids = [c.hubspot_company_id for _, c in matched]
    try:
        owned = client.fully_owned_deal_associations("companies", ids)
    except Exception:  # noqa: BLE001 — no unproven company data in a brief
        logger.warning(
            "meeting-prep crm: company ownership check failed", exc_info=True
        )
        out.errors.append("HubSpot company access could not be checked.")
        return out
    matched = [
        (dom, company)
        for dom, company in matched
        if company.hubspot_company_id in owned
    ]
    if not matched:
        return out
    stages, owners = _directories(client)
    ids = [c.hubspot_company_id for _, c in matched]
    out.company_ids = list(ids)
    company_props: dict[str, dict] = {}
    try:
        rows = _with_backoff(client.batch_read, "companies", ids, _COMPANY_PROPS)
        company_props = {str(r.get("id")): r.get("properties") or {} for r in rows}
    except Exception:  # noqa: BLE001 — the deal facts still stand on their own
        logger.warning("meeting-prep crm: company batch read failed", exc_info=True)

    by_domain = {e["domain"]: e for e in out.companies}
    for dom, company in matched:
        cid = company.hubspot_company_id
        entry = by_domain[dom]
        props = company_props.get(cid) or {}
        company_seen = _hs_instant(props.get("notes_last_contacted"))
        entry.update(
            name=(props.get("name") or company.name or dom).strip(),
            hubspot_company_id=cid,
            hubspot_url=record_url("company", cid),
            lifecycle_stage=(props.get("lifecyclestage") or company.lifecyclestage)
            or None,
            company_last_contact=company_seen.isoformat() if company_seen else None,
        )
        if not allowed:
            entry["note"] = "Deal details aren't configured for this workspace."
            continue
        filters: list[dict] = [
            {"propertyName": "associations.company", "operator": "EQ", "value": cid}
        ]
        filters.append(
            {
                "propertyName": "pipeline",
                "operator": "IN",
                "values": sorted(allowed),
            }
        )
        try:
            deals = _with_backoff(
                client.search_all,
                "deals",
                filters,
                _DEAL_PROPS,
                sorts=[{"propertyName": "closedate", "direction": "DESCENDING"}],
            )
        except Exception:  # noqa: BLE001 — one company's deals, not the brief
            logger.warning("meeting-prep crm: deals failed for %s", cid, exc_info=True)
            out.errors.append(f"HubSpot deals couldn't be read for {entry['name']}.")
            entry["note"] = "Deal details couldn't be read from HubSpot."
            continue

        open_deals: list[dict] = []
        won: list[dict] = []
        contacts: list[datetime] = []
        for deal in deals or []:
            p = deal.get("properties") or {}
            # Belt and braces: the server-side filter is the real gate, but a
            # client pipeline must never reach the brief.
            if str(p.get("pipeline") or "") not in allowed:
                continue
            deal_id = str(deal.get("id") or "")
            if deal_id:
                out.deal_ids.add(deal_id)
            last = _hs_instant(p.get("notes_last_contacted"))
            if last:
                contacts.append(last)
            if str(p.get("hs_is_closed_won") or "").lower() == "true":
                won.append(p)
            elif str(p.get("hs_is_closed") or "").lower() == "false":
                stage = str(p.get("dealstage") or "")
                owner_id = str(p.get("hubspot_owner_id") or "")
                open_deals.append(
                    {
                        "name": (p.get("dealname") or "(unnamed deal)").strip(),
                        "stage": (stages.get(stage) or stage) or None,
                        "amount": _amount(p),
                        "close_date": (str(p.get("closedate") or "")[:10] or None),
                        "owner": owners.get(owner_id) if owner_id else None,
                        "url": record_url("deal", deal_id) if deal_id else None,
                    }
                )
        open_deals.sort(key=lambda d: (d["close_date"] is None, d["close_date"] or ""))
        won.sort(key=lambda p: str(p.get("closedate") or ""), reverse=True)
        entry["open_deals"] = open_deals[:MAX_OPEN_DEALS]
        entry["won_deals_count"] = len(won)
        entry["last_won"] = (
            {
                "name": (won[0].get("dealname") or "(unnamed deal)").strip(),
                "close_date": str(won[0].get("closedate") or "")[:10] or None,
            }
            if won
            else None
        )
        entry["deal_last_contact"] = max(contacts).isoformat() if contacts else None
    return out


def _money(amount: float | None) -> str | None:
    return f"${amount:,.0f}" if isinstance(amount, (int, float)) else None


def format_crm_block(
    companies: list[dict], *, skipped_domains: list[str] | None = None, as_of: str = ""
) -> str:
    """The CRM ACCOUNT RECORD prompt block, or "" when HubSpot said nothing."""
    if not companies:
        return ""
    lines = [
        "## CRM ACCOUNT RECORD (HubSpot, live — authoritative for deal stage, "
        "amount and dates)"
    ]
    for c in companies:
        dom = c.get("domain") or ""
        if c.get("ambiguous"):
            lines.append(
                f"- {dom}: {c.get('note') or 'several HubSpot companies use this domain'}"
                " Do not assume which one this meeting is with."
            )
            continue
        if not c.get("hubspot_company_id"):
            lines.append(
                f"- {dom}: no HubSpot company uses this domain (no CRM record — "
                "that alone says nothing about the relationship)."
            )
            continue
        head = f"- {c.get('name') or dom} ({dom})"
        if c.get("lifecycle_stage"):
            head += f" — lifecycle stage: {c['lifecycle_stage']}"
        lines.append(head)
        if c.get("won_deals_count") is None:
            lines.append(f"  {c.get('note') or 'Deal details were not available.'}")
        else:
            deals = c.get("open_deals") or []
            if deals:
                lines.append("  Open deals (our pipelines):")
                for d in deals[:MAX_OPEN_DEALS_IN_PROMPT]:
                    bits = [f'"{d.get("name")}"']
                    if d.get("stage"):
                        bits.append(f"stage {d['stage']}")
                    money = _money(d.get("amount"))
                    if money:
                        bits.append(money)
                    if d.get("close_date"):
                        bits.append(f"close date {d['close_date']}")
                    if d.get("owner"):
                        bits.append(f"owner {d['owner']}")
                    lines.append("  • " + "; ".join(bits))
                if len(deals) > MAX_OPEN_DEALS_IN_PROMPT:
                    lines.append(
                        f"  (+{len(deals) - MAX_OPEN_DEALS_IN_PROMPT} more open deals)"
                    )
            else:
                lines.append("  Open deals (our pipelines): none")
            won = c.get("won_deals_count") or 0
            if won:
                lw = c.get("last_won") or {}
                lines.append(
                    f'  Won deals: {won} (most recent: "{lw.get("name")}", '
                    f"closed {lw.get('close_date') or 'date not recorded'})"
                )
            else:
                lines.append("  Won deals: none on record in our pipelines")
        # Both dates, each labelled; one never stands in for the other.
        deals_read = c.get("won_deals_count") is not None
        deal_date = c.get("deal_last_contact") or (
            "not recorded" if deals_read else "not read"
        )
        lines.append(
            f"  Last logged contact — on its deals: {deal_date}; on the company "
            f"record: {c.get('company_last_contact') or 'not recorded'}"
        )
    if skipped_domains:
        lines.append(
            f"({_plural(len(skipped_domains), 'more client domain')} on the invite "
            f"not looked up: {', '.join(skipped_domains)})"
        )
    tail = "deals in our own pipelines only"
    lines.append(f"(as of {as_of[:10]}; {tail})" if as_of else f"({tail})")
    return "\n".join(lines)


# ── 2. Customer X-Ray on file ────────────────────────────────────────────────


def _xray_record(snap: Any) -> tuple[dict, dict]:
    """``(card dict, prompt material)`` for one ready snapshot."""
    sections = snap.sections or {}
    churn = (snap.signals or {}).get("churn") or {}
    score = churn.get("score")
    summary = sections.get("executive_summary")
    summary = summary if isinstance(summary, str) else ""
    health = sections.get("relationship_health")
    narrative = (
        health.get("narrative_md")
        if isinstance(health, dict)
        else health
        if isinstance(health, str)
        else ""
    )
    plays = []
    for opp in sections.get("opportunities") or []:
        if isinstance(opp, dict) and opp.get("title"):
            plays.append(
                str(opp["title"]).strip()
                + (f" — {str(opp['why_now']).strip()}" if opp.get("why_now") else "")
            )
    created = snap.created_at.isoformat() if snap.created_at else None
    card = {
        "version": int(snap.version or 1),
        "churn_band": churn.get("band") or None,
        "churn_score": score if isinstance(score, (int, float)) else None,
        "summary": _clip(summary, XRAY_CARD_SUMMARY_CHARS),
        "created_at": created,
        "url": f"/customers/{snap.company_id}/xray",
    }
    material = {
        "company_name": snap.company_name or "",
        "summary": _clip(summary, 1200),
        "relationship": _clip(narrative or "", 800),
        "plays": plays[:3],
    }
    return card, material


def fetch_xray_summaries(company_ids: list[str]) -> dict[str, tuple[dict, dict]]:
    """Latest READY X-Ray per company id → ``(card, prompt material)``.

    A database read only; never starts an X-Ray run.
    """
    if not company_ids:
        return {}
    from rubik_core.xray.persistence import snapshot_index

    db = _session()
    try:
        index = snapshot_index(db, list(company_ids))
        return {cid: _xray_record(snap) for cid, snap in index.items()}
    finally:
        db.close()


def format_xray_block(entries: list[tuple[str, dict, dict]]) -> str:
    """``entries`` = ``[(company name, card, material)]``."""
    if not entries:
        return ""
    lines = [
        f"## CUSTOMER X-RAY ON FILE ({PRODUCT_NAME}'s own earlier account analysis — dated; "
        "not live data)"
    ]
    for name, card, material in entries:
        when = (card.get("created_at") or "")[:10] or "date unknown"
        lines.append(f"### {name} — X-Ray v{card.get('version')}, {when}")
        if card.get("churn_band") or card.get("churn_score") is not None:
            score = card.get("churn_score")
            score_txt = f" ({score:g}/100)" if isinstance(score, (int, float)) else ""
            lines.append(f"Churn risk: {card.get('churn_band') or 'n/a'}{score_txt}")
        if material.get("summary"):
            lines.append(f"Summary: {material['summary']}")
        if material.get("relationship"):
            lines.append(f"Relationship health: {material['relationship']}")
        if material.get("plays"):
            lines.append("Growth plays it flagged:")
            lines.extend(f"- {p}" for p in material["plays"])
    return "\n".join(lines)


# ── 3. The reader's open to-dos for these people ─────────────────────────────


def fetch_open_promises(user_email: str) -> list[dict]:
    """The reader's own open ledger rows, as plain dicts (owner-scoped read)."""
    from rubik_core.promises.store import list_open

    db = _session()
    try:
        return [
            {
                "id": r.id,
                "title": r.title,
                "status": r.status,
                "due_local": r.due_local.isoformat() if r.due_local else None,
                "counterparty_kind": r.counterparty_kind,
                "counterparty_id": r.counterparty_id,
                "counterparty_label": r.counterparty_label,
                "is_private": bool(r.is_private),
            }
            for r in list_open(db, user_email)
        ]
    finally:
        db.close()


def promises_for_meeting(
    promises: list[dict],
    *,
    attendees: list[dict],
    user_email: str,
    domains: list[str],
    records: AccountRecords | None,
) -> list[dict]:
    """The open to-dos whose counterparty is someone (or some company) on this
    invite. Private rows are left out: the brief can be saved to Assets."""
    me = (user_email or "").strip().lower()
    names = _attendee_names(attendees)
    emails = {e for e in names if e != me}
    full_names = {
        n.lower() for e, n in names.items() if e != me and len(n.split()) >= 2
    }
    company_ids = set(records.company_ids) if records else set()
    deal_ids = set(records.deal_ids) if records else set()
    company_names = {
        (c.get("name") or "").lower()
        for c in (records.companies if records else [])
        if c.get("hubspot_company_id")
    }
    domain_set = {d.lower() for d in domains or []}

    out: list[dict] = []
    for p in promises or []:
        if p.get("is_private"):
            continue
        kind = (p.get("counterparty_kind") or "").lower()
        cid = (p.get("counterparty_id") or "").strip()
        label = (p.get("counterparty_label") or "").strip()
        hit = False
        if kind in ("person", "") and cid and cid.lower() in emails:
            hit = True
        elif kind == "person" and not cid and label.lower() in full_names:
            hit = True
        elif kind == "company" and (
            cid in company_ids
            or cid.lower() in domain_set
            or (label and label.lower() in company_names)
        ):
            hit = True
        elif kind == "deal" and cid and cid in deal_ids:
            hit = True
        if hit:
            who = label or names.get(cid.lower()) or cid
            out.append({**p, "who": who})
    return out[:MAX_PROMISES]


def format_promises_block(items: list[dict], *, today: date) -> str:
    if not items:
        return ""
    lines = [
        "## YOUR OPEN TO-DOS FOR THESE PEOPLE (your own To-Do ledger — things you "
        "said you would do and have not marked done)"
    ]
    for p in items:
        line = f"- {p.get('title')}"
        if p.get("who"):
            line += f" — for {p['who']}"
        due = _parse_iso_date(p.get("due_local"))
        if due:
            line += f" — due {_day_label(due)}"
            if due < today:
                line += " (overdue)"
        status = p.get("status")
        if status == "slipping":
            line += " (slipping)"
        elif status == "evidence_found":
            line += f" ({PRODUCT_NAME} saw signs it may be done — not confirmed)"
        lines.append(line)
    return "\n".join(lines)


# ── 4. What was agreed last time ─────────────────────────────────────────────

_MD_HEADING = re.compile(r"^\s*#{1,4}\s+\S")
# Where a Gemini export's notes stop: its "You should review Gemini's notes"
# footer, then (with transcription on) the transcript tab — its "📖 Transcript"
# or "<title> - Transcript" header and its bare "00:12:34" timestamp lines.
_NOTES_END = re.compile(
    r"^\s*(?:you\s+should\s+review\s+gemini|📖\s*transcript)"
    r"|\s-\s+transcript\s*$"
    r"|^\s*\d{2}:\d{2}:\d{2}\s*$",
    re.IGNORECASE,
)
_AGREED_HEADING = re.compile(
    r"^\s*(?P<hash>#{1,4}\s*)?(?P<name>action\s+items(?:\s*&\s*next\s+steps)?|"
    r"(?:suggested\s+)?next\s+steps|(?:meeting\s+)?summary|key\s+decisions|"
    r"decisions)\b(?P<rest>[^\n]*)$",
    re.IGNORECASE,
)


def _is_agreed_heading(line: str) -> bool:
    m = _AGREED_HEADING.match(line)
    if not m:
        return False
    rest = (m.group("rest") or "").strip()
    if m.group("hash"):
        return len(rest) <= 40  # "## Meeting Summary (from Fathom)"
    return rest in ("", ":")  # a bare heading line, not a spoken sentence


def extract_agreed(text: str) -> str:
    """The summary / action-items part of a meeting's notes, or "".

    Fathom transcripts carry "## Action Items & Next Steps (from Fathom)" and
    "## Meeting Summary (from Fathom)" after the body; Gemini notes carry
    "Summary" / "Suggested next steps". Action items come first; each section
    is capped on its own so a long one cannot push the others out. A transcript
    with no notes heading yields "": its opening dialogue is not what was agreed.
    """
    text = (text or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    sections: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        if _is_agreed_heading(lines[i]):
            heading = lines[i].strip().lstrip("#").strip()
            body: list[str] = []
            i += 1
            while i < len(lines) and not (
                _MD_HEADING.match(lines[i])
                or _is_agreed_heading(lines[i])
                or _NOTES_END.search(lines[i])
            ):
                body.append(lines[i])
                i += 1
            content = "\n".join(body).strip()
            if content:
                sections.append(
                    (heading, _clip(content, LAST_MEETING_SECTION_MAX_CHARS))
                )
            continue
        i += 1
    if not sections:
        return ""
    sections.sort(
        key=lambda s: 0 if "action" in s[0].lower() or "next" in s[0].lower() else 1
    )
    joined = "\n\n".join(f"{h}:\n{c}" for h, c in sections)
    return _clip(joined, LAST_MEETING_NOTES_MAX_CHARS)


#: Title words that say nothing about which account a meeting was with. (The
#: company's own name is not listed: it varies by workspace, and since it sits
#: in most titles alike it shifts every candidate's score by about the same.)
_TITLE_NOISE = frozenset(
    {"and", "for", "the", "with", "meeting", "call", "notes", "gemini",
     "fathom", "transcript"}
)  # fmt: skip


def _title_words(title: str | None) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z]+", (title or "").lower())
        if len(w) >= 3 and w not in _TITLE_NOISE
    }


def rank_shared_meetings(
    shared: list[Any],
    *,
    title: str,
    attendees: list[dict],
    user_email: str,
    sizes: dict[str, int],
) -> list[Any]:
    """The shared meetings that fit this invite best, best first.

    Fit = the share of the invite's other people who were in that meeting, plus
    the share of the invite title's words in its title. A meeting with far
    more people than the invite (an L10, an all-hands) is dropped: its action
    items are the whole group's, not what was agreed with these people. Equal
    fits keep ``shared``'s newest-first order. ``sizes`` = doc_id → people.
    """
    me = (user_email or "").strip().lower()
    others = {
        (a.get("email") or "").strip().lower()
        for a in attendees or []
        if isinstance(a, dict) and a.get("resource") is not True
    } - {"", me}
    words = _title_words(title)
    crowd = 2 * (len(others) + 1) + 3

    def fit(m: Any) -> float:
        overlap = {(e or "").lower() for e in (getattr(m, "overlap", None) or [])}
        share = len(overlap & others) / len(others) if others else 0.0
        theirs = _title_words(getattr(m, "title", None))
        named = len(words & theirs) / len(words) if words else 0.0
        return share + named

    kept = [m for m in shared if not (others and sizes.get(m.doc_id, 0) > crowd)]
    return sorted(kept, key=fit, reverse=True)


def read_last_meeting_notes(
    user_email: str,
    shared: list[Any],
    *,
    title: str = "",
    attendees: list[dict] | None = None,
    limit: int = LAST_MEETING_NOTES_LIMIT,
) -> list[dict]:
    """Notes from the ``limit`` shared meetings that best fit this invite
    (``rank_shared_meetings``), newest first.

    ``shared`` comes from ``list_shared_meetings`` (already ACL'd on the
    reader's attendance row). The row is re-checked here before any read — the
    reader must hold a ``meeting_participants`` row for the doc, or own it —
    and the text is read through ``TranscriptService`` for that reader.
    """
    from sqlalchemy import func

    from backend.services.transcript_service import (
        TranscriptRecord,
        TranscriptService,
    )
    from rubik_core.database.models import Asset, MeetingParticipant

    reader = (user_email or "").strip().lower()
    candidates = [m for m in (shared or []) if getattr(m, "doc_id", None)]
    if not reader or not candidates:
        return []
    records: list[tuple[Any, Any]] = []
    db = _session()
    try:
        sizes = dict(
            db.query(MeetingParticipant.doc_id, func.count())
            .filter(MeetingParticipant.doc_id.in_([m.doc_id for m in candidates]))
            .group_by(MeetingParticipant.doc_id)
            .all()
        )
        ranked = rank_shared_meetings(
            candidates,
            title=title,
            attendees=attendees or [],
            user_email=reader,
            sizes=sizes,
        )[:limit]
        # Shown newest first, as the block's dates read.
        wanted = [m for m in candidates if m in ranked]
        for m in wanted:
            asset = db.query(Asset).filter(Asset.doc_id == m.doc_id).first()
            if asset is None or asset.status != "approved":
                continue
            attended = (
                db.query(MeetingParticipant.doc_id)
                .filter(
                    MeetingParticipant.doc_id == m.doc_id,
                    MeetingParticipant.participant_email == reader,
                )
                .first()
                is not None
            )
            if not attended and (asset.owner_email or "").lower() != reader:
                continue
            records.append(
                (
                    m,
                    TranscriptRecord(
                        doc_id=asset.doc_id,
                        meeting_id="",
                        owner_email=asset.owner_email,
                        original_filename=asset.original_filename,
                        source_path=asset.source_path,
                        created_at=asset.created_at or datetime.utcnow(),
                        participants=[],
                        is_owner=(asset.owner_email or "").lower() == reader,
                    ),
                )
            )
    finally:
        db.close()

    service = TranscriptService(reader)
    out: list[dict] = []
    for m, rec in records:
        text = service.read_transcript_text(rec, max_bytes=TRANSCRIPT_READ_BYTES)
        excerpt = extract_agreed(text)
        if excerpt:
            out.append(
                {
                    "doc_id": rec.doc_id,
                    "title": m.title or rec.original_filename or rec.doc_id,
                    "when": m.when,
                    "text": excerpt,
                }
            )
    return out


def format_last_meeting_block(notes: list[dict]) -> str:
    if not notes:
        return ""
    lines = [
        "## WHAT WAS AGREED LAST TIME (notes from your earlier meetings with these "
        "attendees — historical, dated)"
    ]
    for n in notes:
        when = n.get("when")
        when_txt = when.strftime("%A, %B %d, %Y") if when else "date unknown"
        lines.append(f'### {when_txt} — "{n.get("title")}"')
        lines.append(n.get("text") or "")
    return "\n".join(lines)


# ── 5. Team availability ─────────────────────────────────────────────────────


def reader_today(user_email: str) -> tuple[date, str | None]:
    """The reader's local date and timezone name (UTC date on failure)."""
    try:
        from rubik_core.priorities.temporal import resolve_user_now

        now = resolve_user_now(user_email)
        return now.today, now.tz_name
    except Exception:  # noqa: BLE001
        return datetime.utcnow().date(), None


def meeting_local_date(start: Any, tz_name: str | None) -> date | None:
    text = str(start or "").strip()
    if not text:
        return None
    if len(text) == 10:
        return _parse_iso_date(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _parse_iso_date(text)
    if dt.tzinfo is not None and tz_name:
        try:
            from zoneinfo import ZoneInfo

            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001
            pass
    return dt.date()


def fetch_time_off(
    emails: list[str], *, today: date, until: date | None = None
) -> list[dict]:
    """Cached OOO periods for these colleagues overlapping [today, until]."""
    if not emails:
        return []
    from sqlalchemy import func

    from rubik_core.database.models import OOOPeriod

    end = until or (today + timedelta(days=AVAILABILITY_HORIZON_DAYS))
    wanted = sorted({e.strip().lower() for e in emails if e})
    db = _session()
    try:
        rows = (
            db.query(OOOPeriod)
            .filter(func.lower(OOOPeriod.user_email).in_(wanted))
            .filter(OOOPeriod.end_date >= today.isoformat())
            .filter(OOOPeriod.start_date <= end.isoformat())
            .order_by(OOOPeriod.start_date)
            .all()
        )
        return [
            {
                "email": (r.user_email or "").lower(),
                "start": r.start_date,
                "end": r.end_date,
                "return_date": r.return_date,
            }
            for r in rows
        ]
    finally:
        db.close()


def time_off_for_meeting(emails: list[str], user_email: str, start: Any) -> list[dict]:
    """Time off for these colleagues from the reader's today through 14 days out,
    stretched to the meeting's own date when it is further away."""
    if not emails:
        return []
    today, tz_name = reader_today(user_email)
    until = today + timedelta(days=AVAILABILITY_HORIZON_DAYS)
    day = meeting_local_date(start, tz_name)
    if day and day > until:
        until = day
    return fetch_time_off(emails, today=today, until=until)


def _range_label(start: date, end: date) -> str:
    if start == end:
        return _short_date(start)
    if start.month == end.month and start.year == end.year:
        return f"{start:%b} {start.day}–{end.day}"
    return f"{_short_date(start)}–{_short_date(end)}"


def format_availability_block(
    periods: list[dict], *, names: dict[str, str], meeting_day: date | None
) -> str:
    lines: list[str] = []
    for p in periods or []:
        start, end = _parse_iso_date(p.get("start")), _parse_iso_date(p.get("end"))
        if not start or not end:
            continue
        who = names.get(p.get("email") or "") or p.get("email")
        line = f"- {who} is out {_range_label(start, end)}"
        back = _parse_iso_date(p.get("return_date"))
        if back:
            line += f", back {_short_date(back)}"
        if meeting_day and start <= meeting_day <= end:
            line += " — that includes the day of this meeting"
        lines.append(line + ".")
    if not lines:
        return ""
    return "\n".join(
        [
            "## TEAM AVAILABILITY (time off on file for your colleagues on this "
            "invite)",
            *lines,
        ]
    )


# ── 6. What Cubie already flagged ────────────────────────────────────────────

_PREP_KINDS = {"prep_debt", "upcoming_prep"}


def flagged_cards(
    bundle: dict | None,
    *,
    event_id: str | None,
    attendees: list[dict],
    user_email: str,
    domains: list[str],
) -> list[dict]:
    """Today's priority cards (the reader's own) touching this meeting's people.

    The "prepare for this meeting" cards are left out — the reader is doing
    exactly that right now.
    """
    if not bundle:
        return []
    me = (user_email or "").strip().lower()
    keys = {e for e in _attendee_names(attendees) if e != me}
    keys |= {d.lower() for d in domains or []}
    eid = str(event_id or "")
    out: list[dict] = []
    for p in bundle.get("priorities") or []:
        if (p.get("priority_kind") or "") in _PREP_KINDS:
            continue
        ids = {
            str(x).lower()
            for x in [p.get("entity_id"), *(p.get("covered_entity_ids") or [])]
            if x
        }
        target = (p.get("target_email") or "").lower()
        if (
            (eid and str(p.get("entity_id") or "") == eid)
            or ids & keys
            or (target and target in keys)
        ):
            out.append(p)
    return out[:MAX_FLAGGED]


#: The prompt heading for today's AI Priorities cards, named the same way
#: wherever the prompt refers back to it.
FLAGGED_HEADING = f"WHAT {PRODUCT_NAME.upper()} ALREADY FLAGGED"


def format_flagged_block(cards: list[dict]) -> str:
    if not cards:
        return ""
    lines = [
        f"## {FLAGGED_HEADING} (today's AI Priorities for you that involve "
        "these people)"
    ]
    for p in cards:
        line = f"- [{p.get('urgency') or 'medium'}] {p.get('title') or ''}".rstrip()
        if p.get("why_it_matters"):
            line += f" — {_clip(p['why_it_matters'], 300)}"
        if p.get("suggested_action"):
            line += f" Suggested: {_clip(p['suggested_action'], 200)}"
        lines.append(line)
    return "\n".join(lines)


# ── Putting it together ──────────────────────────────────────────────────────


def assemble(
    *,
    user_email: str,
    attendees: list[dict],
    event_id: str | None,
    start: Any,
    crm_domains: list[str],
    skipped_domains: list[str],
    matches: dict[str, Any] | None,
    records: AccountRecords | None,
    xrays: dict[str, tuple[dict, dict]] | None,
    promises: list[dict] | None,
    last_meeting_notes: list[dict] | None,
    time_off: list[dict] | None,
    priorities_bundle: dict | None,
) -> dict[str, Any]:
    """The context keys this module owns, from the raw source results."""
    today, tz_name = reader_today(user_email)
    # The card's "as of" is an instant (the client shows it in local time); the
    # dates in the prompt and on the card are the reader's calendar dates.
    as_of = datetime.utcnow().replace(microsecond=0).isoformat()
    tz = _reader_tz(tz_name)
    names = _attendee_names(attendees)
    xrays = xrays or {}

    # The client card. CRM records when HubSpot answered; otherwise (CRM switch
    # off or failed) only the matched companies that have an X-Ray, so the X-Ray
    # still has a home and no deal-less card implies "no deals".
    companies: list[dict] = []
    if records is not None:
        companies = [
            {
                **c,
                "deal_last_contact": _local_day(c.get("deal_last_contact"), tz),
                "company_last_contact": _local_day(c.get("company_last_contact"), tz),
            }
            for c in records.companies
        ]
    elif matches:
        for dom in crm_domains:
            m = matches.get(dom)
            if (
                m is not None
                and m.company is not None
                and m.company.hubspot_company_id in xrays
            ):
                entry = _blank_company(dom, name=m.company.name)
                entry["hubspot_company_id"] = m.company.hubspot_company_id
                entry["note"] = "Deal details were not loaded for this brief."
                companies.append(entry)
    xray_entries: list[tuple[str, dict, dict]] = []
    for c in companies:
        cid = c.get("hubspot_company_id")
        if cid and cid in xrays:
            card, material = xrays[cid]
            c["xray"] = card
            xray_entries.append(
                (c.get("name") or material.get("company_name"), card, material)
            )

    blocks: dict[str, str] = {}
    if records is not None:
        blocks["crm"] = format_crm_block(
            companies, skipped_domains=skipped_domains, as_of=today.isoformat()
        )
    blocks["xray"] = format_xray_block(xray_entries)
    matched_promises = promises_for_meeting(
        promises or [],
        attendees=attendees,
        user_email=user_email,
        domains=crm_domains,
        records=records,
    )
    blocks["promises"] = format_promises_block(matched_promises, today=today)
    blocks["last_meeting_notes"] = format_last_meeting_block(last_meeting_notes or [])
    blocks["availability"] = format_availability_block(
        time_off or [], names=names, meeting_day=meeting_local_date(start, tz_name)
    )
    flagged = flagged_cards(
        priorities_bundle,
        event_id=event_id,
        attendees=attendees,
        user_email=user_email,
        domains=crm_domains,
    )
    blocks["flagged"] = format_flagged_block(flagged)

    from rubik_core.meeting_prep.provenance import stamp

    company_ids = set(records.company_ids) if records is not None else set()
    company_ids.update(xrays)
    deal_ids = set(records.deal_ids) if records is not None else set()
    flagged_stamped = not flagged
    if flagged:
        markers = (priorities_bundle or {}).get("_crm_provenance") or []
        flagged_stamped = isinstance(markers, list) and bool(markers)
        for marker in markers:
            if (
                not isinstance(marker, dict)
                or marker.get("workspace") != current_workspace()
                or marker.get("unprovable") is not False
                or not isinstance(marker.get("deal_ids"), list)
                or any(not isinstance(value, str) for value in marker["deal_ids"])
            ):
                flagged_stamped = False
                continue
            deal_ids.update(marker["deal_ids"])

    return {
        "signal_blocks": {k: v for k, v in blocks.items() if v},
        "account_snapshot": (
            {"companies": companies, "as_of": as_of} if companies else None
        ),
        "open_promises": matched_promises,
        "last_meeting_notes": list(last_meeting_notes or []),
        "time_off": list(time_off or []),
        "flagged_priorities": flagged,
        "crm_provenance": stamp(
            company_ids=company_ids,
            deal_ids=deal_ids,
            unprovable=not flagged_stamped,
        ),
        "flagged_provenance_stamped": flagged_stamped,
    }


_TRANSCRIPT_MISS_PREFIXES = (
    "no ",
    "you have no",
)


def email_threads_in_block(block: str) -> int:
    """How many threads ``home._format_email_threads_for_prompt`` put in its
    block: each starts "[date] From:" and they are joined by a "---" rule;
    "(none found)" carries none."""
    return sum(1 for part in (block or "").split("\n\n---\n\n") if part[:1] == "[")


def compute_signals_used(ctx: dict[str, Any]) -> list[dict]:
    """The "Built from" strip: every source that actually contributed."""
    out: list[dict] = []

    def add(key: str, label: str, detail: str) -> None:
        out.append({"key": key, "label": label, "detail": detail})

    blocks = ctx.get("signal_blocks") or {}
    snapshot = ctx.get("account_snapshot") or {}
    companies = snapshot.get("companies") or []
    if blocks.get("crm"):
        known = [c for c in companies if c.get("hubspot_company_id")]
        # A company whose deals could not be read has no deal count, not zero.
        read = [c for c in known if c.get("won_deals_count") is not None]
        unread = len(known) - len(read)
        deals = _plural(sum(len(c.get("open_deals") or []) for c in read), "open deal")
        if not read:
            deals = "deals not read"
        elif unread:
            deals += f" (deals not read for {_plural(unread, 'company', 'companies')})"
        if len(known) == 1:
            detail = f"{known[0].get('name')} · {deals}"
        elif known:
            detail = f"{_plural(len(known), 'company', 'companies')} · {deals}"
        else:
            detail = "no confirmed company record"
        add("hubspot", "HubSpot", detail)
    xray_cards = [c for c in companies if c.get("xray")]
    if blocks.get("xray") and xray_cards:
        detail = (
            f"{xray_cards[0].get('name')} v{xray_cards[0]['xray'].get('version')}"
            if len(xray_cards) == 1
            else _plural(len(xray_cards), "account")
        )
        add("xray", "Customer X-Ray", detail)
    # The threads the prompt carries (``email_threads_in_block``), not every
    # thread fetched: the formatter stops at its size cap.
    threads = int(ctx.get("email_threads_used") or 0)
    if threads:
        add("gmail", "Gmail", _plural(threads, "thread"))
    report = (ctx.get("past_meetings_report") or "").strip()
    if report and not report.lower().startswith(_TRANSCRIPT_MISS_PREFIXES):
        add(
            "transcripts",
            "Meeting transcripts",
            _plural(report.count("\n\n---\n\n") + 1, "excerpt"),
        )
    prior = ctx.get("prior_meetings") or []
    if prior:
        add(
            "attendance",
            "Attendance records",
            _plural(len(prior), "shared meeting") + " on record",
        )
    notes = ctx.get("last_meeting_notes") or []
    if blocks.get("last_meeting_notes") and notes:
        add(
            "last_meeting_notes",
            "Last meeting notes",
            "notes from " + _plural(len(notes), "meeting"),
        )
    promises = ctx.get("open_promises") or []
    if blocks.get("promises") and promises:
        add("promises", "My To-Dos", _plural(len(promises), "open to-do"))
    drive = ctx.get("drive_context") or ""
    docs = len(re.findall(r"^(?:Linked )?Doc: ", drive, flags=re.MULTILINE))
    if docs:
        add("drive", "Google Drive", _plural(docs, "linked doc"))
    intel = [
        i
        for i in (ctx.get("attendee_intel") or [])
        if isinstance(i, dict) and not i.get("error")
    ]
    if intel:
        add(
            "attendee_research",
            "Attendee research",
            _plural(len(intel), "person", "people") + " researched",
        )
    off = ctx.get("time_off") or []
    if blocks.get("availability") and off:
        people = {p.get("email") for p in off}
        add(
            "availability",
            "Time off",
            _plural(len(people), "colleague") + " with time off",
        )
    flagged = ctx.get("flagged_priorities") or []
    if blocks.get("flagged") and flagged:
        add("flagged", "AI Priorities", _plural(len(flagged), "priority card"))
    return out


# ── Prompt pieces (both personas) ────────────────────────────────────────────

_GROUNDING_RULES: dict[str, str] = {
    "crm": (
        "\n- CRM ACCOUNT RECORD is HubSpot's live record and is authoritative for "
        "deal names, stages, amounts, close dates, won history and last-contact "
        "dates: copy them exactly, never estimate or round them, and prefer them "
        "over anything a transcript or email implies. Its two last-contact dates "
        "(on the deals, on the company record) are different facts; when you cite "
        "one, say which. A domain it marks as shared by several companies, or "
        "with no company, has NO confirmed account: do not pick one and do not "
        "infer a relationship from it."
    ),
    "xray": (
        f"\n- CUSTOMER X-RAY ON FILE is an earlier {PRODUCT_NAME} analysis of the account, "
        "dated. Treat it as a prior read, not new evidence: attribute anything you "
        "take from it to that X-Ray and its date, never present its churn score or "
        "growth plays as live facts, and let the CRM record or newer emails win "
        "where they differ."
    ),
    "last_meeting_notes": (
        "\n- WHAT WAS AGREED LAST TIME is taken from the notes of earlier meetings "
        "with these attendees. It is historical: say when each item was agreed, "
        "and never claim an item is done, or still open, unless a newer source "
        "shows it."
    ),
    "promises": (
        "\n- YOUR OPEN TO-DOS FOR THESE PEOPLE is the reader's own To-Do ledger: "
        "each line is something they committed to and have not marked done. "
        "Present them as still owed; never mark one complete and never add an "
        "obligation that is not listed there or in the sources."
    ),
    "availability": (
        "\n- TEAM AVAILABILITY is time off on file for colleagues on the invite "
        "and is a fact: do not plan work, follow-ups or hand-offs for someone "
        "inside the dates they are out."
    ),
    "flagged": (
        f"\n- {FLAGGED_HEADING} lists today's AI Priorities cards that "
        "involve these people. Use them as reminders of open items; their wording "
        f"is {PRODUCT_NAME}'s own earlier summary, not something the attendees said."
    ),
}


def research_section(blocks: dict[str, str]) -> str:
    """The blocks for RESEARCH DATA / CONTEXTUAL DATA, or "" (no trailing text)."""
    present = [blocks[k] for k in BLOCK_ORDER if blocks.get(k)]
    return ("\n\n".join(present) + "\n") if present else ""


def grounding_rules(blocks: dict[str, str]) -> str:
    return "".join(_GROUNDING_RULES[k] for k in BLOCK_ORDER if blocks.get(k))


def prompt_parts(
    blocks: dict[str, str] | None, *, battle_brief: bool
) -> dict[str, str]:
    """Every string the brief prompt gains from the signal blocks.

    Each value is "" unless the block it points at is present, so a run with
    none of the new data yields a prompt byte-identical to before.
    """
    blocks = {k: v for k, v in (blocks or {}).items() if v}
    has = blocks.get
    owe_ea = (
        " Also include every item in YOUR OPEN TO-DOS FOR THESE PEOPLE, with its "
        "due date."
        if has("promises")
        else ""
    )
    owe_battle = (
        "\n- open_items_you_owe: A JSON ARRAY of what the reader still owes these "
        "attendees, drawn ONLY from YOUR OPEN TO-DOS FOR THESE PEOPLE (keep each "
        'due date) and from email threads above marked "you have not replied". '
        "Never invent an obligation."
        if has("promises")
        else ""
    )
    return {
        "research": research_section(blocks),
        "grounding": grounding_rules(blocks),
        "client_profile": (
            " Take every deal stage, amount, close date and last-contact date from "
            "CRM ACCOUNT RECORD exactly as written there."
            if has("crm")
            else ""
        ),
        "past_meeting_context": (
            " Start from WHAT WAS AGREED LAST TIME: what was agreed and on which "
            "date, then whether later sources show it moved."
            if has("last_meeting_notes")
            else ""
        ),
        "open_items": owe_battle if battle_brief else owe_ea,
        "next_steps": (
            " Where TEAM AVAILABILITY shows a colleague out, do not give them a "
            "step that falls inside their time off."
            if has("availability")
            else ""
        ),
        "talking_points": (
            f" Where {FLAGGED_HEADING} lists an open item with these "
            "people, make sure it is covered."
            if has("flagged")
            else ""
        ),
    }


def context_notes(blocks: dict[str, str] | None) -> str:
    """Deep Prep: the blocks plus how to read them, or "" when there are none."""
    blocks = {k: v for k, v in (blocks or {}).items() if v}
    present = [blocks[k] for k in BLOCK_ORDER if blocks.get(k)]
    if not present:
        return ""
    return (
        "\n\n".join(present)
        + "\n\nHOW TO READ THE SOURCES ABOVE:"
        + grounding_rules(blocks)
    )
