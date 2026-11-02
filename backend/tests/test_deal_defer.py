"""Defer a deal — the rules, the endpoints, and every surface it touches.

What has to be true (the Oct 9 Sales/HubSpot Open Office commitment):

1. A defer needs a follow-up date (after today, at most 180 days out) and a note.
2. While deferred, the deal is off every list Cubie shows the REP: My Day's
   deal blocks, the daily-rhythm checks (My Day and Ask Cubie), the priorities
   signals and today's already-built card, the cockpit digest and My To-Dos.
3. It comes back on its own on the follow-up date, and Undefer brings it back
   early.
4. The manager views never hide it: the Team Roll-up and the weekly close-date
   roll-up list it with owner, date and note.
5. The HubSpot task is written once, through the wrap-up ledger's idempotency
   key, however many times the rep presses Defer.

Nothing here talks to HubSpot: every client is faked and patched by STRING
target (other suites ``importlib.reload()`` modules mid-run).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.call_capture import CallHubspotWrite
from rubik_core.database.commitments import Commitment, CommitmentStageLag
from rubik_core.database.db import get_db
from rubik_core.database.deal_defers import DealDefer
from rubik_core.database.schemas import UserProfile
from rubik_core.features import clear_cache as clear_feature_cache
from rubik_core.sales import deal_defer as dd

client = TestClient(app)

AE = "parker@acme.example"
OTHER_AE = "reese@acme.example"
DEFERRED = "201"
LIVE = "202"
#: Fixed "today" for every reader, so the dates below never drift.
TODAY = date(2026, 10, 15)


def _profile(email: str = AE, *, department: str = "Systems", admin=False):
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["admin"] if admin else ["user"],
        created_at="2026-10-01T00:00:00",
        updated_at="2026-10-01T00:00:00",
    )


def _as(email: str = AE, **kw) -> UserProfile:
    profile = _profile(email, **kw)
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database import eos  # noqa: F401 — registers the eos_* tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    app.dependency_overrides[get_db] = lambda: s
    clear_feature_cache()
    try:
        yield s
    finally:
        app.dependency_overrides.clear()
        clear_feature_cache()
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    """Every reader's today is TODAY unless a test moves it."""
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    monkeypatch.setattr("rubik_core.sales.deal_defer.reader_today", lambda _e: TODAY)
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)
    config = HubSpotConnectorConfig(pipeline_ids=("default",), portal_id="9100001")
    for target in (
        "backend.routers.cockpit.get_hubspot_config",
        "backend.signals.hubspot.get_hubspot_config",
        "rubik_core.sales.rhythm_checks.get_hubspot_config",
        "rubik_core.retrieval.hubspot_api.get_hubspot_config",
        "rubik_core.retrieval.hubspot_write.get_hubspot_config",
        "rubik_core.tenancy.hubspot_config.get_hubspot_config",
    ):
        monkeypatch.setattr(target, lambda: config)
    monkeypatch.setattr(
        "backend.routers.agents.visible_hubspot_deal_ids", lambda ids: set(ids)
    )


def _defer_row(
    db, deal=DEFERRED, *, owner=AE, days=30, note="Budget opens in Q1."
) -> DealDefer:
    row = DealDefer(
        hubspot_deal_id=deal,
        deal_name=f"Deal {deal}",
        owner_email=owner,
        deferred_by=owner,
        follow_up_on=TODAY + timedelta(days=days),
        note=note,
        created_at=datetime(2026, 10, 15, 14, 0),
        updated_at=datetime(2026, 10, 15, 14, 0),
        hubspot_task_status="written",
    )
    db.add(row)
    db.commit()
    return row


# ── HubSpot fake for the write path ──────────────────────────────────────────


class _Reads:
    def __init__(self, owners):
        self._owners = owners

    def owner_directory(self):
        return self._owners


class FakeWriter:
    """Stands in for ``HubSpotWriteClient``: one deal read, one task create."""

    instances: list[FakeWriter] = []

    def __init__(self, *, owner_email=AE, fail_times=0, gone=False):
        self.owner_email = owner_email
        self.fail_times = fail_times
        self.gone = gone
        self.tasks: list[dict] = []
        self.read_client = _Reads({"o-1": {"email": owner_email, "name": "Parker"}})

    def get_object(self, object_type, object_id, properties):
        from rubik_core.retrieval.hubspot_write import HubSpotWriteError

        if self.gone:
            raise HubSpotWriteError("HubSpot read of deals 1 failed: HTTP 404: gone")
        return {
            "id": object_id,
            "properties": {"dealname": f"Acme {object_id}", "hubspot_owner_id": "o-1"},
        }

    def create_task(self, properties, *, owner_email, deal_id=None, **_kw):
        from rubik_core.retrieval.hubspot_write import HubSpotWriteError

        if self.fail_times:
            self.fail_times -= 1
            raise HubSpotWriteError(
                "HubSpot POST /crm/v3/objects/tasks failed: HTTP 403"
            )
        self.tasks.append(
            {"properties": properties, "owner_email": owner_email, "deal_id": deal_id}
        )
        return {"id": f"task-{len(self.tasks)}"}


@pytest.fixture
def writer(monkeypatch):
    fake = FakeWriter()
    monkeypatch.setattr("backend.routers.deal_defer.HubSpotWriteClient", lambda: fake)
    return fake


def _post_defer(deal=DEFERRED, *, days=30, note="Budget opens in Q1."):
    return client.post(
        f"/api/sales/deals/{deal}/defer",
        json={"follow_up_on": (TODAY + timedelta(days=days)).isoformat(), "note": note},
    )


def test_revoked_workspace_deal_is_hidden_and_cannot_be_changed(
    db, writer, monkeypatch
):
    _as()
    row = _defer_row(db, note="Private deferred deal note")
    monkeypatch.setattr(
        "backend.routers.agents.visible_hubspot_deal_ids", lambda _ids: set()
    )

    listed = client.get("/api/sales/deals/deferred")
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert "Private deferred deal note" not in listed.text
    assert client.post(f"/api/sales/deals/{DEFERRED}/undefer").status_code == 404
    assert _post_defer().status_code == 404
    assert writer.tasks == []
    db.refresh(row)
    assert row.ended_at is None


# ── 1. the rules ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "follow_up, note, message",
    [
        (None, "x", "Pick a follow-up date"),
        (TODAY, "x", "in the future"),
        (TODAY - timedelta(days=1), "x", "in the future"),
        (TODAY + timedelta(days=181), "x", "at most 180 days"),
        (TODAY + timedelta(days=10), "   ", "Add a note"),
        (TODAY + timedelta(days=10), "x" * 1001, "under 1000"),
    ],
)
def test_a_defer_needs_a_future_date_within_180_days_and_a_note(
    follow_up, note, message
):
    with pytest.raises(dd.DeferInvalid, match=message):
        dd.validate(follow_up, note, today=TODAY)


def test_the_last_allowed_day_is_exactly_180_days_out():
    assert dd.validate(TODAY + timedelta(days=180), " ok ", today=TODAY) == "ok"


def test_the_endpoint_refuses_a_bad_request_with_the_rule(db, writer):
    _as()
    assert _post_defer(days=0).status_code == 422
    assert _post_defer(days=200).status_code == 422
    assert _post_defer(note="").status_code == 422
    assert (
        client.post(
            "/api/sales/deals/not-a-deal/defer",
            json={"follow_up_on": "2026-11-01", "note": "x"},
        ).status_code
        == 422
    )
    assert db.query(DealDefer).count() == 0
    assert writer.tasks == []


# ── 5. the HubSpot task is written once ──────────────────────────────────────


def test_defer_writes_one_task_on_the_deal_due_on_the_follow_up_date(db, writer):
    _as()
    res = _post_defer()
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["outcome"] == "created"
    assert body["hubspot_task"]["status"] == "written"
    assert body["defer"]["follow_up_on"] == "2026-11-14"
    assert body["defer"]["deal_name"] == "Acme 201"

    [task] = writer.tasks
    assert task["deal_id"] == DEFERRED and task["owner_email"] == AE
    props = task["properties"]
    # The note is the body; the stage and close date are never in a task.
    assert props["hs_task_body"].startswith("Budget opens in Q1.")
    assert "dealstage" not in props and "closedate" not in props
    due = datetime.utcfromtimestamp(props["hs_timestamp"] / 1000)
    assert due.date() == date(2026, 11, 14)

    row = db.query(DealDefer).one()
    assert row.hubspot_task_status == "written" and row.hubspot_task_id == "task-1"


def test_pressing_defer_twice_writes_the_task_once(db, writer):
    _as()
    first = _post_defer().json()
    second = _post_defer().json()

    assert first["outcome"] == "created"
    assert second["outcome"] == "unchanged"
    assert second["hubspot_task"]["status"] == "already_written"
    assert len(writer.tasks) == 1
    # One open defer, and one row in the wrap-up ledger keyed for it.
    assert db.query(DealDefer).count() == 1
    assert db.query(CallHubspotWrite).count() == 1
    assert db.query(CallHubspotWrite).one().succeeded_at is not None


def test_a_new_date_replaces_the_defer_and_keeps_the_history(db, writer):
    _as()
    _post_defer(days=30)
    res = _post_defer(days=60, note="Pushed to their March board.").json()

    assert res["outcome"] == "replaced"
    assert len(writer.tasks) == 2
    rows = db.query(DealDefer).order_by(DealDefer.created_at).all()
    assert len(rows) == 2
    assert rows[0].ended_reason == "replaced" and rows[0].ended_at is not None
    assert rows[1].ended_at is None and rows[1].note == "Pushed to their March board."


def test_a_failed_task_keeps_the_defer_and_a_retry_writes_it_once(db, writer):
    _as()
    writer.fail_times = 1
    res = _post_defer().json()

    # The deal is deferred either way; the response says the task did not land.
    assert res["hubspot_task"]["status"] == "write_failed"
    assert "write scope" in res["hubspot_task"]["message"]
    assert db.query(DealDefer).one().hubspot_task_status == "failed"
    listed = client.get("/api/sales/deals/deferred").json()["items"]
    assert listed[0]["hubspot_task_status"] == "failed"

    retry = _post_defer().json()
    assert retry["outcome"] == "unchanged"
    assert retry["hubspot_task"]["status"] == "written"
    assert len(writer.tasks) == 1
    # Same key, reclaimed — never a second ledger row for the same defer.
    assert db.query(CallHubspotWrite).count() == 1
    assert db.query(DealDefer).one().hubspot_task_status == "written"


# ── who may defer ────────────────────────────────────────────────────────────


def test_only_the_deals_owner_can_defer_it(db, monkeypatch):
    fake = FakeWriter(owner_email=OTHER_AE)
    monkeypatch.setattr("backend.routers.deal_defer.HubSpotWriteClient", lambda: fake)
    _as()
    res = _post_defer()
    assert res.status_code == 403
    assert db.query(DealDefer).count() == 0 and fake.tasks == []


def test_an_ae_holding_a_commitment_on_the_deal_may_defer_it(db, monkeypatch):
    fake = FakeWriter(owner_email=OTHER_AE)
    monkeypatch.setattr("backend.routers.deal_defer.HubSpotWriteClient", lambda: fake)
    _commitment(db, deal=DEFERRED)
    _as()
    assert _post_defer().status_code == 200
    # The task goes to the deal's HubSpot owner, who has to do the follow-up.
    assert fake.tasks[0]["owner_email"] == OTHER_AE


def test_off_the_roster_is_a_403(db, writer, monkeypatch):
    monkeypatch.setattr(
        "backend.routers.deal_defer.feature_allows", lambda key, user: False
    )
    _as()
    assert _post_defer().status_code == 403
    assert client.get("/api/sales/deals/deferred").status_code == 403


def test_it_ships_open_to_everyone_without_the_exec_door():
    from rubik_core.features.registry import feature_by_key

    policy = feature_by_key("deal_defer").default()
    assert policy.everyone and not policy.departments
    # Narrowed later, seniority still does not open it: it acts on own deals.
    assert not policy.exec_allowed


def test_a_deal_gone_from_hubspot_is_a_404(db, monkeypatch):
    fake = FakeWriter(gone=True)
    monkeypatch.setattr("backend.routers.deal_defer.HubSpotWriteClient", lambda: fake)
    _as()
    assert _post_defer().status_code == 404


# ── 3. undefer, and the list it lives on ─────────────────────────────────────


def test_undefer_brings_the_deal_back_and_says_the_task_stays(db, writer):
    _as()
    _post_defer()
    assert [
        d["deal_id"] for d in client.get("/api/sales/deals/deferred").json()["items"]
    ] == [DEFERRED]

    res = client.post(f"/api/sales/deals/{DEFERRED}/undefer")
    assert res.status_code == 200
    assert "still in HubSpot" in res.json()["message"]
    row = db.query(DealDefer).one()
    assert row.ended_reason == "undeferred" and row.ended_by == AE
    assert client.get("/api/sales/deals/deferred").json()["items"] == []
    assert dd.deferred_deal_ids([DEFERRED], reader=AE) == set()


def test_undefer_of_a_deal_that_is_not_deferred_is_a_404(db, writer):
    _as()
    assert client.post(f"/api/sales/deals/{DEFERRED}/undefer").status_code == 404


def test_another_rep_cannot_undefer_your_deal(db, writer):
    _defer_row(db)
    _as(OTHER_AE)
    assert client.post(f"/api/sales/deals/{DEFERRED}/undefer").status_code == 403
    assert db.query(DealDefer).one().ended_at is None


def test_the_deferred_list_is_only_the_callers(db, writer):
    _defer_row(db, DEFERRED, owner=AE)
    _defer_row(db, LIVE, owner=OTHER_AE)
    _as()
    items = client.get("/api/sales/deals/deferred").json()["items"]
    assert [i["deal_id"] for i in items] == [DEFERRED]


def test_it_comes_back_on_its_follow_up_date_with_nothing_running(db, monkeypatch):
    _defer_row(db, days=30)
    assert dd.deferred_deal_ids([DEFERRED], reader=AE) == {DEFERRED}
    monkeypatch.setattr(
        "rubik_core.sales.deal_defer.reader_today",
        lambda _e: TODAY + timedelta(days=30),
    )
    assert dd.deferred_deal_ids([DEFERRED], reader=AE) == set()


def test_deferring_again_after_it_came_back_opens_a_new_defer(db, writer, monkeypatch):
    _defer_row(db, days=5)
    monkeypatch.setattr(
        "rubik_core.sales.deal_defer.reader_today",
        lambda _e: TODAY + timedelta(days=6),
    )
    _as()
    res = client.post(
        f"/api/sales/deals/{DEFERRED}/defer",
        json={"follow_up_on": "2026-12-01", "note": "Again."},
    )
    assert res.json()["outcome"] == "created"
    old, new = db.query(DealDefer).order_by(DealDefer.created_at).all()
    assert old.ended_reason == "expired" and new.ended_at is None


def test_reads_fail_open(monkeypatch):
    """An unreadable defer table shows every deal — never hides one."""

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr("rubik_core.sales.deal_defer.active_defers", boom)
    assert dd.active_by_deal([DEFERRED], reader=AE, db=object()) == {}


# ── 2. My Day ────────────────────────────────────────────────────────────────


def _commitment(db, *, deal: str, owner: str = AE, kind: str = "sow", state="breached"):
    now = datetime.utcnow()
    row = Commitment(
        id=uuid.uuid4().hex,
        kind=kind,
        hubspot_deal_id=deal,
        hubspot_company_id=f"co-{deal}",
        owner_email=owner,
        opened_at=now - timedelta(days=6),
        opened_source="call",
        sow_kind="standard" if kind == "sow" else None,
        sla_hours=24,
        breach_at=now - timedelta(days=3),
        state=state,
    )
    row.refresh_live_scope_flags()
    db.add(row)
    db.commit()
    return row


def _lag(db, *, deal: str) -> CommitmentStageLag:
    row = CommitmentStageLag(
        id=uuid.uuid4().hex,
        hubspot_deal_id=deal,
        hubspot_stage_id="stage-1",
        evidence_stage_id="stage-2",
        confidence=0.9,
        status="open",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def my_day_world(db, monkeypatch):
    from backend.routers import cockpit as cockpit_mod

    commitments = {d: _commitment(db, deal=d) for d in (DEFERRED, LIVE)}
    lags = {d: _lag(db, deal=d) for d in (DEFERRED, LIVE)}
    crm = cockpit_mod._Crm(
        available=True,
        reason=None,
        owner_id="o-1",
        deal_names={DEFERRED: "Acme 201", LIVE: "Beta 202"},
        stale_deals=[
            {"id": DEFERRED, "name": "Acme 201", "days": 120, "state": "quiet"},
            {"id": LIVE, "name": "Beta 202", "days": 95, "state": "quiet"},
        ],
    )
    monkeypatch.setattr("backend.routers.cockpit._crm_context", lambda **_kw: crm)
    return SimpleNamespace(commitments=commitments, lags=lags)


def _my_day_ids() -> dict[str, list[str]]:
    res = client.get("/api/sales/my-day")
    assert res.status_code == 200, res.text
    blocks = res.json()["blocks"]
    return {
        k: blocks[k]["items"]
        for k in ("pipeline_hygiene", "deals", "no_contact_90_days")
    }


def test_my_day_hides_a_deferred_deal_from_all_three_deal_blocks(db, my_day_world):
    _defer_row(db)
    _as()
    blocks = _my_day_ids()

    assert [i["id"] for i in blocks["deals"]] == [my_day_world.commitments[LIVE].id]
    assert [i["id"] for i in blocks["pipeline_hygiene"]] == [my_day_world.lags[LIVE].id]
    assert [i["id"] for i in blocks["no_contact_90_days"]] == [LIVE]
    # Every remaining deal row carries the Defer handle for the live deal.
    for items in blocks.values():
        assert items[0]["defer"] == {"deal_id": LIVE, "deal_name": "Beta 202"}


def test_my_day_shows_it_again_on_its_date_and_after_undefer(
    db, my_day_world, monkeypatch, writer
):
    _defer_row(db, days=30)
    _as()
    monkeypatch.setattr(
        "rubik_core.sales.deal_defer.reader_today",
        lambda _e: TODAY + timedelta(days=30),
    )
    assert len(_my_day_ids()["deals"]) == 2

    monkeypatch.setattr("rubik_core.sales.deal_defer.reader_today", lambda _e: TODAY)
    assert len(_my_day_ids()["deals"]) == 1
    client.post(f"/api/sales/deals/{DEFERRED}/undefer")
    assert len(_my_day_ids()["deals"]) == 2


def test_my_day_rows_carry_no_defer_handle_off_the_roster(
    db, my_day_world, monkeypatch
):
    monkeypatch.setattr(
        "rubik_core.features.feature_allows",
        lambda key, user, db=None: key != "deal_defer",
    )
    _as()
    for items in _my_day_ids().values():
        assert all("defer" not in i for i in items)


# ── 2. the daily rhythm (My Day) and Ask Cubie ───────────────────────────────


def _rhythm_result(check: str):
    from rubik_core.sales.rhythm_checks import CheckResult, CheckRow

    return CheckResult(
        check=check,
        rows=[
            CheckRow(
                record_id=DEFERRED,
                object_type="deal",
                title="Acme 201",
                reasons=["quiet 9d"],
            ),
            CheckRow(
                record_id=LIVE,
                object_type="deal",
                title="Beta 202",
                reasons=["quiet 8d"],
            ),
            CheckRow(
                record_id="t-1", object_type="task", title="Call Acme", reasons=[]
            ),
        ],
        scanned=14,
    )


@pytest.fixture
def rhythm(monkeypatch):
    from rubik_core.sales.rhythm_checks import RhythmScope

    calls = {}

    def run_check(name, _client, _scope, **_kw):
        calls[name] = calls.get(name, 0) + 1
        return _rhythm_result(name)

    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda: object()
    )
    monkeypatch.setattr(
        "rubik_core.sales.rhythm_checks.resolve_scope",
        lambda *_a, **_k: RhythmScope(
            owner_ids=("o-1",), user_ids=None, team=False, label="you"
        ),
    )
    monkeypatch.setattr("rubik_core.sales.rhythm_checks.run_check", run_check)
    return calls


def test_the_daily_rhythm_hides_a_deferred_deal_and_says_so(db, rhythm):
    from backend.routers import sales_rhythm

    _defer_row(db)
    res = sales_rhythm._build("midday", AE, can_defer=True)

    for block in res.blocks:
        ids = [i.id for i in block.items]
        assert f"deal-{DEFERRED}" not in ids and f"deal-{LIVE}" in ids
        assert "1 deal you deferred is left out" in (block.note or "")
        live = next(i for i in block.items if i.id == f"deal-{LIVE}")
        assert live.defer.deal_id == LIVE
        task = next(i for i in block.items if i.id == "task-t-1")
        assert task.defer is None  # a task is not a deal


def test_the_cached_check_answer_is_never_edited_in_place(db):
    """run_check caches its CheckResult for five minutes; hiding a row in the
    cached object would hide it for the team view and after an Undefer too."""
    _defer_row(db)
    result = _rhythm_result("overdue")
    defers = dd.active_by_deal([DEFERRED, LIVE], reader=AE)

    hidden = dd.apply_to_check(result, defers, hide=True)
    marked = dd.apply_to_check(result, defers, hide=False)

    assert len(result.rows) == 3 and result.notes == []
    assert [r.record_id for r in hidden.rows] == [LIVE, "t-1"]
    # A leader's team view keeps the deal, marked with the date and the note.
    acme = next(r for r in marked.rows if r.record_id == DEFERRED)
    assert "deferred until 2026-11-14: Budget opens in Q1." in acme.reasons


def test_the_assistant_cold_scan_leaves_out_your_deferred_deals(db, monkeypatch):
    """ "Which of my deals haven't I contacted in a week" — the rep's own list."""
    from rubik_core.retrieval.hubspot_api import HubSpotAPIRetriever

    _defer_row(db)

    class _HS:
        def search_all(self, *_a, **_k):
            return [
                {
                    "id": DEFERRED,
                    "properties": {"dealname": "Acme 201", "hubspot_owner_id": "o-1"},
                },
                {
                    "id": LIVE,
                    "properties": {"dealname": "Beta 202", "hubspot_owner_id": "o-1"},
                },
            ]

        def owner_name_map(self):
            return {"o-1": "Parker"}

    monkeypatch.setattr(
        "rubik_core.sales.rhythm_checks.client_contacts", lambda _c, _ids: {}
    )
    retriever = HubSpotAPIRetriever.__new__(HubSpotAPIRetriever)
    mine = retriever._deals_needing_followup(
        _HS(), {"stale_days": 7}, (None, None, ""), "o-1", "", hide_deferred_for=AE
    )
    team = retriever._deals_needing_followup(
        _HS(), {"stale_days": 7}, (None, None, ""), None, ""
    )
    assert "Acme 201" not in mine and "Beta 202" in mine
    assert "1 deal you deferred is left out" in mine
    assert "Acme 201" in team


# ── 2. priorities: no new signal, and today's card drops it ──────────────────


def test_a_deferred_deal_emits_no_priorities_signal(db, monkeypatch):
    from backend.signals import hubspot as hs_mod
    from backend.signals.hubspot import HubspotSignalSource
    from backend.tests.test_priorities_close_date_probe import NOW, _deal, _FakeHubSpot

    deferred = _deal(close_in_days=20, quiet_days=82, deal_id=DEFERRED)
    live = _deal(close_in_days=20, quiet_days=82, deal_id=LIVE)
    fake = _FakeHubSpot(deals=[deferred, live])
    monkeypatch.setattr(hs_mod, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda *a, **k: fake
    )
    monkeypatch.setattr(
        hs_mod, "_cached_directories", lambda _c: ({"o-1": {"email": AE}}, {})
    )
    _defer_row(db)

    sigs = HubspotSignalSource().fetch(AE, since=NOW)

    deals = {s.entity_refs.get("deal_id") for s in sigs}
    assert LIVE in deals and DEFERRED not in deals


def test_todays_card_drops_the_deferred_deal_on_the_next_read(db):
    from backend.routers.home import _apply_deal_defers

    bundle = {
        "priorities": [
            {"id": "p1", "entity_kind": "deal", "entity_id": DEFERRED},
            {"id": "p2", "entity_kind": "deal", "entity_id": LIVE},
            {"id": "p3", "entity_kind": "event", "entity_id": DEFERRED},
        ]
    }
    _defer_row(db)
    kept = _apply_deal_defers(AE, bundle)["priorities"]
    # Only the deal's own card goes; an event card that shares an id stays.
    assert [p["id"] for p in kept] == ["p2", "p3"]


# ── 2. the cockpit digest DM and My To-Dos ───────────────────────────────────


def test_the_digest_drops_deferred_deal_rows_but_keeps_the_wrapup(db):
    from rubik_core.cockpit import digest

    sow = _commitment(db, deal=DEFERRED)
    wrapup = _commitment(db, deal=DEFERRED, kind="wrapup", state="open")
    live = _commitment(db, deal=LIVE)
    _lag(db, deal=DEFERRED)
    _defer_row(db)

    items = digest.gather_actionable(db, AE)
    ids = {i["id"] for i in items}
    assert live.id in ids and wrapup.id in ids
    assert sow.id not in ids
    assert not any(
        i["source"] == "stage_lag" and i["deal_id"] == DEFERRED for i in items
    )


def test_my_todos_customer_commitments_drop_the_deferred_deal(db):
    from rubik_core.promises.readthrough import commitments_for

    _commitment(db, deal=DEFERRED)
    live = _commitment(db, deal=LIVE)
    _defer_row(db)
    assert [c["id"] for c in commitments_for(db, AE)] == [live.id]


# ── 4. the manager views never hide it ───────────────────────────────────────


def test_the_team_rollup_lists_every_deferred_deal_with_owner_date_and_note(db):
    from backend.routers.cockpit import _deferred_rows

    _defer_row(db, DEFERRED, owner=AE)
    _defer_row(db, LIVE, owner="outsider@acme.example")

    team = SimpleNamespace(unrestricted=False, owner_emails=frozenset({AE}))
    [row] = _deferred_rows(db, team)
    assert row.deal_name == f"Deal {DEFERRED}"
    assert row.follow_up_on == "2026-11-14"
    assert row.note == "Budget opens in Q1."
    assert row.owner_name  # a name, never blank

    everyone = SimpleNamespace(unrestricted=True, owner_emails=frozenset())
    assert len(_deferred_rows(db, everyone)) == 2


class _RollupHS:
    """The weekly roll-up's HubSpot read: three open deals."""

    def __init__(self):
        now = datetime.utcnow()
        self.deals = [
            # Deferred, and its close date passed 10 days ago.
            self._deal(
                DEFERRED, "Acme 201", now - timedelta(days=10), now - timedelta(days=5)
            ),
            # Live, date passed 3 days ago: ranked.
            self._deal(
                LIVE, "Beta 202", now - timedelta(days=3), now - timedelta(days=2)
            ),
            # Deferred with no close date at all: still listed.
            self._deal("203", "Gamma 203", None, now - timedelta(days=2)),
        ]

    @staticmethod
    def _deal(deal_id, name, close, contacted):
        return {
            "id": deal_id,
            "properties": {
                "hs_object_id": deal_id,
                "dealname": name,
                "dealstage": "s1",
                "pipeline": "default",
                "hubspot_owner_id": "o-1",
                "closedate": close.isoformat() + "Z" if close else None,
                "notes_last_contacted": contacted.isoformat() + "Z",
                "amount": "1000",
            },
        }

    def owner_directory(self):
        return {"o-1": {"email": AE, "name": "Parker Rep"}}

    def deal_stage_labels(self):
        return {"s1": "Proposal"}

    def search_all(self, *_a, **_k):
        return list(self.deals)


def test_the_weekly_rollup_lists_deferred_deals_instead_of_ranking_them(
    db, monkeypatch
):
    import backend.jobs.close_date_rollup as mod

    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda *a, **k: _RollupHS()
    )
    _defer_row(db, DEFERRED, note="Waiting on their <!channel> budget & board.")
    _defer_row(db, "203", days=60, note="Q2 RFP.")

    res = mod.collect_rows()

    assert res.status == "ok"
    assert [r.deal_id for r in res.rows] == [LIVE]
    assert sum(res.tier_counts.values()) == 1
    assert [d.deal_id for d in res.deferred] == [DEFERRED, "203"]
    acme = res.deferred[0]
    assert acme.owner == "Parker Rep" and acme.follow_up_on == "2026-11-14"
    assert acme.flag == "close date passed 10d ago"
    assert res.deferred[1].flag is None

    text, blocks = mod.build_blocks(res)
    assert "Deferred by their reps* — 2 of 3 open deals" in repr(blocks)
    assert "Acme 201 — Parker Rep, until 2026-11-14" in text
    rendered = repr(blocks)
    # A rep's note can never smuggle a mention or a link into the message,
    assert "<!channel>" not in rendered and "&lt;!channel&gt;" in rendered
    # and the message stays read-only.
    assert not any(b.get("type") == "actions" for b in blocks)


def test_a_quiet_week_still_lists_the_deferred_deals():
    from backend.jobs.close_date_rollup import DeferredRow, RollupResult, build_blocks

    res = RollupResult(checked=5, open_deals=6)
    res.deferred = [
        DeferredRow(
            deal_id="9",
            deal_name="Delta",
            owner="Parker",
            follow_up_on="2026-12-01",
            note="Q1.",
        )
    ]
    text, _blocks = build_blocks(res)
    assert "Nothing to flag among the deals still on the reps' lists" in text
    assert "Deferred by their reps: 1 of 6 open deal" in text


# ── review fixes ─────────────────────────────────────────────────────────────


def test_a_closed_commitment_does_not_make_someone_the_owner(db, monkeypatch):
    """A rep who once worked a deal that was reassigned must not be able to
    hide it from its current owner."""
    fake = FakeWriter(owner_email=OTHER_AE)
    monkeypatch.setattr("backend.routers.deal_defer.HubSpotWriteClient", lambda: fake)
    old = _commitment(db, deal=DEFERRED)
    old.closed_at = datetime.utcnow()
    old.state = "closed"
    old.refresh_live_scope_flags()
    db.commit()
    _as()
    assert _post_defer().status_code == 403
    assert db.query(DealDefer).count() == 0


def _ledger_key(row: DealDefer) -> str:
    from backend.routers.calls import _idempotency_key

    return _idempotency_key(f"deal_defer:{row.id}", "tasks", DEFERRED, "deal_defer")


def test_a_held_claim_is_reported_as_in_progress_never_as_written(db, writer):
    row = _defer_row(db)
    row.hubspot_task_status = "pending"
    db.add(
        CallHubspotWrite(
            id=uuid.uuid4().hex,
            idempotency_key=_ledger_key(row),
            method="POST",
            path="/crm/v3/objects/tasks",
            attempts=1,
            updated_at=datetime.utcnow(),
        )
    )
    db.commit()
    _as()
    res = _post_defer().json()  # same date and note: the same defer, same key

    assert res["outcome"] == "unchanged"
    assert res["hubspot_task"]["status"] == "in_progress"
    assert writer.tasks == []
    assert db.query(DealDefer).one().hubspot_task_status == "pending"


def test_an_unconfirmed_task_keeps_its_claim_so_a_retry_cannot_write_twice(
    db, monkeypatch
):
    class _Garbled(FakeWriter):
        def create_task(self, *_a, **_k):
            raise ValueError("response was not JSON")

    fake = _Garbled()
    monkeypatch.setattr("backend.routers.deal_defer.HubSpotWriteClient", lambda: fake)
    _as()
    first = _post_defer().json()
    assert first["hubspot_task"]["status"] == "unconfirmed"
    row = db.query(DealDefer).one()
    assert row.hubspot_task_status == "pending"
    assert "Check the deal in HubSpot" in row.hubspot_task_error

    good = FakeWriter()
    monkeypatch.setattr("backend.routers.deal_defer.HubSpotWriteClient", lambda: good)
    retry = _post_defer().json()
    # Held for the ledger's lease: the task may have landed.
    assert retry["hubspot_task"]["status"] == "in_progress"
    assert good.tasks == []


def test_a_failed_defer_read_leaves_the_callers_session_usable(db):
    """Fail open must not poison the request's transaction: My Day reads its
    wrap-ups on the same session right after the defers."""
    import sqlalchemy as sa

    _commitment(db, deal=LIVE)
    db.execute(sa.text("DROP TABLE deal_defers"))
    db.commit()

    assert dd.active_by_deal([LIVE], reader=AE, db=db) == {}
    assert db.query(Commitment).count() == 1


def test_deferred_on_is_the_reps_calendar_day_not_utcs(db):
    # The test workspace's control row is pinned to America/Chicago.
    # 01:00 UTC on the 16th is 8 p.m. on the 15th in Chicago.
    assert dd.local_day(AE, datetime(2026, 10, 16, 1, 0)) == date(2026, 10, 15)
