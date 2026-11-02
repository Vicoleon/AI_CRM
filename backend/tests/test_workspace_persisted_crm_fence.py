"""Cached CRM output loses visibility when its workspace loses pipeline access."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.routers import agents, assets, dam, home
from rubik_core.tenancy.context import current_workspace, use_workspace


def _rec(deal_id: str, *, source: str = "hubspot") -> SimpleNamespace:
    return SimpleNamespace(
        id=deal_id,
        lead_id=deal_id,
        type="deal_priority",
        source_metadata={
            "plugin": "multi_source_priorities",
            "primary_entity_kind": "deal",
        },
        sources=[{"source": source, "entity_refs": {"deal_id": deal_id}}],
    )


def test_cached_recommendations_are_rechecked_for_each_workspace(monkeypatch):
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy import hubspot_config

    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: SimpleNamespace(pipeline_ids=(f"pipeline-{current_workspace()}",)),
    )

    class Portal:
        def batch_read(self, object_type, ids, properties):
            assert object_type == "deals" and properties == ["pipeline"]
            seen.append((current_workspace(), ids))
            owned = {"acme": "deal-a", "bravo": "deal-b"}[current_workspace()]
            return [
                {
                    "id": owned,
                    "properties": {"pipeline": f"pipeline-{current_workspace()}"},
                }
            ]

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", Portal)
    rows = [_rec("deal-a"), _rec("deal-b")]
    with use_workspace("acme"):
        assert [r.id for r in agents._visible_recommendations(rows)] == ["deal-a"]
    with use_workspace("bravo"):
        assert [r.id for r in agents._visible_recommendations(rows)] == ["deal-b"]
    assert seen == [
        ("acme", ["deal-a", "deal-b"]),
        ("bravo", ["deal-a", "deal-b"]),
    ]


def test_empty_or_unavailable_config_hides_crm_without_touching_portal(monkeypatch):
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy import hubspot_config

    called: list[str] = []
    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: SimpleNamespace(pipeline_ids=()),
    )
    monkeypatch.setattr(
        hubspot_api, "HubSpotAPIClient", lambda: called.append("opened")
    )
    non_crm = SimpleNamespace(
        id="calendar",
        lead_id="event-1",
        type="event_priority",
        source_metadata={
            "plugin": "multi_source_priorities",
            "primary_entity_kind": "event",
        },
        sources=[{"source": "calendar", "entity_refs": {}}],
    )
    assert agents._visible_recommendations([_rec("deal-a"), non_crm]) == [non_crm]
    assert called == []

    def unavailable():
        raise RuntimeError("control unavailable")

    monkeypatch.setattr(hubspot_config, "get_hubspot_config", unavailable)
    assert agents._visible_recommendations([_rec("deal-a"), non_crm]) == [non_crm]
    assert called == []


def test_recommendation_action_ownership_proof_does_not_block_event_loop(monkeypatch):
    row = SimpleNamespace(id="rec-1", user_email="rep@acme.example")

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return row

    db = SimpleNamespace(query=lambda _model: Query())
    started = threading.Event()
    release = threading.Event()

    def slow_proof(_rows):
        started.set()
        release.wait(0.5)
        return [row]

    monkeypatch.setattr(agents, "_visible_recommendations", slow_proof)

    async def check():
        task = asyncio.create_task(
            agents._get_owned_rec(
                db, "rec-1", SimpleNamespace(email="rep@acme.example")
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 0.2)
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        assert await task is row

    asyncio.run(check())


def _card(card_id: str, entity_kind: str, entity_id: str, source: str) -> dict:
    return {
        "id": card_id,
        "title": f"Private {card_id}",
        "why_it_matters": f"Sensitive {card_id}",
        "source_channels": [source],
        "source_links": [f"https://example.test/{card_id}"],
        "entity_kind": entity_kind,
        "entity_id": entity_id,
    }


def _v2(priorities: list[dict], *, deal_ids: list[str] | None = None) -> dict:
    return {
        "schema_version": 2,
        "crm_provenance": {
            "workspace": current_workspace(),
            "deal_ids": sorted(deal_ids or []),
            "unprovable": False,
        },
        "generated_at": "2026-10-30T08:00:00Z",
        "headline": "Untrusted stored headline",
        "priorities": priorities,
    }


def test_cached_home_card_hides_foreign_and_unprovable_crm(monkeypatch):
    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda ids: {"deal-a"})
    mixed = _card("mixed", "deal", "deal-a", "hubspot")
    mixed["covered_entity_ids"] = ["deal-a", "deal-b"]
    stored = _v2(
        [
            _card("own", "deal", "deal-a", "hubspot"),
            _card("foreign", "deal", "deal-b", "hubspot"),
            _card("unbound", "company", "company-1", "hubspot"),
            mixed,
            _card("mail", "thread", "thread-1", "gmail"),
        ],
        deal_ids=["deal-a", "deal-b"],
    )
    assert home._bundle_dict_from_metadata(stored) is None

    result = home._bundle_dict_from_metadata(
        _v2(
            [
                _card("own", "deal", "deal-a", "hubspot"),
                _card("mail", "thread", "thread-1", "gmail"),
            ],
            deal_ids=["deal-a"],
        )
    )
    assert [item["id"] for item in result["priorities"]] == ["own", "mail"]
    assert result["generated_count"] == 2
    assert result["headline"] == "Top priority: Private own"
    assert all("Sensitive foreign" not in str(value) for value in result.values())


def test_gmail_tag_cannot_hide_revoked_deal_in_bundle_scope(monkeypatch):
    from rubik_core.priorities.cached_visibility import cached_bundle_visible

    secret = "Foreign deal B closes for a private amount"
    mail = _card("mail", "thread", "thread-1", "gmail")
    mail["title"] = secret
    metadata = _v2([mail], deal_ids=["deal-a", "deal-b"])
    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: {"deal-a"})
    assert not cached_bundle_visible(metadata)
    assert home._bundle_dict_from_metadata(metadata) is None

    broken = _v2([mail], deal_ids=["deal-a"])
    broken["crm_provenance"]["unprovable"] = True
    assert not cached_bundle_visible(broken)
    with use_workspace("other"):
        assert not cached_bundle_visible(metadata)

    def outage(_ids):
        raise RuntimeError("HubSpot unavailable")

    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", outage)
    assert not cached_bundle_visible(metadata)


def test_crm_scope_stamp_uses_all_native_prompt_signals():
    from rubik_core.priorities.models import (
        ChannelDigest,
        ChannelKind,
        ChannelSignal,
        EntityKind,
    )
    from rubik_core.priorities.orchestrator import _crm_prompt_provenance

    signals = [
        ChannelSignal(
            id=deal_id,
            channel=ChannelKind.HUBSPOT,
            title=f"Deal {deal_id}",
            entity_kind=EntityKind.DEAL,
            entity_id=deal_id,
        )
        for deal_id in ("deal-a", "deal-b")
    ]
    stamp = _crm_prompt_provenance(
        [ChannelDigest(channel=ChannelKind.HUBSPOT, signals=signals)]
    )
    assert stamp == {
        "workspace": current_workspace(),
        "deal_ids": ["deal-a", "deal-b"],
        "unprovable": False,
    }
    bad = ChannelDigest(
        channel=ChannelKind.HUBSPOT,
        narrative="A CRM story with no native deal",
        signals=[],
    )
    assert _crm_prompt_provenance([bad])["unprovable"] is True
    assert (
        _crm_prompt_provenance(
            [ChannelDigest(channel=ChannelKind.HUBSPOT, error="portal outage")]
        )["unprovable"]
        is True
    )
    missing_id = ChannelSignal(
        id="hubspot::synthetic",
        channel=ChannelKind.HUBSPOT,
        title="Unbound CRM account",
    )
    assert (
        _crm_prompt_provenance(
            [ChannelDigest(channel=ChannelKind.HUBSPOT, signals=[missing_id])]
        )["unprovable"]
        is True
    )
    inherited = ChannelSignal(
        id="offboard_item:item-1",
        channel=ChannelKind.INHERITED,
        title="Inherited deal",
        entity_kind=EntityKind.OFFBOARD_ITEM,
        entity_id="item-1",
        attrs={"orig_entity_kind": "deal", "orig_entity_id": "deal-b"},
    )
    assert _crm_prompt_provenance(
        [ChannelDigest(channel=ChannelKind.INHERITED, signals=[inherited])]
    )["deal_ids"] == ["deal-b"]


def test_old_feedback_and_resolved_evidence_are_absent_from_decision_prompt():
    from rubik_core.priorities.agents.decision_maker import DecisionMakerAgent
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.priorities.models import ConsolidatedDigest

    row = SimpleNamespace(
        state="resolved",
        last_state_change_at="2026-10-01",
        resolved_evidence="OLD_RESOLVED_EVIDENCE",
        snoozed_until=None,
    )
    prompt = DecisionMakerAgent(PrioritiesConfig.default())._build_prompt(
        ConsolidatedDigest(narrative="Current signals only"),
        max_priorities=5,
        focus=None,
        entity_index={},
        department=None,
        playbook_text="",
        memory={
            ("deal", "foreign-deal", "neglect_debt"): row,
            ("thread", "mail-1", "follow_up_debt"): row,
        },
        wgll_text="OLD_PROPOSAL_COMMENT",
    )
    assert "OLD_PROPOSAL_COMMENT" not in prompt
    assert "OLD_RESOLVED_EVIDENCE" not in prompt
    assert "foreign-deal" not in prompt
    assert "thread:mail-1" in prompt


def test_persist_bundle_refuses_unproved_scope_but_keeps_valid_quiet_day(
    monkeypatch,
):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from rubik_core.database.models import Base, HomeCard
    from rubik_core.priorities import persistence
    from rubik_core.priorities.models import GENERATION_NO_SIGNALS, PrioritiesBundle

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(persistence, "SessionLocal", Session)

    def bundle(deal_ids, unprovable=False):
        return PrioritiesBundle(
            user_email="person@acme.example",
            headline="",
            priorities=[],
            generation_status=GENERATION_NO_SIGNALS,
            crm_provenance={
                "workspace": current_workspace(),
                "deal_ids": deal_ids,
                "unprovable": unprovable,
            },
        )

    try:
        persistence.persist_bundle("person@acme.example", "2026-10-30", bundle([]))
        with Session() as db:
            cards = db.query(HomeCard).all()
            assert len(cards) == 1
            assert cards[0].metadata_json["schema_version"] == 2

        with pytest.raises(ValueError):
            persistence.persist_bundle(
                "person@acme.example", "2026-10-30", bundle([], True)
            )
        monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: set())
        with pytest.raises(ValueError):
            persistence.persist_bundle(
                "person@acme.example", "2026-10-30", bundle(["deal-b"])
            )
        with Session() as db:
            assert db.query(HomeCard).count() == 1
    finally:
        engine.dispose()


def test_cached_headline_with_no_priorities_is_never_replayed():
    result = home._bundle_dict_from_metadata(_v2([]))
    assert result["headline"] == ""
    assert result["priorities"] == []
    assert (
        home._bundle_dict_from_metadata(
            {"headline": "Foreign deal is closing tomorrow", "priorities": []}
        )
        is None
    )


def test_cached_briefing_markdown_uses_live_deal_proof(monkeypatch):
    from backend.routers import offboarding
    from rubik_core.priorities.cached_visibility import render_cached_briefing

    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: set())
    metadata = _v2(
        [
            _card("foreign", "deal", "deal-b", "hubspot"),
            _card("mail", "thread", "thread-1", "gmail"),
        ],
        deal_ids=["deal-b"],
    )
    rendered = render_cached_briefing(metadata, "2026-10-30")
    assert rendered is None

    card = SimpleNamespace(
        id="card-1",
        date="2026-10-30",
        category="daily_briefing",
        content_md="Foreign deal is closing tomorrow",
        metadata_json=metadata,
        created_at=None,
    )
    history = offboarding._history_card_out(card, eos_enabled=True)
    assert history is None

    fresh = _v2([_card("mail", "thread", "thread-1", "gmail")])
    assert "Private mail" in render_cached_briefing(fresh, "2026-10-30")


def test_legacy_briefing_without_structured_provenance_is_hidden(monkeypatch):
    from backend.routers import offboarding
    from rubik_core.priorities.cached_visibility import render_cached_briefing

    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: set())
    card = SimpleNamespace(
        id="old",
        date="2026-10-30",
        category="daily_briefing",
        content_md="Foreign deal details",
        metadata_json=None,
        created_at=None,
    )
    assert offboarding._history_card_out(card, eos_enabled=True) is None
    assert render_cached_briefing(None, card.date) is None


def test_ask_cubie_daily_briefing_omits_revoked_crm(monkeypatch):
    from rubik_core.adk_agent.tools import AgentTools
    from rubik_core.control import repository
    from rubik_core.database import db as database
    from rubik_core.priorities import temporal

    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: set())
    monkeypatch.setattr(temporal, "user_today_str", lambda _email: "2026-10-30")
    monkeypatch.setattr(repository, "module_enabled", lambda _module: True)
    card = SimpleNamespace(
        content_md="Foreign deal details",
        metadata_json=_v2(
            [
                _card("foreign", "deal", "deal-b", "hubspot"),
                _card("mail", "thread", "thread-1", "gmail"),
            ],
            deal_ids=["deal-b"],
        ),
    )
    query = MagicMock()
    query.filter.return_value = query
    query.first.return_value = card
    session = MagicMock()
    session.query.return_value = query
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(database, "SessionLocal", lambda: context)

    report = AgentTools("person@acme.example").get_daily_briefing()["report"]
    assert "No verifiable daily briefing" in report
    assert "Private foreign" not in report
    assert "Foreign deal details" not in report


def test_cached_foreign_priority_id_cannot_be_acted_on(monkeypatch):
    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda ids: set())
    monkeypatch.setattr(home, "user_today_str", lambda _email: "2026-10-30")
    card = SimpleNamespace(
        metadata_json=_v2(
            [_card("p1", "deal", "deal-b", "hubspot")], deal_ids=["deal-b"]
        )
    )
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.all.return_value = [card]
    session = MagicMock()
    session.query.return_value = query
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(home, "SessionLocal", lambda: context)
    assert home._find_priority_card_in_today("person@acme.example", "p1") == (
        None,
        None,
    )


def test_gmail_tagged_priority_action_refuses_revoked_bundle(monkeypatch):
    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: {"deal-a"})
    monkeypatch.setattr(home, "user_today_str", lambda _email: "2026-10-30")
    mail = _card("p1", "thread", "thread-1", "gmail")
    mail["title"] = "Foreign deal B private terms"
    card = SimpleNamespace(metadata_json=_v2([mail], deal_ids=["deal-a", "deal-b"]))
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.all.return_value = [card]
    session = MagicMock()
    session.query.return_value = query
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(home, "SessionLocal", lambda: context)
    assert home._find_priority_card_in_today("person@acme.example", "p1") == (
        None,
        None,
    )


def test_priority_history_and_reopen_hide_revoked_deal(monkeypatch):
    from rubik_core.priorities import memory as priority_memory

    def row(kind: str, entity_id: str):
        return SimpleNamespace(
            entity_kind=kind,
            entity_id=entity_id,
            priority_kind="follow_up_debt",
            state="dismissed",
            snoozed_until=None,
            last_state_change_at="2026-10-30T08:00:00Z",
        )

    monkeypatch.setattr(
        priority_memory,
        "load_inactive",
        lambda _email: [
            row("deal", "revoked-b"),
            row("deal", ""),
            row("thread", "mail-1"),
        ],
    )
    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: set())
    reopen = MagicMock()
    monkeypatch.setattr(priority_memory, "reopen", reopen)
    user = SimpleNamespace(email="person@acme.example")

    history = asyncio.run(home.list_priority_history(user=user))
    assert [item.entity_id for item in history] == ["mail-1"]
    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            home.reopen_priority(
                home._ReopenBody(
                    entity_kind="deal",
                    entity_id="revoked-b",
                    priority_kind="follow_up_debt",
                ),
                user=user,
            )
        )
    assert denied.value.status_code == 404
    reopen.assert_not_called()


def _xray_asset(doc_id: str, company_id: str | None):
    tags = ["cubie-generated", "customer-xray"]
    if company_id:
        tags.append(f"xray-company:{company_id}")
    return SimpleNamespace(
        doc_id=doc_id,
        tags=tags,
        topic="Customer X-Ray",
        original_filename="customer-xray-private.docx",
        owner="owner@acme.example",
        is_admin=False,
        status="approved",
        source_path="/not-read.docx",
    )


def test_xray_asset_metadata_and_bytes_need_live_company_proof(monkeypatch):
    from backend.services import generated_docs
    from rubik_core.xray import fetchers

    asset = _xray_asset("xraydoc-1", "foreign")
    monkeypatch.setattr(assets.dam_registry, "get_asset", lambda _id: asset)
    monkeypatch.setattr(assets, "has_permission", lambda *_a: True)
    monkeypatch.setattr(assets, "has_document_access", lambda *_a: True)
    monkeypatch.setattr(fetchers, "company_available", lambda _id: False)
    load = MagicMock(return_value=b"private bytes")
    monkeypatch.setattr(generated_docs, "load_doc_bytes", load)
    user = SimpleNamespace(email="owner@acme.example", is_admin=True)

    with pytest.raises(HTTPException) as metadata:
        asyncio.run(assets.get_asset("xraydoc-1", user=user))
    assert metadata.value.status_code == 404
    with pytest.raises(HTTPException) as download:
        asyncio.run(assets.download_asset("xraydoc-1", user=user))
    assert download.value.status_code == 404
    load.assert_not_called()

    monkeypatch.setattr(fetchers, "company_available", lambda _id: True)
    assert asyncio.run(assets.get_asset("xraydoc-1", user=user)) is asset


def test_foreign_xray_asset_cannot_be_shared_or_have_tags_rewritten(monkeypatch):
    from backend.services import generated_docs
    from rubik_core.xray import fetchers

    asset = _xray_asset("xraydoc-1", "foreign")
    monkeypatch.setattr(assets.dam_registry, "get_asset", lambda _id: asset)
    monkeypatch.setattr(assets, "has_permission", lambda *_a: True)
    monkeypatch.setattr(assets, "has_document_access", lambda *_a: True)
    monkeypatch.setattr(fetchers, "company_available", lambda _id: False)
    share = MagicMock()
    monkeypatch.setattr(generated_docs, "share_asset", share)
    user = SimpleNamespace(email="owner@acme.example", is_admin=True)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            assets.share_asset(
                "xraydoc-1",
                assets.AssetShareRequest(emails=["other@acme.example"]),
                user=user,
            )
        )
    assert error.value.status_code == 404
    share.assert_not_called()

    monkeypatch.setattr(fetchers, "company_available", lambda _id: True)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            assets.update_asset_status(
                "xraydoc-1", assets.AssetUpdate(tags=[]), user=user
            )
        )
    assert error.value.status_code == 400


def test_legacy_xray_asset_is_hidden_from_admin_list(monkeypatch):
    from rubik_core.xray import fetchers

    legacy = _xray_asset("gendoc-old", None)
    owned = _xray_asset("xraydoc-new", "own")
    ordinary = SimpleNamespace(
        doc_id="note-1", tags=[], topic="Notes", original_filename="note.txt"
    )
    rows = [legacy, owned, ordinary]
    for index, row in enumerate(rows):
        row.created_at = datetime(2026, 10, 30) - timedelta(seconds=index)
    monkeypatch.setattr(assets, "has_permission", lambda *_a: True)

    def listing(*, limit, after=None, **_kwargs):
        start = (
            0
            if after is None
            else next(
                (
                    i + 1
                    for i, row in enumerate(rows)
                    if (row.created_at, row.doc_id) == after
                ),
                len(rows),
            )
        )
        return {"assets": {row.doc_id: row for row in rows[start : start + limit]}}

    monkeypatch.setattr(assets.dam_registry, "list_assets", listing)
    monkeypatch.setattr(fetchers, "visible_company_ids", lambda ids: {"own"})
    user = SimpleNamespace(email="admin@acme.example", is_admin=True)
    result = asyncio.run(
        assets.list_assets(
            limit=10,
            offset=0,
            cursor=None,
            q=None,
            status=None,
            topic=None,
            department=None,
            is_company_wide=None,
            user=user,
        )
    )
    assert set(result["assets"]) == {"xraydoc-new", "note-1"}
    assert result["total"] == 2
    first_page = asyncio.run(
        assets.list_assets(
            limit=1,
            offset=0,
            cursor=None,
            q=None,
            status=None,
            topic=None,
            department=None,
            is_company_wide=None,
            user=user,
        )
    )
    assert list(first_page["assets"]) == ["xraydoc-new"]
    second_page = asyncio.run(
        assets.list_assets(
            limit=1,
            offset=0,
            cursor=first_page["next_cursor"],
            q=None,
            status=None,
            topic=None,
            department=None,
            is_company_wide=None,
            user=user,
        )
    )
    assert list(second_page["assets"]) == ["note-1"]
    assert second_page["total"] == 2


def test_assets_scan_is_bounded_and_cursor_reaches_owned_rows(monkeypatch):
    from rubik_core.xray import fetchers

    # Old X-Ray assets without immutable provenance require no provider call;
    # they exercise the separate 250-row scan cap.
    rows = [_xray_asset(f"xraydoc-{i}", None) for i in range(500)]
    rows.extend(
        [
            _xray_asset("xraydoc-owned", "own"),
            SimpleNamespace(
                doc_id="ordinary",
                tags=[],
                topic="Notes",
                original_filename="note.txt",
            ),
        ]
    )
    for index, row in enumerate(rows):
        row.created_at = datetime(2026, 10, 30) - timedelta(seconds=index)
    calls: list[tuple[int, tuple[datetime, str] | None]] = []
    proofs: list[int] = []

    def listing(*, limit, after=None, **_kwargs):
        assert limit is not None and limit <= assets._LIST_CHUNK_SIZE
        calls.append((limit, after))
        start = (
            0
            if after is None
            else next(
                (
                    i + 1
                    for i, row in enumerate(rows)
                    if (row.created_at, row.doc_id) == after
                ),
                len(rows),
            )
        )
        return {
            "assets": {row.doc_id: row for row in rows[start : start + limit]},
            "total": len(rows),
        }

    def visible_companies(ids):
        proofs.append(len(ids))
        assert len(ids) <= assets._LIST_CHUNK_SIZE
        return {"own"}

    monkeypatch.setattr(assets.dam_registry, "list_assets", listing)
    monkeypatch.setattr(assets, "has_permission", lambda *_args: True)
    monkeypatch.setattr(fetchers, "visible_company_ids", visible_companies)
    user = SimpleNamespace(email="admin@acme.example", is_admin=True)

    def page(offset, cursor):
        return asyncio.run(
            assets.list_assets(
                limit=1,
                offset=offset,
                cursor=cursor,
                q=None,
                status=None,
                topic=None,
                department=None,
                is_company_wide=None,
                user=user,
            )
        )

    first = page(0, None)
    assert first["assets"] == {}
    assert first["total"] == 0 and not first["total_is_exact"]
    assert first["has_more"] and isinstance(first["next_cursor"], str)
    assert len(calls) == 5 and proofs == []

    second = page(0, first["next_cursor"])
    assert second["assets"] == {}
    assert isinstance(second["next_cursor"], str) and second["has_more"]

    second = page(0, second["next_cursor"])
    assert list(second["assets"]) == ["xraydoc-owned"]
    assert second["total"] == 1 and not second["total_is_exact"]
    assert isinstance(second["next_cursor"], str)

    third = page(0, second["next_cursor"])
    assert list(third["assets"]) == ["ordinary"]
    assert third["total"] == 2 and third["total_is_exact"]
    assert not third["has_more"] and third["next_cursor"] is None


def test_assets_cursor_counts_only_rows_passing_document_acl(monkeypatch):
    from rubik_core.auth import access

    rows = [
        SimpleNamespace(
            doc_id=f"note-{i}",
            owner="reader@acme.example" if i >= 120 else "other@acme.example",
            department="docs",
            created_at=datetime(2026, 10, 30) - timedelta(seconds=i),
            tags=[],
            topic="Notes",
            original_filename=f"{i}.txt",
        )
        for i in range(125)
    ]

    def listing(*, limit, after=None, **_kwargs):
        start = (
            0
            if after is None
            else next(
                (
                    i + 1
                    for i, row in enumerate(rows)
                    if (row.created_at, row.doc_id) == after
                ),
                len(rows),
            )
        )
        return {
            "assets": {row.doc_id: row for row in rows[start : start + limit]},
            "total": len(rows),
        }

    monkeypatch.setattr(assets.dam_registry, "list_assets", listing)
    monkeypatch.setattr(assets, "has_permission", lambda *_args: True)
    lookups: list[str] = []
    monkeypatch.setattr(
        access,
        "UserRegistry",
        lambda: SimpleNamespace(
            meta_repo=SimpleNamespace(
                get_user_access_policies=lambda email: lookups.append(email) or []
            )
        ),
    )
    user = SimpleNamespace(
        email="reader@acme.example", is_admin=False, department="other"
    )

    def page(offset, cursor):
        return asyncio.run(
            assets.list_assets(
                limit=3,
                offset=offset,
                cursor=cursor,
                q=None,
                status=None,
                topic=None,
                department=None,
                is_company_wide=None,
                user=user,
            )
        )

    first = page(0, None)
    assert list(first["assets"]) == ["note-120", "note-121", "note-122"]
    assert first["total"] == 3 and first["has_more"]
    second = page(0, first["next_cursor"])
    assert list(second["assets"]) == ["note-123", "note-124"]
    assert second["total"] == 5 and second["total_is_exact"]
    assert lookups == [user.email, user.email]


def test_assets_cap_live_xray_company_proofs_per_request(monkeypatch):
    from rubik_core.xray import fetchers

    rows = [_xray_asset(f"xraydoc-{i}", f"company-{i}") for i in range(45)]
    for index, row in enumerate(rows):
        row.created_at = datetime(2026, 10, 30) - timedelta(seconds=index)
    proof_batches: list[tuple[str, ...]] = []

    def listing(*, limit, after=None, **_kwargs):
        start = (
            0
            if after is None
            else next(
                (
                    i + 1
                    for i, row in enumerate(rows)
                    if (row.created_at, row.doc_id) == after
                ),
                len(rows),
            )
        )
        return {"assets": {row.doc_id: row for row in rows[start : start + limit]}}

    def prove(companies):
        assert len(companies) <= assets._LIST_COMPANY_PROOF_LIMIT
        proof_batches.append(tuple(companies))
        return set(companies)

    monkeypatch.setattr(assets.dam_registry, "list_assets", listing)
    monkeypatch.setattr(assets, "has_permission", lambda *_args: True)
    monkeypatch.setattr(fetchers, "visible_company_ids", prove)
    user = SimpleNamespace(email="admin@acme.example", is_admin=True)

    def page(cursor):
        return asyncio.run(
            assets.list_assets(
                limit=50,
                offset=0,
                cursor=cursor,
                q=None,
                status=None,
                topic=None,
                department=None,
                is_company_wide=None,
                user=user,
            )
        )

    first = page(None)
    assert len(first["assets"]) == 20 and first["has_more"]
    second = page(first["next_cursor"])
    assert len(second["assets"]) == 20 and second["has_more"]
    third = page(second["next_cursor"])
    assert len(third["assets"]) == 5 and not third["has_more"]
    assert third["total"] == 45 and third["total_is_exact"]
    assert [len(batch) for batch in proof_batches] == [20, 20, 5]
    assert set(first["assets"]).isdisjoint(second["assets"])
    assert set(second["assets"]).isdisjoint(third["assets"])


def test_assets_check_local_acl_before_xray_provider_proof(monkeypatch):
    from rubik_core.xray import fetchers

    hidden = _xray_asset("xraydoc-hidden", "foreign")
    hidden.created_at = datetime(2026, 10, 30)
    monkeypatch.setattr(assets, "has_permission", lambda *_args: True)
    monkeypatch.setattr(assets, "load_document_access_policies", lambda _user: [])
    monkeypatch.setattr(
        assets, "has_document_access", lambda _user, _asset, **_kwargs: False
    )
    monkeypatch.setattr(
        assets.dam_registry,
        "list_assets",
        lambda **_kwargs: {"assets": {hidden.doc_id: hidden}, "total": None},
    )
    monkeypatch.setattr(
        fetchers,
        "visible_company_ids",
        lambda _ids: pytest.fail("Hidden assets must not call HubSpot"),
    )
    user = SimpleNamespace(email="reader@acme.example", is_admin=False)
    result = asyncio.run(
        assets.list_assets(
            limit=10,
            offset=0,
            cursor=None,
            q=None,
            status=None,
            topic=None,
            department=None,
            is_company_wide=None,
            user=user,
        )
    )
    assert result["assets"] == {} and result["total"] == 0
    assert result["total_is_exact"]


def test_assets_reject_numeric_offset_even_when_hidden_rows_exist(monkeypatch):
    monkeypatch.setattr(assets, "has_permission", lambda *_args: True)
    monkeypatch.setattr(
        assets.dam_registry,
        "list_assets",
        lambda **_kwargs: pytest.fail("Numeric offset must not scan hidden rows"),
    )
    user = SimpleNamespace(email="admin@acme.example", is_admin=True)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            assets.list_assets(
                limit=50,
                offset=100,
                cursor=None,
                q=None,
                status=None,
                topic=None,
                department=None,
                is_company_wide=None,
                user=user,
            )
        )
    assert error.value.status_code == 400


def test_dam_status_delete_and_replace_refuse_foreign_xray_first(monkeypatch):
    from rubik_core.xray import fetchers

    asset = _xray_asset("xraydoc-1", "foreign")
    monkeypatch.setattr(dam.dam_registry, "get_asset", lambda _id: asset)
    monkeypatch.setattr(dam, "has_document_access", lambda *_a: True)
    monkeypatch.setattr(dam, "has_permission", lambda *_a: True)
    monkeypatch.setattr(dam, "reject_support_background_work", lambda: None)
    monkeypatch.setattr(fetchers, "company_available", lambda _id: False)
    purge = MagicMock()
    monkeypatch.setattr(dam, "delete_document_vectors", purge)
    user = SimpleNamespace(email="owner@acme.example", is_admin=True)

    with pytest.raises(HTTPException) as status:
        asyncio.run(dam.get_processing_status("xraydoc-1", user=user))
    assert status.value.status_code == 404
    with pytest.raises(HTTPException) as deletion:
        asyncio.run(dam.delete_document("xraydoc-1", user=user))
    assert deletion.value.status_code == 404
    purge.assert_not_called()

    with pytest.raises(HTTPException) as replacement:
        asyncio.run(
            dam.upload_document(
                background_tasks=MagicMock(),
                file=SimpleNamespace(filename="replacement.txt"),
                topic="Notes",
                department="Sales",
                description="",
                tags="",
                is_company_wide=False,
                force=False,
                replace_doc_id="xraydoc-1",
                user=user,
                file_processor=MagicMock(),
            )
        )
    assert replacement.value.status_code == 404
