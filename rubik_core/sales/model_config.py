"""Admin-managed model routing for the sales features.

Stored in the existing ``workspace_settings`` table under the key
``sales_model_config`` (mirrors the UC-20 AI-Priorities store and the chat
store). Lets an admin re-point any sales AI call **at runtime — no redeploy**.

Why this exists
---------------
Chat and AI Priorities are runtime-tunable so that when a model id 429s or
Google rotates one, an admin flips a dropdown
instead of waiting on a deploy. Every sales feature was left on the other side
of that line — ``proposals`` and ``call_capture`` behind env vars that are set
nowhere, ``xray``/``crm_sync`` behind nothing at all — so the
same outage would have cost a code change and a full deploy per feature.

Per-role rather than one id, because the tiers are a deliberate cost/quality
split: X-Ray's channel extractors are cheap fan-out work (Flash) while its
analyst reasons over everything they produced (Pro). Collapsing that to one
setting would either triple the cost of a Deep run or gut the analysis.

Precedence for each role: **stored admin value → env override → tier default**
from ``config.py``. The env overrides (``PROPOSALS_MODEL``,
``CCS_EXTRACTION_MODEL``) stay honoured so existing deploys behave identically
until an admin actually picks something; an explicit choice in the UI outranks
them, which is the entire point of the store.

Every read goes to the DB — deliberately uncached, exactly like the chat and
priorities stores. A run makes a handful of these (one indexed KV lookup each,
against tens of seconds of model time) and in exchange "takes effect
immediately" stays literally true across every Cloud Run instance.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any

from rubik_core.config import settings
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import WorkspaceSetting

logger = logging.getLogger(__name__)

SETTINGS_KEY = "sales_model_config"
CURRENT_SCHEMA_VERSION = 1

# ── Catalog ──────────────────────────────────────────────────────────────────
# GA-safe servable Vertex ids. Preview ids are deliberately EXCLUDED: they have
# no dedicated per-project quota bucket and 429 under light load, and that quota
# cannot be raised by request (see the comment block in ``config.py``).
#
# Unlike CHAT_MODEL_CATALOG this lists the Pro tier too, because the sales roles
# span both — and because the cost lever an admin most wants in an incident is
# moving a Pro role down to Flash, not just swapping one Flash id for another.
#
# There is exactly ONE Pro id. Re-probed in full on 2026-09-17: every Pro-tier
# 3.x id (3.8/3.7/3.6/3.5/3.1/3-pro) and both "-latest" aliases 404 on Vertex
# global, while made-up negative controls 404 identically and "gemini-2.5-pro"
# answered 200/ON_DEMAND — so this is a real availability limit, not a stale
# list. Do not add a Pro id back without re-probing: a 404 here is a hard
# failure for every role defaulted to the Pro tier.
#
# 2026-09-17 also added gemini-3.7-flash, the one genuinely new servable id in
# that sweep (5/5 on a real workload AND 5/5 grounded; 3.9/4.0-flash and every
# Flash-Lite above 3.5 404, and gemini-3.5-flash 429s, so all stay off).
SALES_MODEL_CATALOG: tuple[str, ...] = (
    "gemini-2.5-pro",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
)

# Ids that can serve a Google-Search-grounded call. The X-Ray web agent is the
# only grounded path in the sales stack and Pro is not one of these today, so
# its dropdown is narrowed rather than letting an admin pick an id that would
# fail every Deep run's web channel.
# gemini-3.7-flash is listed on evidence, not assumption: probed 2026-09-17
# with a search-forcing prompt, it issued real web_search_queries 5/5. A
# trivially-answerable prompt is NOT a valid grounding test here — 3.x answers
# those from parametric memory and emits no grounding metadata, which reads as
# "cannot ground" and is wrong.
GROUNDING_CAPABLE_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-flash-latest",
)


# ── Roles ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SalesModelRole:
    """One tunable model slot, and everything the admin UI needs to render it."""

    key: str
    label: str
    group: str
    #: Business outcome, not implementation — this is the help text an admin reads.
    description: str
    #: Name of the ``config.py`` tier this falls back to, for display.
    tier: str
    #: Resolved fallback when nothing is stored: env override, else the tier.
    default_factory: Any
    #: Narrower choices than SALES_MODEL_CATALOG where the role demands it.
    allowed_models: tuple[str, ...] | None = None
    #: Why ``allowed_models`` is narrower, shown under the dropdown.
    choices_note: str = ""


SALES_MODEL_ROLES: tuple[SalesModelRole, ...] = (
    SalesModelRole(
        key="proposals",
        label="Proposal drafting",
        group="Proposals & calls",
        description=(
            "Writes the proposal a prospect's decision-maker reads. The most "
            "expensive place to be wrong, and the lowest volume — worth the Pro tier."
        ),
        tier="PRO",
        default_factory=lambda: (
            settings.PROPOSALS_MODEL or settings.VERTEX_LLM_MODEL_PRO
        ),
    ),
    SalesModelRole(
        key="call_extraction",
        label="Call wrap-up & scripts",
        group="Proposals & calls",
        description=(
            "Turns a call recording into the wrap-up, the to-dos and the SOW clock. "
            "Errors here quietly become someone's missed commitment."
        ),
        tier="PRO",
        default_factory=lambda: (
            settings.CCS_EXTRACTION_MODEL or settings.VERTEX_LLM_MODEL_PRO
        ),
    ),
    SalesModelRole(
        key="xray_channel",
        label="Channel extractors",
        group="Customer X-Ray",
        description=(
            "Reads one source each (HubSpot, Gmail, meetings, docs) and summarises it. "
            "Cheap fan-out work that runs five times per Deep report."
        ),
        tier="FLASH",
        default_factory=lambda: settings.VERTEX_LLM_MODEL_FLASH,
    ),
    SalesModelRole(
        key="xray_analyst",
        label="Analyst",
        group="Customer X-Ray",
        description=(
            "Reasons across everything the extractors found to produce the actual "
            "account read. This is the report's quality ceiling."
        ),
        tier="PRO",
        default_factory=lambda: settings.VERTEX_LLM_MODEL_PRO,
    ),
    SalesModelRole(
        key="xray_critic",
        label="Critic",
        group="Customer X-Ray",
        description=(
            "Scores the analyst's draft and triggers one rewrite if it falls short. "
            "A grader, not an author — Flash is sufficient."
        ),
        tier="FLASH",
        default_factory=lambda: settings.VERTEX_LLM_MODEL_FLASH,
    ),
    SalesModelRole(
        key="xray_web",
        label="Web research",
        group="Customer X-Ray",
        description=(
            "Searches the open web for news on the account. Requires Google Search "
            "grounding, so the choices here are narrower than the other roles."
        ),
        tier="FLASH",
        default_factory=lambda: settings.VERTEX_LLM_MODEL_FLASH,
        allowed_models=GROUNDING_CAPABLE_MODELS,
        choices_note="needs web-search grounding, so fewer options",
    ),
    SalesModelRole(
        key="xray_fast_synth",
        label="Fast score",
        group="Customer X-Ray",
        description=(
            "The instant relationship score shown on the customer page, before anyone "
            "asks for the Deep report. Runs on every page view."
        ),
        tier="PRO",
        default_factory=lambda: settings.VERTEX_LLM_MODEL_PRO,
    ),
    SalesModelRole(
        key="crm_sync",
        label="CRM Sync extraction",
        group="CRM Sync",
        description=(
            "Proposes the HubSpot field updates a call implies. Every write is "
            "human-approved, so this drafts rather than decides."
        ),
        tier="FLASH",
        default_factory=lambda: settings.VERTEX_LLM_MODEL_FLASH,
    ),
    # Not a sales feature: Deep Prep serves every team. It lives here because
    # this is the per-job model store with validation and an admin page; the
    # registry's name predates it.
    SalesModelRole(
        key="deep_prep",
        label="Deep Prep brief",
        group="Meeting Prep (all teams)",
        description=(
            "The emailed Deep Prep brief: plans what the meeting needs, writes it "
            "from the records, then fact-checks every claim. Three calls per brief, "
            "for every team that uses Deep Prep, not only Sales."
        ),
        tier="PRO",
        default_factory=lambda: settings.VERTEX_LLM_MODEL_PRO,
        # The three models replayed on real briefs. Argon is
        # offered only at high thinking: at its inherited level it thinks 0
        # tokens here, which is not the model being chosen.
        allowed_models=(
            settings.VERTEX_LLM_MODEL_PRO,
            f"{settings.VERTEX_LLM_MODEL_ARGON}@high",
            settings.VERTEX_LLM_MODEL_FLASH,
        ),
        choices_note="the three models tested side by side on real briefs",
    ),
)

ROLE_KEYS: tuple[str, ...] = tuple(role.key for role in SALES_MODEL_ROLES)
_ROLES_BY_KEY: dict[str, SalesModelRole] = {r.key: r for r in SALES_MODEL_ROLES}


def allowed_models_for(role_key: str) -> tuple[str, ...]:
    """The ids a given role may be set to. Unknown roles get the full catalog."""
    role = _ROLES_BY_KEY.get(role_key)
    if role is None:
        return SALES_MODEL_CATALOG
    return role.allowed_models or SALES_MODEL_CATALOG


def default_model_for(role_key: str) -> str:
    """Resolved fallback for a role: env override if set, else its config tier."""
    role = _ROLES_BY_KEY.get(role_key)
    if role is None:
        return settings.VERTEX_LLM_MODEL_FLASH
    return role.default_factory()


# ── DB I/O ───────────────────────────────────────────────────────────────────


def _default_blob() -> dict[str, Any]:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "roles": {
            role.key: {"model_id": default_model_for(role.key)}
            for role in SALES_MODEL_ROLES
        },
    }


def load_settings_blob() -> dict[str, Any]:
    """Read the stored blob, merged onto defaults. Never raises.

    Always fully populated so the admin UI can render every role without
    special-casing a fresh install. A stored role that is no longer in
    :data:`SALES_MODEL_ROLES` (its feature was removed) is dropped: the admin
    page sends back every role it read, and the PUT refuses unknown roles.
    """
    blob = _default_blob()
    try:
        with SessionLocal() as db:
            row = (
                db.query(WorkspaceSetting)
                .filter(WorkspaceSetting.key == SETTINGS_KEY)
                .first()
            )
            if row and isinstance(row.value, dict):
                _deep_merge(blob, row.value)
    except Exception:  # noqa: BLE001 — a DB hiccup must never break a sales run
        logger.exception("Failed to load sales model settings, using defaults")
    roles = blob.get("roles")
    if isinstance(roles, dict):
        blob["roles"] = {k: v for k, v in roles.items() if k in _ROLES_BY_KEY}
    return blob


def save_settings_blob(
    payload: dict[str, Any], *, actor_email: str | None = None
) -> dict[str, Any]:
    """Persist the sales model config under SETTINGS_KEY (upsert)."""
    blob = _default_blob()
    _deep_merge(blob, payload)
    blob["schema_version"] = CURRENT_SCHEMA_VERSION

    with SessionLocal() as db:
        row = (
            db.query(WorkspaceSetting)
            .filter(WorkspaceSetting.key == SETTINGS_KEY)
            .first()
        )
        if row is None:
            row = WorkspaceSetting(key=SETTINGS_KEY, value=blob)
            db.add(row)
        else:
            row.value = blob
        db.commit()

    logger.info("sales model settings updated by %s", actor_email or "(unknown)")
    return blob


def model_for(role_key: str) -> str:
    """The model id a sales role should run on right now.

    This is the function every sales call site goes through. Falls back to the
    role's env override / config tier if nothing is stored, if the stored value
    is blank, or if the DB read fails — so a missing row behaves exactly like
    the hardcoded default did before this store existed.
    """
    blob = load_settings_blob()
    entry = (blob.get("roles") or {}).get(role_key) or {}
    model_id = entry.get("model_id")
    if isinstance(model_id, str) and model_id.strip():
        return model_id.strip()
    return default_model_for(role_key)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """In-place: recursively merge ``overlay`` into ``base``."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
