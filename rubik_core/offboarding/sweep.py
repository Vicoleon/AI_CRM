"""The exit sweep — inventory a departed person's open work and write the dossier.

Runs as a cubie-worker task (``backend/jobs/offboard_sweep_job.py``) right
after an admin offboards someone. The users row is already gone; every source
here reads what survives it:

1. **EOS** — the departed person's ``eos_people`` row outlives the roster:
   L10s they manage, pending To-Dos, open Rocks + incomplete milestones,
   scorecard measurables they enter. Same queries as the PTO handoff brief
   (:mod:`rubik_core.availability.handoff`) but with NO deadline window —
   they are not coming back, so everything open needs an owner.
2. **HubSpot** — open/at-risk deals they own, via the same signal source the
   priorities pipeline uses.
3. **Google last pass** (best-effort) — while the Workspace account is still
   impersonable, one more Gmail/Calendar/Drive fetch so the LLM can extract
   open loops that never made it into a bundle. A suspended account raises
   ``ChannelAuthError`` → the sweep marks itself ``degraded`` and says so in
   the dossier; this is why offboarding should run BEFORE IT suspends.

Separate LLM calls synthesize the dossier narrative and select a handful of
Google signal commitments. Stored item wording comes from the actual signal. Deterministic
fallback throughout: with no LLM the inventory still ships with a template
narrative — the checklist is the deliverable, the prose is garnish.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from rubik_core.brand import PRODUCT_NAME
from rubik_core.cost.usage import attribute_to

logger = logging.getLogger(__name__)

#: AI-cost feature key. Registered in ``_FEATURE_LABELS``
#: (backend/routers/admin.py) in the same change that added this module.
COST_FEATURE = "offboard_handoff"

_MAX_GOOGLE_SIGNALS = 40  # prompt budget for the last-pass extraction
_MAX_LLM_EXTRA_ITEMS = 8


@attribute_to(COST_FEATURE)
def run_offboard_sweep(record_id: str) -> None:
    """Inventory + synthesize + persist for one offboard record.

    Owns its session and its failure handling: any crash marks the record
    ``failed`` with the trail so the Handoffs page never shows a silent
    forever-``pending``.
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.offboarding import store

    audit: list[str] = []
    with SessionLocal() as db:
        record = store.get_record(db, record_id)
        if record is None:
            logger.error("[offboard] sweep: no record %s", record_id)
            return
        departed = record.user_email
        audit.append(f"sweep start for {departed} → successor {record.successor_email}")

        try:
            items: list[dict] = []
            items += _inventory_eos(db, departed, audit)
            items += _inventory_hubspot(departed, audit)
            # Cached priority prose can mix Google and CRM facts regardless of
            # its source tag. Do not turn it into inheritable checklist items.
            audit.append("history → cached priorities skipped; origin unprovable")

            google_signals, google_ok = _google_last_pass(departed, audit)

            narrative, extra_items = _synthesize(
                record, items, google_signals, google_ok, audit
            )
            items += extra_items

            created = store.add_items(db, record, items)
            audit.append(f"persisted {len(created)} item(s)")

            record.sweep_status = "ok" if google_ok else "degraded"
            record.dossier_md = _render_dossier(record, created, narrative, google_ok)
            record.dossier_meta = {
                "item_count": len(created),
                "google_last_pass": google_ok,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            record.sweep_detail = audit
            record.status = "active" if created else "closed"
            db.commit()
            logger.info(
                "[offboard] sweep done for %s: %d items, google_ok=%s",
                departed,
                len(created),
                google_ok,
            )
        except Exception:
            logger.exception("[offboard] sweep failed for %s", departed)
            db.rollback()
            audit.append("sweep crashed — see service logs")
            record = store.get_record(db, record_id)
            if record is not None:
                record.sweep_status = "failed"
                record.sweep_detail = audit
                db.commit()


# ── Inventories ──────────────────────────────────────────────────────────────


def _inventory_eos(db, departed: str, audit: list[str]) -> list[dict]:
    """Everything open the departed person owns in EOS — no deadline window."""
    from rubik_core.offboarding.store import eos_enabled

    items: list[dict] = []
    if not eos_enabled():
        audit.append("eos → workspace module off, skipped")
        return items
    try:
        from backend.routers.eos._authz import person_by_email
        from rubik_core.database.eos import (
            L10Meeting,
            Rock,
            RockStatus,
            ScorecardMetric,
            Todo,
            TodoStatus,
        )
    except Exception:  # noqa: BLE001 — EOS module optional in some rigs
        audit.append("eos → module unavailable, skipped")
        return items

    me = person_by_email(db, departed)
    if me is None:
        audit.append("eos → no eos_people identity, nothing owned")
        return items

    try:
        meetings = (
            db.query(L10Meeting)
            .filter(L10Meeting.is_deleted.is_(False), L10Meeting.manager_id == me.id)
            .all()
        )
        for m in meetings:
            items.append(
                {
                    "kind": "l10",
                    "label": m.name,
                    "detail": (
                        "They were the manager of this L10 — nobody can start it "
                        "or drive the agenda until the manager role is re-pointed "
                        "(Meetings page → the meeting's settings)."
                    ),
                    "urgency": "high",
                    "entity_kind": "l10_meeting",
                    "entity_id": str(m.id),
                }
            )
    except Exception:  # noqa: BLE001 — one source failing must not drop the rest
        logger.warning("[offboard] L10 scan failed for %s", departed, exc_info=True)

    try:
        todos = (
            db.query(Todo)
            .filter(
                Todo.is_deleted.is_(False),
                Todo.status == TodoStatus.PENDING,
                Todo.owner_id == me.id,
            )
            .order_by(Todo.due_date.asc().nullslast())
            .limit(24)
            .all()
        )
        for t in todos:
            d = _as_date(t.due_date)
            items.append(
                {
                    "kind": "todo",
                    "label": t.description or "(untitled to-do)",
                    "detail": "Open EOS to-do they owned"
                    + (f", due {d.isoformat()}." if d else "."),
                    "due": d.isoformat() if d else None,
                    "urgency": "high"
                    if d and d <= date.today() + timedelta(days=7)
                    else "medium",
                    "entity_kind": "eos_todo",
                    "entity_id": str(t.id),
                }
            )
    except Exception:  # noqa: BLE001
        logger.warning("[offboard] To-Do scan failed for %s", departed, exc_info=True)

    try:
        rocks = (
            db.query(Rock)
            .filter(
                Rock.is_deleted.is_(False),
                Rock.owner_id == me.id,
                Rock.status != RockStatus.DONE,
            )
            .limit(12)
            .all()
        )
        rock_ids = []
        for r in rocks:
            rock_ids.append(r.id)
            items.append(
                {
                    "kind": "rock",
                    "label": r.description or "(untitled rock)",
                    "detail": (
                        "Open Rock they owned — needs a new owner or an explicit "
                        "call to drop it."
                    ),
                    "urgency": "high"
                    if _enum_value(r.status) == RockStatus.OFF_TRACK.value
                    else "medium",
                    "entity_kind": "eos_rock",
                    "entity_id": str(r.id),
                }
            )
        if rock_ids:
            from rubik_core.database.eos.rocks import RockMilestone

            milestones = (
                db.query(RockMilestone)
                .filter(
                    RockMilestone.is_deleted.is_(False),
                    RockMilestone.rock_id.in_(rock_ids),
                )
                .limit(24)
                .all()
            )
            for ms in milestones:
                if bool(getattr(ms, "completed", False)):
                    continue
                d = _as_date(getattr(ms, "due_date", None))
                items.append(
                    {
                        "kind": "milestone",
                        "label": getattr(ms, "description", "")
                        or "(untitled milestone)",
                        "detail": "Incomplete milestone on one of their Rocks"
                        + (f", due {d.isoformat()}." if d else "."),
                        "due": d.isoformat() if d else None,
                        "urgency": "medium",
                        "entity_kind": "eos_milestone",
                        "entity_id": str(ms.id),
                    }
                )
    except Exception:  # noqa: BLE001
        logger.warning("[offboard] Rock scan failed for %s", departed, exc_info=True)

    try:
        metrics = (
            db.query(ScorecardMetric)
            .filter(
                ScorecardMetric.is_deleted.is_(False), ScorecardMetric.owner_id == me.id
            )
            .limit(12)
            .all()
        )
        for sm in metrics:
            items.append(
                {
                    "kind": "scorecard",
                    "label": getattr(sm, "name", "") or "(untitled measurable)",
                    "detail": (
                        "They owned this scorecard measurable — someone must take "
                        "over entering the weekly number."
                    ),
                    "urgency": "medium",
                    "entity_kind": "eos_metric",
                    "entity_id": str(sm.id),
                }
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[offboard] scorecard scan failed for %s", departed, exc_info=True
        )

    audit.append(f"eos → {len(items)} open item(s)")
    return items


def _inventory_hubspot(departed: str, audit: list[str]) -> list[dict]:
    """Open/at-risk deals the departed person owns, one item per deal."""
    try:
        from backend.signals.hubspot import HubspotSignalSource
    except Exception:  # noqa: BLE001 — optional module
        audit.append("hubspot → module unavailable, skipped")
        return []

    since = datetime.now(timezone.utc) - timedelta(days=45)
    try:
        signals = HubspotSignalSource().fetch(departed, since=since) or []
    except Exception as exc:  # noqa: BLE001
        audit.append(f"hubspot → fetch failed ({type(exc).__name__}), skipped")
        return []

    by_deal: dict[str, dict] = {}
    for sig in signals:
        refs = getattr(sig, "entity_refs", {}) or {}
        payload = getattr(sig, "payload", {}) or {}
        deal_id = refs.get("deal_id") or payload.get("deal_id")
        if not deal_id:
            continue
        name = (
            payload.get("deal_name")
            or refs.get("company_name")
            or payload.get("company_name")
            or f"HubSpot deal {deal_id}"
        )
        link = payload.get("hubspot_url") or refs.get("hubspot_url")
        entry = by_deal.setdefault(
            str(deal_id),
            {
                "kind": "deal",
                "label": str(name)[:200],
                "detail": "",
                "urgency": "medium",
                "entity_kind": "deal",
                "entity_id": str(deal_id),
                "source_links": [link] if link else [],
                "_flags": [],
            },
        )
        flag = getattr(sig, "signal_type", "") or ""
        if flag and flag not in entry["_flags"]:
            entry["_flags"].append(flag)
        if getattr(sig, "severity_hint", "medium") == "high":
            entry["urgency"] = "high"

    items = []
    for entry in by_deal.values():
        flags = entry.pop("_flags")
        entry["detail"] = "Open deal they owned in HubSpot" + (
            f" (flags: {', '.join(flags)})." if flags else "."
        )
        items.append(entry)
    audit.append(f"hubspot → {len(items)} owned deal(s)")
    return items


def _google_last_pass(departed: str, audit: list[str]) -> tuple[list[dict], bool]:
    """One final Gmail/Calendar/Drive fetch while impersonation still works.

    Returns ``(signals, ok)``. ``ok=False`` means the account is no longer
    impersonable (or Workspace auth is not configured) — the dossier reports
    the sweep as degraded and the LLM only sees the DB-side inventory.
    """
    try:
        from rubik_core.priorities.agents._google_auth import ChannelAuthError
        from rubik_core.priorities.agents.calendar_agent import CalendarChannelAgent
        from rubik_core.priorities.agents.drive_agent import DriveChannelAgent
        from rubik_core.priorities.agents.gmail_agent import GmailChannelAgent
        from rubik_core.priorities.config import PrioritiesConfig
        from rubik_core.priorities.temporal import resolve_user_now
    except Exception:  # noqa: BLE001
        audit.append("google → priorities agents unavailable, skipped")
        return [], False

    config = PrioritiesConfig()
    try:
        temporal = resolve_user_now(user_email=departed, lookahead_hours=24 * 14)
    except Exception:  # noqa: BLE001
        temporal = None

    signals: list[dict] = []
    ok = True
    for agent_cls, channel in (
        (GmailChannelAgent, "gmail"),
        (CalendarChannelAgent, "calendar"),
        (DriveChannelAgent, "drive"),
    ):
        try:
            agent = agent_cls(user_email=departed, config=config, temporal=temporal)
            # A successor may inherit work commitments, never the departed
            # person's connected personal or second-work Gmail account.
            raw = (
                agent.fetch_signals(include_connected_accounts=False)
                if channel == "gmail"
                else agent.fetch_signals()
            )
            parsed = agent.parse_signals(raw)
            for s in parsed[: _MAX_GOOGLE_SIGNALS // 3]:
                signals.append(
                    {
                        "channel": channel,
                        "title": s.title,
                        "snippet": (s.snippet or "")[:400],
                        "link": s.link,
                        "entity_kind": s.entity_kind.value if s.entity_kind else None,
                        "entity_id": s.entity_id,
                    }
                )
            audit.append(f"google/{channel} → {len(parsed)} signal(s)")
        except ChannelAuthError:
            audit.append(
                f"google/{channel} → account not impersonable (already "
                "suspended?) — sweep degraded"
            )
            ok = False
            break
        except Exception as exc:  # noqa: BLE001
            audit.append(f"google/{channel} → fetch failed ({type(exc).__name__})")
    return signals, ok


# ── Synthesis + rendering ────────────────────────────────────────────────────


_GOOGLE_SIGNAL_ENTITY = {"gmail": "thread", "calendar": "event", "drive": "document"}


def _safe_google_signals(signals: list[dict]) -> list[dict]:
    """Keep only native Google references before asking for extra commitments."""
    safe = []
    for raw in signals[:_MAX_GOOGLE_SIGNALS]:
        if not isinstance(raw, dict):
            continue
        channel = raw.get("channel")
        if not isinstance(channel, str):
            continue
        if raw.get("entity_kind") != _GOOGLE_SIGNAL_ENTITY.get(channel):
            continue
        entity_id = raw.get("entity_id")
        title = raw.get("title")
        if not isinstance(entity_id, str) or not entity_id:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        link = raw.get("link")
        if link is not None:
            if not isinstance(link, str):
                continue
            try:
                parsed = urlparse(link)
            except ValueError:
                continue
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not (
                hostname == "google.com" or hostname.endswith(".google.com")
            ):
                continue
        snippet = raw.get("snippet")
        safe.append(
            {
                "channel": channel,
                "title": title.strip()[:160],
                "snippet": snippet.strip()[:400] if isinstance(snippet, str) else "",
                "link": link,
                "entity_kind": raw["entity_kind"],
                "entity_id": entity_id,
            }
        )
    return safe


def _google_item_from_signal(signal: dict, choice: dict) -> dict:
    """The model selects a signal; persisted words come from that signal."""
    urgency = choice.get("urgency")
    if urgency not in {"high", "medium", "low"}:
        urgency = "medium"
    due = choice.get("due")
    if not isinstance(due, str):
        due = None
    else:
        try:
            due = date.fromisoformat(due).isoformat()
        except ValueError:
            due = None
    return {
        "kind": "google_signal_commitment",
        "label": signal["title"],
        "detail": signal["snippet"] or f"Open item from {signal['channel']}",
        "urgency": urgency,
        "due": due,
        "entity_kind": signal["entity_kind"],
        "entity_id": signal["entity_id"],
        "source_links": [signal["link"]] if signal["link"] else [],
    }


def _synthesize(
    record, items: list[dict], google_signals: list[dict], google_ok: bool, audit
) -> tuple[str, list[dict]]:
    """Separate narrative and Google-only extraction calls.

    The extraction call never sees CRM inventory, and the stored item's words
    are copied from a selected Google signal. Fallback is deterministic.
    """
    fallback = _fallback_narrative(record, items, google_ok)
    try:
        from rubik_core.priorities.agents.base import LLMJsonClient
    except Exception:  # noqa: BLE001
        return fallback, []

    from rubik_core.company.profile import company_possessive, get_company_profile

    company = company_possessive(get_company_profile())
    prompt = f"""You are {PRODUCT_NAME}, {company} internal copilot. {record.user_name}
({record.user_email}, {record.department or "unknown department"}) has left the
company. {record.successor_email} inherits their open work.

Below is the inventoried open work (JSON).

INVENTORY:
{json.dumps([{k: v for k, v in i.items() if not k.startswith("_")} for i in items], indent=1, default=str)[:8000]}

Return ONE JSON object:
{{
  "narrative_md": "3-6 sentences of markdown for the successor: the shape of what
    they inherit, what is time-critical, what can wait. Business language, no
    jargon, no flattery. Refer to the departed person by first name."
}}"""
    try:
        client = LLMJsonClient(model_key="gemini-flash", temperature=0.2)
        out = client.invoke_json(prompt, fallback=None, attempts=2)
        narrative = (out.get("narrative_md") or "").strip() or fallback
    except Exception as exc:  # noqa: BLE001 — prose is garnish, ship the list
        audit.append(f"llm → unavailable ({type(exc).__name__}), template narrative")
        narrative = fallback

    safe_signals = []
    for signal in _safe_google_signals(google_signals):
        if len(json.dumps([*safe_signals, signal])) > 8000:
            break
        safe_signals.append(signal)
    if not safe_signals:
        return narrative, []
    extraction_prompt = f"""Identify up to {_MAX_LLM_EXTRA_ITEMS} open commitments
from these Gmail, Calendar, and Drive signals only. No CRM inventory is
provided. Select each item by its zero-based signal_index. Return only JSON:
{{"selected": [{{"signal_index": 0, "urgency": "high|medium|low",
"due": "YYYY-MM-DD or null"}}]}}

GOOGLE SIGNALS:
{json.dumps(safe_signals, indent=1, default=str)}"""
    try:
        client = LLMJsonClient(model_key="gemini-flash", temperature=0.2)
        out = client.invoke_json(extraction_prompt, fallback=None, attempts=2)
        selected = out.get("selected") if isinstance(out, dict) else None
        extra: list[dict] = []
        seen: set[int] = set()
        for choice in selected if isinstance(selected, list) else []:
            if not isinstance(choice, dict):
                continue
            index = choice.get("signal_index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(safe_signals)
                or index in seen
            ):
                continue
            seen.add(index)
            extra.append(_google_item_from_signal(safe_signals[index], choice))
            if len(extra) >= _MAX_LLM_EXTRA_ITEMS:
                break
        audit.append(f"llm/google → {len(extra)} signal-bound commitment(s)")
        return narrative, extra
    except Exception as exc:  # noqa: BLE001 — Google extras are optional
        audit.append(f"llm/google → unavailable ({type(exc).__name__})")
        return narrative, []


def _fallback_narrative(record, items: list[dict], google_ok: bool) -> str:
    n = len(items)
    first = record.user_name.split(" ")[0] if record.user_name else "They"
    base = (
        f"{first} left {n} open item{'s' if n != 1 else ''} that now need an "
        "owner. Work through the checklist below — each entry links to its "
        "source where one exists."
    )
    if not google_ok:
        base += (
            " Their Google account was no longer readable when this sweep ran, "
            "so email/calendar commitments may be missing — check the "
            "Workspace-transferred mailbox and files for anything not listed."
        )
    return base


_KIND_HEADINGS = [
    ("l10", "L10 meetings they managed"),
    ("todo", "Open EOS to-dos"),
    ("rock", "Open Rocks"),
    ("milestone", "Rock milestones"),
    ("scorecard", "Scorecard measurables they entered"),
    ("deal", "HubSpot deals they owned"),
    ("google_signal_commitment", "Commitments in flight"),
]


def _render_dossier(record, items, narrative: str, google_ok: bool) -> str:
    lines = [
        f"### Handoff — {record.user_name}",
        "",
        f"_Offboarded {record.offboarded_at:%Y-%m-%d}; successor: "
        f"{record.successor_email}._",
        "",
        narrative,
        "",
    ]
    if not google_ok:
        lines += [
            "> ⚠️ Their Google account was not readable during this sweep — the "
            "email/calendar side may be incomplete.",
            "",
        ]
    by_kind: dict[str, list] = {}
    for i in items:
        by_kind.setdefault(i.kind, []).append(i)
    for kind, heading in _KIND_HEADINGS:
        group = by_kind.pop(kind, [])
        if not group:
            continue
        lines.append(f"**{heading}**")
        for i in group:
            due = f" _(due {i.due})_" if i.due else ""
            mark = "🔴 " if i.urgency == "high" else ""
            lines.append(f"- {mark}{i.label}{due} — {i.detail}")
            for link in i.source_links or []:
                lines.append(f"  - {link}")
        lines.append("")
    for kind, group in by_kind.items():  # anything with an unexpected kind
        lines.append(f"**{kind}**")
        lines += [f"- {i.label} — {i.detail}" for i in group]
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _enum_value(v) -> str:
    return getattr(v, "value", None) or str(v)
