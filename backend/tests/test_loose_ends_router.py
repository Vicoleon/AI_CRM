"""Sales → Loose Ends: the HTTP surface, the /users/me fold and the chat tool.

Gates first (the kill switch is a 404, the roster a 403, and the team view has
its OWN roster), then the rules the router itself is responsible for:

* own items only — someone else's id is the same 404 as a made-up one;
* "Not a real miss" needs a real reason, and a mute is a sender or a domain;
* "Log it in HubSpot" previews the AE's own sent emails and logs exactly the ones
  named, once — a second click answers with the first result, and the item
  closes with ids only as evidence; a failure partway, or a batch sent with
  ``more``, leaves the item open with the ids already written remembered;
* nothing is read from a mailbox for a log, written to HubSpot or drafted in
  Gmail while impersonating;
* a HubSpot or Gmail failure is a sentence (502), never a 500;
* the team view is metadata only and every read leaves an audit row.

The scan's read models and writers (``rubik_core.loop_closure``) are faked BY
STRING TARGET: this suite reloads modules mid-run, and the router reaches them
through the module attribute at call time, so a string patch is what lands.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.auth import get_current_user, require_full_app_access
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.identifiers import (
    CRM_LOGGED_DETAIL_KEY,
    LOOP_CLOSE_LOGGED_FROM_ASSISTANT,
)
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

client = TestClient(app)

AE = "ae@acme.example"
OTHER = "other.ae@acme.example"
LEADER = "leader@acme.example"


def _profile(email: str = AE, department: str = "Sales") -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].replace(".", " ").title(),
        department=department,
        roles=["user"],
        created_at="2026-10-06T00:00:00",
        updated_at="2026-10-06T00:00:00",
    )


def _as(email: str = AE) -> UserProfile:
    profile = _profile(email)
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


@pytest.fixture
def db(tmp_path):
    """A private SQLite database behind ``get_db`` (audit rows land here)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'loose_ends.db'}",
        connect_args={"check_same_thread": False},
    )
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()

    def _get_db():
        yield session

    app.dependency_overrides[get_db] = _get_db
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def roster(monkeypatch):
    """Which Feature Access keys the caller holds — both, unless a test says."""
    allowed = {"loop_closure", "loop_closure_team"}
    monkeypatch.setattr(
        "backend.routers.loose_ends.feature_allows",
        lambda key, user, db=None: key in allowed,
    )
    return allowed


@pytest.fixture(autouse=True)
def _setup(monkeypatch, roster):
    # These cases exercise Loose Ends' own gates; the enclosing core gate has
    # separate coverage and needs no unrelated feature_access database here.
    app.dependency_overrides[require_full_app_access] = lambda: _profile()
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    monkeypatch.setattr(
        "rubik_core.auth.impersonation.impersonation_active", lambda: False
    )
    yield
    app.dependency_overrides.clear()


def _impersonating(monkeypatch):
    monkeypatch.setattr(
        "rubik_core.auth.impersonation.impersonation_active", lambda: True
    )


def _item(**over) -> SimpleNamespace:
    base = {
        "id": "it1",
        "owner_email": AE,
        "kind": "unlogged",
        "source": "gmail",
        "status": "open",
        "close_reason": None,
        "close_evidence": None,
        "thread_id": "t1",
        "hubspot_contact_id": "101",
        "hubspot_company_id": None,
        "hubspot_deal_id": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def items(monkeypatch):
    """``store.get_owned`` over an in-memory dict, keyed (owner, id)."""
    rows: dict[tuple[str, str], SimpleNamespace] = {}
    calls: list[tuple[str, str]] = []
    locked: list[str] = []
    # What a locked re-read returns instead of the plain row (a concurrent
    # request committed in between): {item_id: row}.
    after_lock: dict[str, SimpleNamespace] = {}

    def get_owned(_db, owner_email, item_id, *, for_update=False):
        calls.append((owner_email, item_id))
        if for_update:
            locked.append(item_id)
            if item_id in after_lock and (owner_email, item_id) in rows:
                return after_lock[item_id]
        return rows.get((owner_email, item_id))

    monkeypatch.setattr("rubik_core.loop_closure.store.get_owned", get_owned)
    return SimpleNamespace(rows=rows, calls=calls, locked=locked, after_lock=after_lock)


def _mine_body(**over) -> dict:
    body = {
        "scanned_at": "2026-10-07T12:04:11",
        "coverage": {
            "gmail": "ok",
            "slack": "not_connected",
            "hubspot": "ok",
            "ai": "ok",
            "threads_scanned": 52,
            "slack_messages_scanned": 0,
            "promises_scanned": 4,
            "tasks_scanned": 7,
        },
        "notes": ["Slack is not connected, so Slack messages were not checked."],
        "totals": {
            "open": 1,
            "over_escalation": 0,
            "flagged_this_week": 1,
            "closed_this_week": 0,
        },
        "groups": [
            {
                "kind": "open_loop",
                "label": "Waiting on your reply",
                "hint": "A client wrote and is waiting.",
                "empty_line": "Nobody is waiting on you.",
                "count": 1,
                "items": [
                    {
                        "id": "it9",
                        "kind": "open_loop",
                        "source": "gmail",
                        "title": "Re: Onboarding pricing",
                        "headline": None,
                        "counterparty": "Jane Doe",
                        "company": "Acme Supply",
                        "opened_at": "2026-10-01T15:00:00",
                        "first_flagged_at": "2026-10-03T12:00:00",
                        "days_open": 4,
                        "age": {"state": "at_risk", "label": "4 days"},
                        "reopened": False,
                        "primary_action": {
                            "kind": "draft_reply",
                            "label": "Draft a reply",
                        },
                        "secondary_action": {
                            "kind": "open_link",
                            "label": "Open in Gmail",
                            "href": "https://mail.google.com/mail/u/0/#all/abc",
                        },
                    }
                ],
            }
        ],
        "thresholds": {
            "open_loop_business_days": 2,
            "todo_grace_days": 5,
            "todo_undated_days": 5,
            "escalation_days": 7,
        },
        "mutes": [{"id": "m1", "pattern": "@acmecorp.example", "created_at": None}],
        "content_hidden": False,
    }
    body.update(over)
    return body


def _team_body() -> dict:
    return {
        "scanned_at": "2026-10-07T12:04:11",
        "people": [
            {
                "email": OTHER,
                "name": "Zed Rep",
                "open": 1,
                "by_kind": {"unlogged": 1, "open_loop": 0, "todo": 0},
                "aging": {"d0_2": 0, "d3_7": 0, "d8_14": 1, "d15_plus": 0},
                "over_escalation": 1,
                "closed_this_week": 0,
                "coverage": {
                    "gmail": "unavailable",
                    "slack": "ok",
                    "hubspot": "ok",
                    "ai": "ok",
                },
                "items": [
                    {
                        "id": "t1",
                        "kind": "unlogged",
                        "who": "Jane Doe · Acme Supply",
                        "days_open": 9,
                        "age": {"state": "breached", "label": "9 days"},
                        # A view that leaked content by mistake: the router's
                        # schema must drop it, not pass it through.
                        "title": "SECRET SUBJECT LINE",
                        "headline": "SECRET HEADLINE",
                    }
                ],
            },
            {
                "email": AE,
                "name": "Avery Rep",
                "open": 0,
                "by_kind": {},
                "aging": {},
                "over_escalation": 0,
                "closed_this_week": 2,
                "items": [],
            },
        ],
        "roster_size": 2,
        "health": {
            "window_days": 30,
            "flagged": 41,
            "dismissed": 3,
            "noise_rate": 0.073,
            "dismissed_by_reason": {"automated": 2, "not_a_client": 1},
            "closed": 22,
            "closed_by_reason": {"replied": 9},
            "median_days_to_close": 2.5,
            "weekly": [
                {
                    "week_start": "2026-09-14",
                    "flagged": 9,
                    "closed": 7,
                    "median_days_to_close": 3.0,
                }
            ],
        },
        "scorecard": {
            "metric_name": "Loose ends over 7 days",
            "metrics_found": 0,
            "people_without_metric": 2,
            "can_setup": True,
        },
    }


# ── gates ────────────────────────────────────────────────────────────────────


def test_the_router_is_mounted_under_the_copilot_parent():
    paths = set(app.openapi()["paths"])
    for path in (
        "/api/sales/loose-ends",
        "/api/sales/loose-ends/{item_id}/log-preview",
        "/api/sales/loose-ends/{item_id}/log",
        "/api/sales/loose-ends/{item_id}/draft-reply",
        "/api/sales/loose-ends/{item_id}/dismiss",
        "/api/sales/loose-ends/mutes/{mute_id}",
        "/api/sales/loose-ends/team",
        "/api/sales/loose-ends/team/scorecard-setup",
    ):
        assert path in paths, path


def test_switched_off_is_a_404_everywhere(monkeypatch, db):
    _as()
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)
    assert client.get("/api/sales/loose-ends").status_code == 404
    assert client.get("/api/sales/loose-ends/team").status_code == 404
    assert client.get("/api/sales/loose-ends/it1/log-preview").status_code == 404
    assert client.post("/api/sales/loose-ends/it1/log").status_code == 404
    assert (
        client.post(
            "/api/sales/loose-ends/it1/log", json={"message_ids": ["m1"]}
        ).status_code
        == 404
    )
    assert client.post("/api/sales/loose-ends/team/scorecard-setup").status_code == 404


def test_off_the_roster_is_a_403_that_says_so(roster, db):
    _as()
    roster.clear()
    res = client.get("/api/sales/loose-ends")
    assert res.status_code == 403
    assert "Loose Ends" in res.json()["detail"]
    assert (
        client.post(
            "/api/sales/loose-ends/it1/dismiss", json={"reason": "other"}
        ).status_code
        == 403
    )


def test_the_team_view_has_its_own_roster(monkeypatch, roster, db):
    monkeypatch.setattr(
        "rubik_core.loop_closure.views.team_view", lambda _db, **_k: _team_body()
    )
    monkeypatch.setattr(
        "rubik_core.loop_closure.views.mine_view", lambda *_a, **_k: _mine_body()
    )
    _as()
    roster.discard("loop_closure_team")
    assert client.get("/api/sales/loose-ends/team").status_code == 403
    assert client.post("/api/sales/loose-ends/team/scorecard-setup").status_code == 403
    assert client.get("/api/sales/loose-ends").status_code == 200

    # A leader on the team roster only (Emery, Indigo) reads the roll-up
    # without being scanned — and without an AE list of their own.
    _as(LEADER)
    roster.clear()
    roster.add("loop_closure_team")
    assert client.get("/api/sales/loose-ends/team").status_code == 200
    assert client.get("/api/sales/loose-ends").status_code == 403


# ── the AE's own list ────────────────────────────────────────────────────────


def test_mine_reads_the_callers_own_list_and_drops_empty_fields(monkeypatch, db):
    seen = {}

    def mine_view(_db, owner_email, **kwargs):
        seen["owner"] = owner_email
        seen.update(kwargs)
        return _mine_body()

    monkeypatch.setattr("rubik_core.loop_closure.views.mine_view", mine_view)
    _as("AE@ACME.example ")
    res = client.get("/api/sales/loose-ends")
    assert res.status_code == 200, res.text
    body = res.json()
    # A Sales AE has no My To-Dos page, so no row may fall back to /todos.
    assert seen == {"owner": AE, "content_hidden": False, "has_todos": False}
    item = body["groups"][0]["items"][0]
    assert item["title"] == "Re: Onboarding pricing"
    assert "headline" not in item  # None is omitted, not shipped as null
    assert "note_preview" not in item
    assert item["secondary_action"]["href"].startswith("https://mail.google.com/")
    assert body["coverage"]["threads_scanned"] == 52
    assert body["mutes"] == [{"id": "m1", "pattern": "@acmecorp.example"}]


def test_impersonation_hides_content_on_the_ae_view(monkeypatch, db):
    seen = {}

    def mine_view(_db, owner_email, **kwargs):
        seen.update(kwargs)
        return _mine_body(content_hidden=kwargs.get("content_hidden", False))

    monkeypatch.setattr("rubik_core.loop_closure.views.mine_view", mine_view)
    _impersonating(monkeypatch)
    _as()
    body = client.get("/api/sales/loose-ends").json()
    assert seen["content_hidden"] is True
    assert body["content_hidden"] is True


def test_before_the_first_scan_scanned_at_and_coverage_are_absent(monkeypatch, db):
    monkeypatch.setattr(
        "rubik_core.loop_closure.views.mine_view",
        lambda *_a, **_k: _mine_body(scanned_at=None, coverage=None, groups=[]),
    )
    _as()
    body = client.get("/api/sales/loose-ends").json()
    # response_model_exclude_none: "never scanned" arrives as a missing key,
    # which the client reads with `== null`.
    assert "scanned_at" not in body and "coverage" not in body


# ── own items only ───────────────────────────────────────────────────────────


def test_someone_elses_id_is_the_same_404_as_a_made_up_one(monkeypatch, items, db):
    items.rows[(OTHER, "theirs")] = _item(id="theirs", owner_email=OTHER)
    monkeypatch.setattr(
        "rubik_core.loop_closure.crm_log.log_emails",
        lambda *_a, **_k: pytest.fail("wrote HubSpot for someone else's item"),
    )
    monkeypatch.setattr(
        "rubik_core.loop_closure.crm_log.log_preview",
        lambda *_a, **_k: pytest.fail("read someone else's mailbox"),
    )
    _as()
    picked = {"message_ids": ["m1"]}
    for path, payload in (
        ("/api/sales/loose-ends/theirs/log", picked),
        ("/api/sales/loose-ends/theirs/draft-reply", None),
        ("/api/sales/loose-ends/theirs/dismiss", {"reason": "automated"}),
        ("/api/sales/loose-ends/nope/log", picked),
    ):
        res = client.post(path, json=payload)
        assert res.status_code == 404, path
        assert res.json()["detail"] == "Not found."
    for path in (
        "/api/sales/loose-ends/theirs/log-preview",
        "/api/sales/loose-ends/nope/log-preview",
    ):
        res = client.get(path)
        assert res.status_code == 404, path
        assert res.json()["detail"] == "Not found."
    # Looked up under the CALLER's address, never one taken from the request.
    assert {owner for owner, _ in items.calls} == {AE}


def test_removing_someone_elses_mute_is_a_404(monkeypatch, db):
    seen = []

    def remove_mute(_db, owner_email, mute_id):
        seen.append((owner_email, mute_id))
        return mute_id == "mine"

    monkeypatch.setattr("rubik_core.loop_closure.store.remove_mute", remove_mute)
    _as()
    assert client.delete("/api/sales/loose-ends/mutes/theirs").status_code == 404
    res = client.delete("/api/sales/loose-ends/mutes/mine")
    assert res.status_code == 200 and res.json() == {"status": "removed"}
    assert seen == [(AE, "theirs"), (AE, "mine")]


# ── "Not a real miss" ────────────────────────────────────────────────────────


def test_dismiss_needs_a_real_reason_and_a_real_mute(monkeypatch, items, db):
    items.rows[(AE, "it1")] = _item()
    dismissed = []

    def dismiss(_db, item, *, reason, actor, mute=None, now=None):
        dismissed.append((item.id, reason, actor, mute))
        item.status = "dismissed"
        return item

    monkeypatch.setattr("rubik_core.loop_closure.store.dismiss", dismiss)
    _as()
    url = "/api/sales/loose-ends/it1/dismiss"
    assert client.post(url, json={"reason": "bored"}).status_code == 400
    assert (
        client.post(url, json={"reason": "automated", "mute": "everyone"}).status_code
        == 400
    )
    assert client.post(url, json={}).status_code == 422
    assert dismissed == []

    res = client.post(url, json={"reason": "Automated", "mute": "domain"})
    assert res.status_code == 200 and res.json() == {"status": "dismissed"}
    assert dismissed == [("it1", "automated", AE, "domain")]

    # Already dismissed: nothing left to do, and the writer is not called again.
    assert client.post(url, json={"reason": "automated"}).status_code == 409
    assert len(dismissed) == 1


def test_a_store_refusal_is_a_400_not_a_500(monkeypatch, items, db):
    items.rows[(AE, "it1")] = _item()

    def dismiss(*_a, **_k):
        raise ValueError("That reason is not offered.")

    monkeypatch.setattr("rubik_core.loop_closure.store.dismiss", dismiss)
    _as()
    res = client.post("/api/sales/loose-ends/it1/dismiss", json={"reason": "other"})
    assert res.status_code == 400
    assert res.json()["detail"] == "That reason is not offered."


# ── "Log it in HubSpot" ──────────────────────────────────────────────────────

URL = "https://app.hubspot.com/contacts/1/contact/101"


def _preview(**over) -> dict:
    body = {
        "emails": [
            {
                "message_id": "m1",
                "subject": "Onboarding pricing",
                "to": ["jane@acmecorp.example"],
                "cc": [],
                "sent_at": "2026-10-05T15:00:00",
                "excerpt": "Hi Jane,",
                "chars": 1200,
                "truncated": False,
                "plain_text_only": True,
                "to_contact": True,
                "already_logged": False,
            },
            {
                "message_id": "m2",
                "subject": "Re: Onboarding pricing",
                "to": ["jane@acmecorp.example"],
                "cc": ["bob@beta.com"],
                "sent_at": "2026-10-06T15:00:00",
                "excerpt": "Following up",
                "chars": 300,
                "truncated": False,
                "already_logged": True,
            },
        ],
        "contact": "Jane Doe",
        "company": "Acme Supply",
        "deal": None,
        "hubspot_url": URL,
    }
    body.update(over)
    return body


@pytest.fixture
def previews(monkeypatch):
    calls = []

    def log_preview(item, *, owner_email, gmail=None, read_client=None):
        calls.append((item.id, owner_email))
        return _preview()

    monkeypatch.setattr("rubik_core.loop_closure.crm_log.log_preview", log_preview)
    return calls


def test_the_preview_shows_the_aes_sent_emails_and_where_they_land(previews, items, db):
    items.rows[(AE, "it1")] = _item()
    _as("AE@ACME.example")
    res = client.get("/api/sales/loose-ends/it1/log-preview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert previews == [("it1", AE)]  # the caller's own mailbox, folded
    assert [e["message_id"] for e in body["emails"]] == ["m1", "m2"]
    assert body["emails"][1]["already_logged"] is True
    assert body["emails"][0]["chars"] == 1200
    # What the page needs to tick the right boxes and explain a long email;
    # a field the server left out defaults to unticked.
    assert body["emails"][0]["to_contact"] is True
    assert body["emails"][0]["plain_text_only"] is True
    assert body["emails"][1]["to_contact"] is False
    assert body["contact"] == "Jane Doe" and body["company"] == "Acme Supply"
    assert "deal" not in body  # None is omitted, not shipped as null
    assert body["hubspot_url"] == URL
    assert "note" not in res.text


def test_the_preview_is_for_an_open_unlogged_email_only(previews, items, db):
    items.rows[(AE, "loop")] = _item(id="loop", kind="open_loop")
    items.rows[(AE, "slack")] = _item(id="slack", source="slack", thread_id=None)
    items.rows[(AE, "done")] = _item(id="done", status="closed", close_reason="replied")
    _as()
    for item_id in ("loop", "slack", "done"):
        res = client.get(f"/api/sales/loose-ends/{item_id}/log-preview")
        assert res.status_code == 409, item_id
        assert res.json()["detail"].endswith(".")
    assert (
        "open the record in HubSpot"
        in client.get("/api/sales/loose-ends/slack/log-preview").json()["detail"]
    )
    assert previews == []


def test_the_preview_never_reads_a_mailbox_while_impersonating(
    monkeypatch, previews, items, db
):
    items.rows[(AE, "it1")] = _item()
    _impersonating(monkeypatch)
    _as()
    res = client.get("/api/sales/loose-ends/it1/log-preview")
    assert res.status_code == 409
    assert "someone else" in res.json()["detail"]
    assert previews == []


@pytest.mark.parametrize(
    "raised,status,detail",
    [
        ("log_error", 502, "could not read that email thread"),
        ("value", 409, "Add the contact in HubSpot first"),
        ("boom", 502, "could not read that email just now"),
    ],
)
def test_a_preview_failure_is_a_sentence(
    monkeypatch, items, db, raised, status, detail
):
    from rubik_core.loop_closure import crm_log

    items.rows[(AE, "it1")] = _item()

    def log_preview(*_a, **_k):
        if raised == "log_error":
            raise crm_log.LogError(
                "Cubie could not read that email thread from your mailbox."
            )
        if raised == "value":
            raise ValueError("Add the contact in HubSpot first.")
        raise RuntimeError("HTTP 500 from somewhere")

    monkeypatch.setattr("rubik_core.loop_closure.crm_log.log_preview", log_preview)
    _as()
    res = client.get("/api/sales/loose-ends/it1/log-preview")
    assert res.status_code == status
    assert detail in res.json()["detail"]
    assert "HTTP 500" not in res.text


@pytest.fixture
def hubspot(monkeypatch):
    """``crm_log.log_emails`` faked; the REAL ``store.mark_logged_from_assistant``
    closes the item, so the evidence it stores is what is asserted."""
    writes = []
    already: set[str] = set()

    def log_emails(item, *, owner_email, message_ids, gmail=None, write_client=None):
        writes.append((item.id, owner_email, list(message_ids)))
        return {
            "logged": [
                {
                    "message_id": m,
                    "hubspot_id": f"h-{m}",
                    "mode": "already_exists" if m in already else "created",
                }
                for m in message_ids
            ],
            "hubspot_url": URL,
        }

    monkeypatch.setattr("rubik_core.loop_closure.crm_log.log_emails", log_emails)
    monkeypatch.setattr(
        "rubik_core.sales.rhythm_checks.get_hubspot_config",
        lambda: HubSpotConnectorConfig(portal_id="1"),
    )
    return SimpleNamespace(writes=writes, already=already)


def test_log_needs_one_to_twenty_message_ids(hubspot, items, db):
    items.rows[(AE, "it1")] = _item()
    _as()
    url = "/api/sales/loose-ends/it1/log"
    for payload in (None, {}, {"message_ids": []}, {"message_ids": ["  "]}):
        res = client.post(url, json=payload)
        assert res.status_code == 400, payload
        assert res.json()["detail"] == "Pick at least one email to log."
    res = client.post(url, json={"message_ids": [f"m{i}" for i in range(21)]})
    assert res.status_code == 400 and "at most 20" in res.json()["detail"]
    assert hubspot.writes == [] and items.rows[(AE, "it1")].status == "open"


def test_an_email_the_server_cannot_find_is_a_400_and_nothing_closes(
    monkeypatch, items, db
):
    item = _item()
    items.rows[(AE, "it1")] = item

    def log_emails(*_a, **_k):
        raise ValueError("That email is not one Cubie can log from this loose end.")

    monkeypatch.setattr("rubik_core.loop_closure.crm_log.log_emails", log_emails)
    _as()
    res = client.post("/api/sales/loose-ends/it1/log", json={"message_ids": ["forged"]})
    assert res.status_code == 400
    assert res.json()["detail"].startswith("That email is not one Cubie can log")
    assert item.status == "open"


def test_log_writes_once_and_closes_with_ids_only(hubspot, items, db):
    item = _item()
    items.rows[(AE, "it1")] = item
    hubspot.already.add("m2")
    _as()
    first = client.post(
        "/api/sales/loose-ends/it1/log", json={"message_ids": ["m1", "m2", "m1"]}
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "status": "logged",
        "hubspot_url": URL,
        "logged": 1,
        "already": 1,
    }
    assert hubspot.writes == [("it1", AE, ["m1", "m2"])]  # de-duplicated
    assert (
        item.status == "closed"
        and item.close_reason == LOOP_CLOSE_LOGGED_FROM_ASSISTANT
    )
    evidence = dict(item.close_evidence)
    assert evidence.pop("at")
    assert evidence == {
        "type": "emails",
        "ids": ["h-m1", "h-m2"],
        "message_ids": ["m1", "m2"],
        "hubspot_url": URL,
    }

    again = client.post("/api/sales/loose-ends/it1/log", json={"message_ids": ["m1"]})
    assert again.status_code == 200
    assert again.json() == {
        "status": "logged",
        "hubspot_url": URL,
        "logged": 0,
        "already": 2,
    }
    assert len(hubspot.writes) == 1  # logged once, not twice
    assert items.locked == ["it1", "it1"]  # the row is claimed before writing


def test_a_double_click_racing_the_first_log_writes_nothing_more(hubspot, items, db):
    """The second request's first look saw the item open; by the time it held
    the row lock, the first request had committed. It must answer with that
    result, never log the emails again."""
    items.rows[(AE, "it1")] = _item()
    items.after_lock["it1"] = _item(
        status="closed",
        close_reason=LOOP_CLOSE_LOGGED_FROM_ASSISTANT,
        close_evidence={
            "type": "emails",
            "ids": ["h-m1"],
            "message_ids": ["m1"],
            "hubspot_url": URL,
        },
    )
    _as()
    res = client.post("/api/sales/loose-ends/it1/log", json={"message_ids": ["m1"]})
    assert res.status_code == 200, res.text
    assert res.json()["hubspot_url"] == URL and res.json()["already"] == 1
    assert hubspot.writes == []


def test_an_open_item_already_carrying_logged_emails_is_not_written_twice(
    hubspot, items, db
):
    items.rows[(AE, "it1")] = _item(
        close_evidence={"type": "emails", "ids": ["h-m1"], "message_ids": ["m1"]}
    )
    _as()
    res = client.post("/api/sales/loose-ends/it1/log", json={"message_ids": ["m1"]})
    assert res.status_code == 200
    assert res.json()["hubspot_url"] == URL  # falls back to the item's contact
    assert hubspot.writes == []


def test_log_is_only_for_an_open_unlogged_email(hubspot, items, db):
    items.rows[(AE, "loop")] = _item(id="loop", kind="open_loop")
    items.rows[(AE, "done")] = _item(id="done", status="closed", close_reason="replied")
    items.rows[(AE, "slack")] = _item(id="slack", source="slack", thread_id=None)
    _as()
    picked = {"message_ids": ["m1"]}
    assert client.post("/api/sales/loose-ends/loop/log", json=picked).status_code == 400
    assert client.post("/api/sales/loose-ends/done/log", json=picked).status_code == 409
    res = client.post("/api/sales/loose-ends/slack/log", json=picked)
    assert res.status_code == 409 and "Slack" in res.json()["detail"]
    assert hubspot.writes == []


def test_nothing_is_written_or_drafted_while_impersonating(
    monkeypatch, hubspot, items, db
):
    items.rows[(AE, "it1")] = _item()
    items.rows[(AE, "it2")] = _item(id="it2", kind="open_loop")
    monkeypatch.setattr(
        "rubik_core.loop_closure.drafts.draft_reply",
        lambda *_a, **_k: pytest.fail("drafted while impersonating"),
    )
    _impersonating(monkeypatch)
    _as()
    for path, payload in (
        ("/api/sales/loose-ends/it1/log", {"message_ids": ["m1"]}),
        ("/api/sales/loose-ends/it2/draft-reply", None),
    ):
        res = client.post(path, json=payload)
        assert res.status_code == 409, path
        assert "someone else" in res.json()["detail"]
    assert hubspot.writes == []


@pytest.mark.parametrize(
    "error,detail",
    [
        ("write", "write scope"),
        ("log_error", "could not check HubSpot"),
    ],
)
def test_a_failed_log_is_a_sentence_and_the_item_stays_open(
    monkeypatch, items, db, error, detail
):
    from rubik_core.loop_closure import crm_log
    from rubik_core.retrieval.hubspot_write import HubSpotWriteError

    item = _item()
    items.rows[(AE, "it1")] = item

    def boom(*_a, **_k):
        if error == "write":
            raise HubSpotWriteError("HubSpot write failed (HTTP 403): forbidden")
        raise crm_log.LogError(
            "Cubie could not check HubSpot for these emails just now, so nothing "
            "was logged. Try again in a minute."
        )

    monkeypatch.setattr("rubik_core.loop_closure.crm_log.log_emails", boom)
    monkeypatch.setattr(
        "rubik_core.loop_closure.store.mark_logged_from_assistant",
        lambda *_a, **_k: pytest.fail("closed an item HubSpot never got"),
    )
    _as()
    res = client.post("/api/sales/loose-ends/it1/log", json={"message_ids": ["m1"]})
    assert res.status_code == 502
    assert detail in res.json()["detail"]
    assert item.status == "open"


def test_a_failure_partway_remembers_what_was_written_and_stays_open(
    monkeypatch, items, db
):
    """HubSpot's search lags a fresh write, so the ids of the emails written
    before the failure are stored on the open item before the 502 — the next
    preview and click read them instead of logging them again."""
    from rubik_core.loop_closure import crm_log
    from rubik_core.retrieval.hubspot_write import HubSpotWriteError

    item = _item(detail={"crm_contact_name": "Jane Doe"})
    items.rows[(AE, "it1")] = item

    def partway(*_a, **_k):
        raise crm_log.PartialLogError(
            [{"message_id": "m1", "hubspot_id": "h-m1"}],
            HubSpotWriteError(
                "HubSpot POST /crm/v3/objects/emails failed: HTTP 429: slow down"
            ),
        )

    monkeypatch.setattr("rubik_core.loop_closure.crm_log.log_emails", partway)
    monkeypatch.setattr(
        "rubik_core.loop_closure.store.mark_logged_from_assistant",
        lambda *_a, **_k: pytest.fail("closed an item HubSpot only half has"),
    )
    _as()
    res = client.post(
        "/api/sales/loose-ends/it1/log", json={"message_ids": ["m1", "m2"]}
    )
    assert res.status_code == 502
    assert "rate-limiting" in res.json()["detail"]
    assert "HTTP 429" not in res.text
    assert item.status == "open"
    assert item.detail == {
        "crm_contact_name": "Jane Doe",
        CRM_LOGGED_DETAIL_KEY: {"m1": "h-m1"},
    }
    assert crm_log.logged_by_cubie(item) == {"m1": "h-m1"}


def test_batches_keep_the_item_open_until_the_last_one(hubspot, items, db):
    """More than 20 ticked: the page sends batches, ``more`` on all but the
    last. Each batch is remembered; the last closes the item with every id."""
    item = _item(detail=None)
    items.rows[(AE, "it1")] = item
    _as()
    first = client.post(
        "/api/sales/loose-ends/it1/log",
        json={"message_ids": [f"m{i}" for i in range(20)], "more": True},
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "in_progress"
    assert first.json()["logged"] == 20
    assert item.status == "open"
    assert len(item.detail[CRM_LOGGED_DETAIL_KEY]) == 20

    last = client.post(
        "/api/sales/loose-ends/it1/log", json={"message_ids": ["m20", "m21"]}
    )
    assert last.status_code == 200, last.text
    assert last.json()["status"] == "logged" and last.json()["logged"] == 2
    assert (
        item.status == "closed"
        and item.close_reason == LOOP_CLOSE_LOGGED_FROM_ASSISTANT
    )
    assert item.close_evidence["message_ids"] == [f"m{i}" for i in range(22)]
    assert item.close_evidence["ids"] == [f"h-m{i}" for i in range(22)]


def test_remembering_written_emails_merges_and_never_touches_a_closed_item():
    from rubik_core.loop_closure import store

    item = SimpleNamespace(
        status="open",
        detail={"crm_contact_name": "Jane", CRM_LOGGED_DETAIL_KEY: {"a": "1"}},
    )
    before = item.detail
    store.remember_logged_emails(item, {"b": "2", "": "x", "c": ""})
    assert item.detail == {
        "crm_contact_name": "Jane",
        CRM_LOGGED_DETAIL_KEY: {"a": "1", "b": "2"},
    }
    assert item.detail is not before  # replaced, so the JSON column sees it
    closed = SimpleNamespace(status="closed", detail=None)
    with pytest.raises(ValueError):
        store.remember_logged_emails(closed, {"a": "1"})


# ── "Draft a reply" ──────────────────────────────────────────────────────────


def test_draft_reply_drafts_and_leaves_the_item_open(monkeypatch, items, db):
    item = _item(id="it2", kind="open_loop")
    items.rows[(AE, "it2")] = item
    seen = []

    def draft_reply(it, *, owner_email, gmail=None, invoke=None):
        seen.append((it.id, owner_email))
        return {
            "draft_id": "d1",
            "draft_url": "https://mail.google.com/mail/#drafts/d1",
        }

    monkeypatch.setattr("rubik_core.loop_closure.drafts.draft_reply", draft_reply)
    _as()
    res = client.post("/api/sales/loose-ends/it2/draft-reply")
    assert res.status_code == 200, res.text
    assert res.json() == {
        "status": "drafted",
        "draft_url": "https://mail.google.com/mail/#drafts/d1",
    }
    assert seen == [("it2", AE)] and item.status == "open"
    # The draft action is for open loops only.
    items.rows[(AE, "it1")] = _item()
    assert client.post("/api/sales/loose-ends/it1/draft-reply").status_code == 400


def test_a_draft_error_is_shown_as_written(monkeypatch, items, db):
    from rubik_core.loop_closure import drafts

    items.rows[(AE, "it2")] = _item(id="it2", kind="open_loop")

    def boom(*_a, **_k):
        raise drafts.DraftError("Cubie could not read that thread in Gmail.")

    monkeypatch.setattr("rubik_core.loop_closure.drafts.draft_reply", boom)
    _as()
    res = client.post("/api/sales/loose-ends/it2/draft-reply")
    assert res.status_code == 502
    assert res.json()["detail"] == "Cubie could not read that thread in Gmail."


# ── the team view ────────────────────────────────────────────────────────────


def test_the_team_view_is_metadata_only_and_audited(monkeypatch, db):
    from rubik_core.database.models import AuditLog

    monkeypatch.setattr(
        "rubik_core.loop_closure.views.team_view", lambda _db, **_k: _team_body()
    )
    _as(LEADER)
    res = client.get("/api/sales/loose-ends/team")
    assert res.status_code == 200, res.text
    assert "SECRET" not in res.text
    body = res.json()
    item = body["people"][0]["items"][0]
    assert set(item) == {"id", "kind", "who", "days_open", "age"}
    assert body["health"]["weekly"][0]["week_start"] == "2026-09-14"
    assert body["scorecard"]["can_setup"] is True

    rows = db.query(AuditLog).filter(AuditLog.action == "LOOSE_ENDS_TEAM_VIEW").all()
    assert len(rows) == 1
    assert rows[0].actor_email == LEADER
    assert rows[0].details == {"roster_size": 2, "people": 2}


def test_an_admin_reading_the_team_view_as_a_leader_is_who_the_audit_names(
    monkeypatch, db
):
    """Under person impersonation ``user`` is the impersonated leader; the
    audit row must name the admin who actually read every AE's loose ends."""
    import asyncio

    from backend.routers import loose_ends
    from rubik_core.database.models import AuditLog

    monkeypatch.setattr(
        "rubik_core.loop_closure.views.team_view", lambda _db, **_k: _team_body()
    )
    # What person-impersonation middleware leaves on the request.
    request = SimpleNamespace(
        state=SimpleNamespace(
            impersonation={
                "actor_email": "admin@acme.example",
                "target_email": LEADER,
            }
        )
    )
    asyncio.run(loose_ends.team(request=request, user=_profile(LEADER), db=db))
    [row] = db.query(AuditLog).filter(AuditLog.action == "LOOSE_ENDS_TEAM_VIEW").all()
    assert row.actor_email == "admin@acme.example"  # never "the CEO read it"
    assert row.details["impersonating"] == LEADER

    # Without impersonation the reader is the user, as before.
    plain = SimpleNamespace(state=SimpleNamespace())
    assert loose_ends._audit_actor(plain, _profile(LEADER)) == (LEADER, {})


def test_the_team_response_carries_thresholds_and_the_seatless(monkeypatch, db):
    body = _team_body()
    body["thresholds"] = {
        "open_loop_business_days": 2,
        "todo_grace_days": 5,
        "todo_undated_days": 5,
        "escalation_days": 9,
    }
    body["scorecard"]["people_without_seat"] = [OTHER]
    monkeypatch.setattr(
        "rubik_core.loop_closure.views.team_view", lambda _db, **_k: body
    )
    _as(LEADER)
    got = client.get("/api/sales/loose-ends/team").json()
    assert got["thresholds"]["escalation_days"] == 9
    assert got["scorecard"]["people_without_seat"] == [OTHER]


def test_scorecard_setup_creates_the_rows_and_is_audited(monkeypatch, db):
    from rubik_core.database.models import AuditLog

    seen = {}

    def setup_metrics(_db, *, actor):
        seen["actor"] = actor
        return {"created": 7, "skipped_no_person": [OTHER], "already": 1}

    monkeypatch.setattr(
        "rubik_core.loop_closure.scorecard.setup_metrics", setup_metrics
    )
    _as(LEADER)
    res = client.post("/api/sales/loose-ends/team/scorecard-setup")
    assert res.status_code == 200, res.text
    assert res.json() == {"created": 7, "skipped_no_person": [OTHER], "already": 1}
    assert seen == {"actor": LEADER}
    row = (
        db.query(AuditLog).filter(AuditLog.action == "LOOSE_ENDS_SCORECARD_SETUP").one()
    )
    assert row.details == {"created": 7, "already": 1, "skipped_no_person": 1}


def test_scorecard_setup_requires_workspace_metric(monkeypatch, db):
    from rubik_core.loop_closure.scorecard import ScorecardMetricUnconfigured

    def unconfigured(_db, *, actor):
        raise ScorecardMetricUnconfigured("Set this workspace's metric before setup")

    monkeypatch.setattr("rubik_core.loop_closure.scorecard.setup_metrics", unconfigured)
    _as(LEADER)
    result = client.post("/api/sales/loose-ends/team/scorecard-setup")
    assert result.status_code == 409
    assert "workspace" in result.json()["detail"]


# ── /api/users/me folds the kill switch into both keys ───────────────────────


@pytest.mark.parametrize("enabled", [False, True])
def test_users_me_folds_the_switch_into_both_keys(monkeypatch, db, enabled):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", enabled)
    # The role-permission map reads the shared engine; it is not under test.
    monkeypatch.setattr("backend.routers.users.visible_permissions", lambda _u: {})
    monkeypatch.setattr(
        "rubik_core.features.resolve_all",
        lambda user, db=None, modules=None: {
            "copilot": True,
            "loop_closure": True,
            "loop_closure_team": True,
        },
    )
    _as()
    features = client.get("/api/users/me").json()["features"]
    assert features["loop_closure"] is enabled
    assert features["loop_closure_team"] is enabled
    assert features["copilot"] is True  # the fold touches only its own keys


# ── Ask Cubie: loose_ends(scope) ─────────────────────────────────────────────


@pytest.fixture
def tool(monkeypatch, db):
    """``AgentTools.loose_ends`` for AE, with the profile and policy faked."""
    from rubik_core.adk_agent.tools import AgentTools

    allowed = {"loop_closure"}
    monkeypatch.setattr(
        "rubik_core.auth.registry.UserRegistry",
        lambda: SimpleNamespace(get_user=lambda email: _profile(email)),
    )
    monkeypatch.setattr(
        "rubik_core.features.feature_allows",
        lambda key, user, db=None: key in allowed,
    )
    monkeypatch.setattr(
        "rubik_core.database.db.SessionLocal", lambda: _ClosingSession(db)
    )
    tools = AgentTools.__new__(AgentTools)
    tools.user_email = AE
    return SimpleNamespace(
        run=lambda **k: AgentTools.loose_ends(tools, **k), allowed=allowed
    )


class _ClosingSession:
    """``with SessionLocal() as db`` over the test session, without closing it."""

    def __init__(self, session):
        self._s = session

    def __enter__(self):
        return self._s

    def __exit__(self, *exc):
        return False


def test_the_tool_refuses_when_switched_off(monkeypatch, tool):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)
    out = tool.run()
    assert out["status"] == "error" and "switched off" in out["report"]


def test_the_tool_says_not_scanned_yet_rather_than_all_clear(monkeypatch, tool):
    monkeypatch.setattr(
        "rubik_core.loop_closure.views.mine_view",
        lambda *_a, **_k: _mine_body(scanned_at=None, coverage=None, groups=[]),
    )
    out = tool.run()
    assert out["status"] == "success"
    assert "not scanned" in out["report"] and "not an all-clear" in out["report"]


def test_the_tool_renders_my_list_with_denominators(monkeypatch, tool):
    monkeypatch.setattr(
        "rubik_core.loop_closure.views.mine_view", lambda *_a, **_k: _mine_body()
    )
    report = tool.run()["report"]
    assert "Re: Onboarding pricing" in report  # the owner's own subject
    assert "1 of 1 open" in report
    assert "0 of the 1 have been open 7 days or more" in report
    assert "checked 52 email threads, 0 Slack messages" in report


def test_the_team_answer_needs_the_team_key_and_carries_no_content(
    monkeypatch, tool, db
):
    from rubik_core.database.models import AuditLog

    monkeypatch.setattr(
        "rubik_core.loop_closure.views.team_view", lambda _db, **_k: _team_body()
    )
    refused = tool.run(scope="team")
    assert refused["status"] == "error" and "Sales leaders" in refused["report"]

    tool.allowed.add("loop_closure_team")
    report = tool.run(scope="team", older_than_days=0)["report"]
    assert "SECRET" not in report
    # A–Z by name, never ranked: Avery before Zed although Zed has more open.
    assert report.index("Avery Rep") < report.index("Zed Rep")
    assert "Jane Doe · Acme Supply · 9 days" in report
    assert "1 of 2 people have open loose ends" in report
    assert "Gmail could not be read" in report
    assert "3 of 41 flags" in report
    assert (
        db.query(AuditLog).filter(AuditLog.action == "LOOSE_ENDS_TEAM_VIEW").count()
        == 1
    )

    over = tool.run(scope="team", older_than_days=10)["report"]
    assert "Jane Doe" not in over
    assert "open 10 days or more" in over


def test_the_tool_docstring_routes_away_from_its_neighbours():
    from rubik_core.adk_agent.tools import AgentTools

    doc = AgentTools.loose_ends.__doc__ or ""
    assert "loose ends" in doc
    assert "what_am_i_waiting_on" in doc and "promises_outstanding" in doc


def test_the_coordinator_offers_the_tool_and_routes_to_it():
    """Unregistered, the tool is never offered: the docstring alone routes
    nothing. Pin the registration and the routing paragraph."""
    import inspect

    from rubik_core.agent_runtime import coordinator

    src = inspect.getsource(coordinator)
    assert "tools_instance.loose_ends," in src
    assert "ALWAYS `loose_ends`" in src


# ── the real read models through the real schemas ────────────────────────────


def _seed(db):
    """Two real rows and a completed run, written straight to the tables."""
    from datetime import datetime, timedelta

    from rubik_core.database.loop_closure import LoopItem, LoopScanRun

    now = datetime.utcnow()
    common = {
        "owner_email": AE,
        "status": "open",
        "first_flagged_at": now - timedelta(days=2),
        "last_checked_at": now,
    }
    db.add_all(
        [
            LoopItem(
                kind="open_loop",
                source="gmail",
                source_key="gmail:t1:m1",
                title="Re: SECRET pricing thread",
                headline="SECRET asks for pricing",
                counterparty_label="Jane Doe",
                company_label="Acme Supply",
                thread_id="t1",
                message_id="m1",
                link="https://mail.google.com/mail/u/0/#all/t1",
                opened_at=now - timedelta(days=9),
                **common,
            ),
            LoopItem(
                kind="unlogged",
                source="gmail",
                source_key="gmail:t2",
                title="SECRET proposal follow-up",
                counterparty_label="Sam Roe",
                company_label="Beta Bank",
                hubspot_contact_id="101",
                thread_id="t2",
                opened_at=now - timedelta(days=3),
                **common,
            ),
            LoopScanRun(
                started_at=now - timedelta(hours=1),
                finished_at=now - timedelta(minutes=50),
                status="completed",
                trigger="scheduler",
                dry_run=False,
                report={"status": "completed"},
                coverage={
                    AE: {
                        "gmail": "ok",
                        "slack": "not_connected",
                        "hubspot": "ok",
                        "ai": "ok",
                        "threads_scanned": 52,
                        "slack_messages_scanned": 0,
                        "promises_scanned": 4,
                        "tasks_scanned": 7,
                        "new": 2,
                        "closed": 0,
                    }
                },
            ),
        ]
    )
    db.commit()


def test_the_real_views_fit_the_response_schemas(monkeypatch, db):
    _seed(db)
    monkeypatch.setattr(
        "rubik_core.sales.rhythm_checks.get_hubspot_config",
        lambda: HubSpotConnectorConfig(portal_id="1"),
    )
    monkeypatch.setattr(
        "rubik_core.loop_closure.roster.roster_emails", lambda _db, **_k: [AE]
    )
    _as()
    mine = client.get("/api/sales/loose-ends")
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["totals"]["open"] == 2
    assert [g["kind"] for g in body["groups"]] == ["unlogged", "open_loop", "todo"]
    assert "SECRET" in mine.text  # the owner sees their own subjects

    _impersonating(monkeypatch)
    hidden = client.get("/api/sales/loose-ends")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["content_hidden"] is True
    assert "SECRET" not in hidden.text

    team = client.get("/api/sales/loose-ends/team")
    assert team.status_code == 200, team.text
    assert "SECRET" not in team.text
    [person] = team.json()["people"]
    assert person["open"] == 2
    assert {i["kind"] for i in person["items"]} == {"open_loop", "unlogged"}


def test_the_chat_answer_never_prints_zero_threads_for_an_unread_mailbox():
    from rubik_core.adk_agent.tools import AgentTools

    for coverage in (
        {"gmail": "unavailable", "threads_scanned": 0},
        {"gmail": "unavailable", "scan": "failed"},
    ):
        line = AgentTools._loose_ends_coverage_line(coverage)
        assert "0 email threads" not in line and "not an all-clear" in line
    assert "52 email threads" in AgentTools._loose_ends_coverage_line(
        {"gmail": "partial", "threads_scanned": 52}
    )
