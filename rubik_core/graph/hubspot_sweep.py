"""Fill the graph's CRM half: contacts, deals and deal owners from HubSpot.

Phase 1 assumed these edges would arrive on their own — "lazy-warm via X-Ray /
Deep Prep runs is sufficient at our scale" (spec §5.5). Measured against a
live graph, that assumption was simply wrong: ``has_contact``,
``has_deal`` and ``owned_by`` were all at **zero**, because X-Ray is
Systems-gated and rarely run, so no bundle write-through had ever fired. Every
CRM-dependent acceptance question (§3 Q2, Q5, Q11, and the deal/owner halves of
Q7 and Q12) was unanswerable for want of edges nobody was writing.

This sweep writes them deliberately instead of hoping. It is entirely
deterministic — no LLM calls, so no AI-cost category — and reuses the exact
fetch and write path X-Ray already uses, rather than growing a second way to
turn a HubSpot company into graph edges.

Which companies: the ones we actually deal with. By default that is every
external COMPANY node the graph already knows (i.e. companies whose people have
turned up in meetings), which is where a CRM join is worth having. Pass explicit
domains to sweep something else.

One deliberate improvement over the X-Ray write-through: this fetches the owner
directory ONCE up front, so ``owned_by`` edges get a real owner email. X-Ray
holds only an id→name map and therefore writes no owner edge at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from rubik_core.graph import keys as gk

logger = logging.getLogger(__name__)

# HubSpot is rate-limited and every company costs several calls, so a sweep is
# capped by default. A typical company's whole external set is well under this.
DEFAULT_MAX_COMPANIES = 250


@dataclass
class SweepStats:
    companies_considered: int = 0
    companies_resolved: int = 0
    companies_unmatched: int = 0
    companies_failed: int = 0
    contacts: int = 0
    deals: int = 0
    unmatched_domains: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "companies_considered": self.companies_considered,
            "companies_resolved": self.companies_resolved,
            "companies_unmatched": self.companies_unmatched,
            "companies_failed": self.companies_failed,
            "contacts": self.contacts,
            "deals": self.deals,
            # Truncated: a long tail of one-off domains is not worth a log line
            # each, and the count above already says how many there were.
            "unmatched_sample": self.unmatched_domains[:10],
        }


def _workspace_domain() -> str:
    from rubik_core.company.domain import workspace_domain

    return workspace_domain()


def external_company_domains(internal_domain: str | None = None) -> list[str]:
    """Domains of COMPANY nodes that are not us, oldest-seen first.

    These are companies whose people have actually appeared in meetings, which
    makes them exactly the set where joining CRM data to attendance pays off.
    ``internal_domain`` overrides the workspace's verified domains when set.
    """
    from rubik_core.company.domain import workspace_domains

    internal = (
        {internal_domain.strip().lower()} if internal_domain else workspace_domains()
    )
    from sqlalchemy import select

    from rubik_core.database import db as db_module
    from rubik_core.database.models import GraphEntity

    session = db_module.SessionLocal()
    try:
        rows = session.execute(
            select(GraphEntity.entity_id)
            .where(GraphEntity.kind == gk.KIND_COMPANY)
            .order_by(GraphEntity.first_seen_at)
        )
        return [
            domain for (domain,) in rows if domain and domain.lower() not in internal
        ]
    finally:
        session.close()


def sweep_hubspot_edges(
    *,
    domains: list[str] | None = None,
    limit: int = DEFAULT_MAX_COMPANIES,
    internal_domain: str | None = None,
) -> dict:
    """Resolve each domain to a HubSpot company and write its CRM edges.

    Idempotent: writes go through the store's upsert path, so re-running
    re-observes edges (bumping ``last_seen_at``) rather than duplicating them.
    A company that fails is logged and skipped — one bad record must not cost
    the rest of the sweep.
    """
    from rubik_core.config import settings

    stats = SweepStats()
    if not settings.CUBIE_GRAPH_WRITES_ENABLED:
        logger.warning("graph hubspot sweep: CUBIE_GRAPH_WRITES_ENABLED is off")
        return stats.as_dict()

    from rubik_core.crm_sync.hubspot_context import fetch_companies_for_domains
    from rubik_core.graph.writers import record_company_bundle
    from rubik_core.retrieval.hubspot_api import HubSpotAPIClient
    from rubik_core.xray.fetchers import fetch_company_bundle

    targets = (
        domains if domains is not None else external_company_domains(internal_domain)
    )
    targets = [d for d in targets if gk.company_key(d)][: max(0, limit)]
    stats.companies_considered = len(targets)
    if not targets:
        return stats.as_dict()

    client = HubSpotAPIClient()

    # One call, reused for every company: this is what lets `owned_by` carry a
    # real person instead of being skipped the way the X-Ray path skips it.
    owner_emails: dict[str, str] = {}
    try:
        owner_emails = {
            owner_id: (info or {}).get("email", "")
            for owner_id, info in (client.owner_directory() or {}).items()
        }
    except Exception:  # noqa: BLE001 — owner edges are a bonus, not the point
        logger.warning(
            "graph hubspot sweep: owner directory unavailable", exc_info=True
        )

    matched = fetch_companies_for_domains(client, targets)
    matched_domains = {(m.get("domain") or "").lower() for m in matched}
    for domain in targets:
        if domain.lower() not in matched_domains:
            stats.companies_unmatched += 1
            stats.unmatched_domains.append(domain)

    for record in matched:
        company_id = record.get("hubspot_id")
        if not company_id:
            continue
        try:
            bundle = fetch_company_bundle(str(company_id))
            # The bundle's own domain is HubSpot's spelling; fall back to the
            # domain we searched on so a blank CRM field cannot orphan the write
            # (record_company_bundle keys the company node on the domain).
            domain = bundle.company_domain or record.get("domain") or ""
            record_company_bundle(
                bundle.company_id,
                bundle.company_name or record.get("name"),
                domain,
                bundle.deals,
                bundle.contacts,
                owner_emails=owner_emails,
            )
            stats.companies_resolved += 1
            stats.contacts += len(bundle.contacts or [])
            stats.deals += len(bundle.deals or [])
            logger.info(
                "graph hubspot sweep: %s → %d contacts, %d deals",
                domain,
                len(bundle.contacts or []),
                len(bundle.deals or []),
            )
        except Exception:  # noqa: BLE001 — one bad company must not stop the sweep
            stats.companies_failed += 1
            logger.warning(
                "graph hubspot sweep: failed for company %s", company_id, exc_info=True
            )

    return stats.as_dict()
