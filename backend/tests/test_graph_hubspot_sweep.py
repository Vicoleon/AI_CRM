"""Filling the graph's CRM half on purpose instead of hoping it fills itself.

Phase 1 left `has_contact` / `has_deal` / `owned_by` to accumulate from X-Ray
runs. Measured against the live graph they were all at zero, which made five of
the twelve acceptance questions unanswerable. This sweep writes them
deliberately.

What these pin:

* The sweep targets the companies we ACTUALLY meet (external company nodes
  already in the graph), and never ourselves.
* `owned_by` finally gets written, because the sweep fetches the owner
  directory once and hands real addresses to the writer — the thing the X-Ray
  path structurally cannot do.
* One unresolvable or exploding company is counted and skipped, never fatal:
  a sweep that dies on record 3 of 59 is worse than useless.
* Re-running is safe (upserts, not inserts).
"""

from __future__ import annotations

from unittest.mock import patch

from rubik_core.graph import keys as gk

INTERNAL = "acme.example"
CLIENT_DOMAIN = "northwind.example"
CONTACT = "carla@northwind.example"
OWNER = "rep@acme.example"


def _wire_sqlite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/s.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)
    return TestSession


def _seed_company_nodes(*domains):
    """Put COMPANY nodes in the graph the way attendance would have."""
    from rubik_core.graph.store import EntityStub, upsert_entities

    upsert_entities([EntityStub(key=gk.company_key(d)) for d in domains])


def _edges(TestSession, rel=None):
    from rubik_core.database.models import GraphEdge

    with TestSession() as s:
        q = s.query(GraphEdge).filter(GraphEdge.invalidated_at.is_(None))
        if rel:
            q = q.filter(GraphEdge.rel == rel)
        return q.all()


def _bundle(domain=CLIENT_DOMAIN, *, contacts=None, deals=None):
    from rubik_core.xray.schemas import CompanyBundle

    b = CompanyBundle(company_id="999")
    b.company_name = "Northwind"
    b.company_domain = domain
    b.contacts = (
        contacts
        if contacts is not None
        else [{"id": "11", "email": CONTACT, "firstname": "Carla", "lastname": "Ruiz"}]
    )
    b.deals = (
        deals
        if deals is not None
        else [{"id": "55", "dealname": "Renewal", "hubspot_owner_id": "7"}]
    )
    return b


def _run_sweep(*, matched, bundles, owner_dir=None, **kwargs):
    """Drive the sweep with HubSpot stubbed out."""
    from rubik_core.graph import hubspot_sweep

    class _Client:
        def owner_directory(self):
            return owner_dir if owner_dir is not None else {"7": {"email": OWNER}}

    def _fetch_bundle(company_id):
        result = bundles[str(company_id)]
        if isinstance(result, Exception):
            raise result
        return result

    # Patched by STRING target: the sweep imports these inside the function, so
    # they resolve at call time (and other suites reload these modules anyway).
    with (
        patch("rubik_core.retrieval.hubspot_api.HubSpotAPIClient", _Client),
        patch(
            "rubik_core.crm_sync.hubspot_context.fetch_companies_for_domains",
            lambda client, domains: matched,
        ),
        patch("rubik_core.xray.fetchers.fetch_company_bundle", _fetch_bundle),
    ):
        return hubspot_sweep.sweep_hubspot_edges(**kwargs)


# ── targeting ────────────────────────────────────────────────────────────────


def test_the_sweep_targets_external_companies_and_never_ourselves(
    tmp_path, monkeypatch
):
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN, "chenoafund.org", INTERNAL)

    from rubik_core.graph.hubspot_sweep import external_company_domains

    domains = external_company_domains(INTERNAL)

    assert INTERNAL not in domains
    assert set(domains) == {CLIENT_DOMAIN, "chenoafund.org"}


def test_the_limit_caps_how_many_companies_are_swept(tmp_path, monkeypatch):
    """HubSpot is rate-limited; an uncapped sweep is how you get throttled."""
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN, "chenoafund.org", "ufcu.org")

    stats = _run_sweep(matched=[], bundles={}, limit=2)

    assert stats["companies_considered"] == 2


# ── what it writes ───────────────────────────────────────────────────────────


def test_the_sweep_writes_contacts_deals_and_owners(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN)

    stats = _run_sweep(
        matched=[{"hubspot_id": "999", "name": "Northwind", "domain": CLIENT_DOMAIN}],
        bundles={"999": _bundle()},
    )

    pairs = {(e.rel, e.src_key, e.dst_key) for e in _edges(TestSession)}
    company = gk.company_key(CLIENT_DOMAIN)
    assert ("has_contact", company, gk.person_key(CONTACT)) in pairs
    assert ("has_deal", company, gk.deal_key("55")) in pairs
    assert ("member_of", gk.person_key(CONTACT), company) in pairs
    assert stats["companies_resolved"] == 1
    assert stats["contacts"] == 1
    assert stats["deals"] == 1


def test_owned_by_is_written_because_the_sweep_resolves_owner_emails(
    tmp_path, monkeypatch
):
    """The gap this sweep exists to close: X-Ray holds an id→NAME map, so it
    writes no owner edge at all. Fetching the owner directory once fixes it."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN)

    _run_sweep(
        matched=[{"hubspot_id": "999", "name": "Northwind", "domain": CLIENT_DOMAIN}],
        bundles={"999": _bundle()},
    )

    owned = _edges(TestSession, rel="owned_by")
    assert [(e.src_key, e.dst_key) for e in owned] == [
        (gk.deal_key("55"), gk.person_key(OWNER))
    ]


def test_no_owner_edge_when_the_directory_is_unavailable(tmp_path, monkeypatch):
    """Better a missing edge than a deal attributed to the wrong person."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN)

    _run_sweep(
        matched=[{"hubspot_id": "999", "name": "Northwind", "domain": CLIENT_DOMAIN}],
        bundles={"999": _bundle()},
        owner_dir={},
    )

    assert _edges(TestSession, rel="owned_by") == []
    assert _edges(TestSession, rel="has_deal") != []


def test_a_blank_crm_domain_falls_back_to_the_domain_we_searched(tmp_path, monkeypatch):
    """Otherwise a company whose HubSpot domain field is empty writes nothing —
    the node is keyed on domain."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN)

    _run_sweep(
        matched=[{"hubspot_id": "999", "name": "Northwind", "domain": CLIENT_DOMAIN}],
        bundles={"999": _bundle(domain="")},
    )

    assert any(e.src_key == gk.company_key(CLIENT_DOMAIN) for e in _edges(TestSession))


# ── resilience ───────────────────────────────────────────────────────────────


def test_one_exploding_company_does_not_stop_the_sweep(tmp_path, monkeypatch):
    """A sweep that dies on record 2 of 3 is worse than useless."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN, "chenoafund.org")

    stats = _run_sweep(
        matched=[
            {"hubspot_id": "1", "name": "Bad", "domain": CLIENT_DOMAIN},
            {"hubspot_id": "2", "name": "Good", "domain": "chenoafund.org"},
        ],
        bundles={
            "1": RuntimeError("hubspot 500"),
            "2": _bundle(domain="chenoafund.org", contacts=[], deals=[]),
        },
    )

    assert stats["companies_failed"] == 1
    assert stats["companies_resolved"] == 1


def test_domains_with_no_hubspot_match_are_counted_not_silently_dropped(
    tmp_path, monkeypatch
):
    """A company we meet but that is not in the CRM is a real finding — it is
    how you notice the CRM is missing an account."""
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN, "notincrm.com")

    stats = _run_sweep(
        matched=[{"hubspot_id": "999", "name": "Northwind", "domain": CLIENT_DOMAIN}],
        bundles={"999": _bundle()},
    )

    assert stats["companies_unmatched"] == 1
    assert "notincrm.com" in stats["unmatched_sample"]


def test_re_running_the_sweep_does_not_duplicate_edges(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN)
    args = dict(
        matched=[{"hubspot_id": "999", "name": "Northwind", "domain": CLIENT_DOMAIN}],
        bundles={"999": _bundle()},
    )

    _run_sweep(**args)
    first = len(_edges(TestSession))
    _run_sweep(**args)

    assert len(_edges(TestSession)) == first


def test_the_write_kill_switch_stops_the_sweep(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes(CLIENT_DOMAIN)

    with patch("rubik_core.config.settings.CUBIE_GRAPH_WRITES_ENABLED", False):
        stats = _run_sweep(
            matched=[{"hubspot_id": "999", "name": "K", "domain": CLIENT_DOMAIN}],
            bundles={"999": _bundle()},
        )

    assert stats["companies_resolved"] == 0
    assert _edges(TestSession) == []


def test_explicit_domains_override_the_graph_derived_target_list(tmp_path, monkeypatch):
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_company_nodes("something-else.com")

    stats = _run_sweep(matched=[], bundles={}, domains=[CLIENT_DOMAIN])

    assert stats["companies_considered"] == 1
