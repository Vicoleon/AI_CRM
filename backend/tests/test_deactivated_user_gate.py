"""Deactivated / offboarded-user gate for AI Priorities.

An ex-employee (Slack account ``deleted`` / Google Workspace ``suspended``) must
be EXCLUDED from the priorities roster and Slack delivery — distinct from PTO (no
"holding your priorities" note; just dropped) — so they stop generating empty
daily bundles and tripping the false "no bundle for their local today" ops alert.

Covers the four layers:
  1. detection sources (Slack primary, Google Directory complement) + combiner,
  2. the generation-roster filter (``list_active_users``),
  3. the delivery recipient filter + ops-alert classification split,
  4. the Google channel-agent auth-failure propagation ([ERROR], not "0 signals").
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from rubik_core.config import settings


@pytest.fixture(autouse=True)
def _workspace_domain(set_test_workspace_domains):
    set_test_workspace_domains("acme.example")


# ─── Layer 1a: the pure combiner ─────────────────────────────────────────────


def test_resolve_unions_sources_and_restricts_to_candidates():
    from rubik_core.directory.active_status import resolve_deactivated_emails

    class _Src:
        def __init__(self, hits):
            self._hits = hits

        def deactivated_emails(self, candidates):
            return self._hits

    candidates = ["a@x.example", "B@x.example", "c@x.example"]
    out = resolve_deactivated_emails(
        candidates,
        [_Src({"a@x.example"}), _Src({"b@x.example", "not-a-candidate@x.example"})],
    )
    # Union of both sources, lower-cased, and never an email outside candidates.
    assert out == {"a@x.example", "b@x.example"}


def test_resolve_is_fail_open_when_a_source_raises():
    from rubik_core.directory.active_status import resolve_deactivated_emails

    class _Boom:
        def deactivated_emails(self, candidates):
            raise RuntimeError("slack down")

    class _Good:
        def deactivated_emails(self, candidates):
            return {"a@x.example"}

    # The raising source contributes nothing; the good one still counts.
    out = resolve_deactivated_emails(["a@x.example", "b@x.example"], [_Boom(), _Good()])
    assert out == {"a@x.example"}


def test_resolve_empty_candidates_short_circuits():
    from rubik_core.directory.active_status import resolve_deactivated_emails

    class _NeverCalled:
        def deactivated_emails(self, candidates):  # pragma: no cover
            raise AssertionError("should not be called for empty candidates")

    assert resolve_deactivated_emails([], [_NeverCalled()]) == set()


# ─── Layer 1b: Slack primary source ──────────────────────────────────────────


def _bot_with_pages(pages):
    """A SlackBotClient whose users.list returns the given fake pages."""
    from backend.services.slack_service import SlackBotClient

    bot = SlackBotClient(bot_token="xoxb-test")  # explicit token → no DB lookup
    calls = {"n": 0}

    def _fake_api(method, *, http_method="get", params=None, json_body=None):
        assert method == "users.list"
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    bot._api = _fake_api  # type: ignore[assignment]
    return bot, calls


def test_slack_deleted_member_emails_paginates_and_filters():
    pages = [
        {
            "members": [
                {"deleted": False, "profile": {"email": "active@acme.example"}},
                {"deleted": True, "profile": {"email": "GONE@acme.example"}},
            ],
            "response_metadata": {"next_cursor": "CURSOR2"},
        },
        {
            "members": [
                {"deleted": True, "profile": {"email": "left@acme.example"}},
                {"deleted": True, "profile": {"email": ""}},  # no email → skipped
            ],
            "response_metadata": {"next_cursor": ""},
        },
    ]
    bot, calls = _bot_with_pages(pages)
    out = bot.deleted_member_emails()
    assert out == {"gone@acme.example", "left@acme.example"}
    assert calls["n"] == 2  # followed the cursor across both pages
    # Cached: a second call does not hit the API again.
    assert bot.deleted_member_emails() == out
    assert calls["n"] == 2


def test_member_emails_shares_the_one_sweep_with_the_deleted_set():
    """``member_emails`` (roster-hygiene classification) and
    ``deleted_member_emails`` (the offboarding gate) come off the SAME cached
    ``users.list`` pass — a batch must not pay for two full sweeps."""
    pages = [
        {
            "members": [
                {"deleted": False, "profile": {"email": "active@acme.example"}},
                {"deleted": True, "profile": {"email": "GONE@acme.example"}},
                {"deleted": False, "profile": {"email": ""}},  # no email → skipped
            ],
            "response_metadata": {"next_cursor": ""},
        },
    ]
    bot, calls = _bot_with_pages(pages)

    # Members include BOTH active and deactivated accounts: the question it
    # answers is "does this address exist here at all".
    assert bot.member_emails() == {"active@acme.example", "gone@acme.example"}
    assert bot.deleted_member_emails() == {"gone@acme.example"}
    assert calls["n"] == 1  # one sweep served both


def test_slack_source_intersects_with_candidates():
    from backend.services.directory_status import SlackDeactivationSource

    bot = MagicMock()
    bot.is_available = True
    bot.deleted_member_emails.return_value = {
        "gone@acme.example",
        "other@acme.example",
    }
    src = SlackDeactivationSource(bot)
    # Only candidates that are also Slack-deleted come back.
    assert src.deactivated_emails(["gone@acme.example", "active@acme.example"]) == {
        "gone@acme.example"
    }


def test_slack_source_noop_without_bot_token():
    from backend.services.directory_status import SlackDeactivationSource

    bot = MagicMock()
    bot.is_available = False
    src = SlackDeactivationSource(bot)
    assert src.deactivated_emails(["gone@acme.example"]) == set()
    bot.deleted_member_emails.assert_not_called()


# ─── Layer 1c: Google Directory complement ───────────────────────────────────


class _FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "err"


class _FakeUsers:
    def __init__(self, responses):
        self._responses = responses  # email -> dict | Exception

    def get(self, userKey, fields=None):
        value = self._responses[userKey]

        class _Exec:
            def execute(_self):
                if isinstance(value, Exception):
                    raise value
                return value

        return _Exec()


class _FakeDirService:
    def __init__(self, responses):
        self._users = _FakeUsers(responses)

    def users(self):
        return self._users


def _directory_client(responses):
    from rubik_core.tools.directory import GoogleDirectoryClient

    client = GoogleDirectoryClient.__new__(GoogleDirectoryClient)  # skip real build
    client.service = _FakeDirService(responses)
    return client


def test_directory_suspended_emails_flags_suspended_and_archived():
    from googleapiclient.errors import HttpError

    client = _directory_client(
        {
            "sus@acme.example": {"suspended": True},
            "arch@acme.example": {"archived": True},
            "active@acme.example": {"suspended": False},
            "ghost@acme.example": HttpError(_FakeResp(404), b"{}"),  # not found
        }
    )
    out = client.suspended_emails(
        [
            "sus@acme.example",
            "arch@acme.example",
            "active@acme.example",
            "ghost@acme.example",
        ]
    )
    assert out == {"sus@acme.example", "arch@acme.example"}


def test_directory_suspended_emails_aborts_on_auth_error():
    from googleapiclient.errors import HttpError

    client = _directory_client({"a@acme.example": HttpError(_FakeResp(403), b"{}")})
    # A 401/403 (scope not granted) must raise so the caller self-disables.
    with pytest.raises(HttpError):
        client.suspended_emails(["a@acme.example"])


def test_google_source_self_disables_on_failure(monkeypatch):
    """The domain source swallows a construction/scope failure → empty set."""
    from rubik_core.directory import active_status as mod

    class _BoomClient:
        def __init__(self, *a, **k):
            raise RuntimeError("no admin.directory scope")

    monkeypatch.setattr("rubik_core.tools.directory.GoogleDirectoryClient", _BoomClient)
    src = mod.GoogleDirectorySuspendedSource()
    assert src.deactivated_emails(["a@acme.example"]) == set()


# ─── Layer 1d: backend composition + kill switch ─────────────────────────────


def test_deactivated_priorities_emails_kill_switch(monkeypatch):
    from backend.services import directory_status as ds

    monkeypatch.setattr(settings, "CUBIE_OFFBOARD_DETECTION_ENABLED", False)
    # A source that WOULD flag someone must not even be consulted when disabled.
    monkeypatch.setattr(
        ds,
        "GoogleDirectorySuspendedSource",
        lambda: (_ for _ in ()).throw(AssertionError("should be gated off")),
    )
    assert ds.deactivated_priorities_emails(["gone@acme.example"]) == set()


def test_deactivated_priorities_emails_unions_slack_and_google(monkeypatch):
    from backend.services import directory_status as ds

    monkeypatch.setattr(settings, "CUBIE_OFFBOARD_DETECTION_ENABLED", True)

    class _Google:
        def deactivated_emails(self, candidates):
            return {"suspended@acme.example"}

    monkeypatch.setattr(ds, "GoogleDirectorySuspendedSource", _Google)

    bot = MagicMock()
    bot.is_available = True
    bot.deleted_member_emails.return_value = {"deleted@acme.example"}

    out = ds.deactivated_priorities_emails(
        [
            "deleted@acme.example",
            "suspended@acme.example",
            "active@acme.example",
        ],
        bot=bot,
    )
    assert out == {"deleted@acme.example", "suspended@acme.example"}


# ─── Layer 2: the generation-roster filter ───────────────────────────────────


def _roster_db(monkeypatch, emails):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import rubik_core.database.db as db_mod
    from rubik_core.database.models import Base, User

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    with TestSession() as s:
        s.add_all(
            [
                # last_login set: these fixtures exercise the DEACTIVATION gate,
                # so they must clear the never-signed-in filter that runs first.
                User(
                    email=e,
                    id=str(i),
                    name=e,
                    department="sales",
                    is_exec=False,
                    last_login=datetime(2026, 7, 1),
                )
                for i, e in enumerate(emails)
            ]
        )
        s.commit()
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_list_active_users_excludes_deactivated(monkeypatch):
    from rubik_core.priorities.batch import list_active_users

    _roster_db(
        monkeypatch,
        ["rep@acme.example", "gone@acme.example", "ceo@acme.example"],
    )

    def _lookup(emails):
        assert "gone@acme.example" in emails  # gets the eligible roster
        return {"gone@acme.example"}

    result = set(list_active_users(deactivated_lookup=_lookup))
    assert result == {"rep@acme.example", "ceo@acme.example"}
    assert "gone@acme.example" not in result


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_list_active_users_fail_open_when_lookup_raises(monkeypatch):
    from rubik_core.priorities.batch import list_active_users

    _roster_db(monkeypatch, ["rep@acme.example", "gone@acme.example"])

    def _lookup(emails):
        raise RuntimeError("slack + directory both down")

    # A detection outage must NOT block the batch — full eligible roster returned.
    result = set(list_active_users(deactivated_lookup=_lookup))
    assert result == {"rep@acme.example", "gone@acme.example"}


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_list_active_users_no_lookup_is_unchanged(monkeypatch):
    from rubik_core.priorities.batch import list_active_users

    _roster_db(monkeypatch, ["rep@acme.example", "ceo@acme.example"])
    # Existing callers (availability refresh, tests) pass nothing → no filtering.
    assert set(list_active_users()) == {
        "rep@acme.example",
        "ceo@acme.example",
    }


# ─── Layer 3: delivery recipient filter + alert classification ───────────────


import backend.agents.slack_delivery as sd  # noqa: E402


class _FakeBot:
    is_available = True

    def __init__(self):
        self.dms: list[tuple[str, str]] = []

    def send_dm(self, email, text=None, blocks=None):
        self.dms.append((email, text))
        return "ts-1"


class _FakeRegistry:
    def get_user(self, email):
        return {"email": email}


def _deliver(
    monkeypatch,
    bot,
    *,
    items_by_email,
    deactivated=frozenset(),
    status_by_email=None,
    ops=("ops@acme.example",),
):
    status_by_email = status_by_email or {}
    monkeypatch.setattr(sd, "SlackBotClient", lambda: bot)
    monkeypatch.setattr(
        sd, "_load_bundle_priority_items", lambda e, **_k: items_by_email.get(e, [])
    )
    monkeypatch.setattr(
        sd, "deactivated_priorities_emails", lambda recips, **k: set(deactivated)
    )
    monkeypatch.setattr(sd, "_bundle_pto_for", lambda e: None)  # no PTO, no DB

    # Default an unlisted empty recipient to "missing card". Entries may be
    # given as (present, status) or (present, status, stored_count); pad the
    # short form so the 3-tuple contract holds.
    def _status(e):
        entry = status_by_email.get(e, (False, None))
        return entry if len(entry) == 3 else (*entry, 0)

    monkeypatch.setattr(sd, "_today_bundle_status", _status)
    monkeypatch.setattr(sd, "_ops_recipients", lambda db: list(ops))
    import rubik_core.auth.registry as registry_mod

    monkeypatch.setattr(registry_mod, "UserRegistry", _FakeRegistry)
    return sd.deliver_slack_priorities(MagicMock(), emails=list(items_by_email.keys()))


def _item():
    return sd._BundlePriority(title="T", severity="info", body="b", link=None)


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_delivery_drops_deactivated_and_never_alerts_them(monkeypatch):
    bot = _FakeBot()
    result = _deliver(
        monkeypatch,
        bot,
        items_by_email={
            "gone@acme.example": [],
            "rep@acme.example": [_item()],
        },
        deactivated={"gone@acme.example"},
    )
    assert result.users_deactivated == ["gone@acme.example"]
    assert "gone@acme.example" not in result.users_skipped_empty
    assert result.users_missing_card == []  # never counted as a failure
    assert result.users_delivered == 1
    # No ops alert fires — a deactivated user is not a delivery problem.
    assert all(e != "ops@acme.example" for (e, _) in bot.dms)
    # The dropped user got NO DM (distinct from PTO, which sends a note).
    assert all(e != "gone@acme.example" for (e, _) in bot.dms)


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_delivery_empty_card_present_is_not_alerted(monkeypatch):
    """A card exists but is empty (no_signals) → legitimate quiet day, NO alert."""
    bot = _FakeBot()
    result = _deliver(
        monkeypatch,
        bot,
        items_by_email={"quiet@acme.example": []},
        status_by_email={"quiet@acme.example": (True, "no_signals")},
    )
    assert result.users_empty_no_signals == ["quiet@acme.example"]
    assert result.users_missing_card == []
    # The scary "no bundle / timezone bug" alert must NOT fire for this case.
    assert all(e != "ops@acme.example" for (e, _) in bot.dms)


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_delivery_missing_card_still_alerts(monkeypatch):
    """No card at all → a real generation gap → ops IS paged."""
    bot = _FakeBot()
    result = _deliver(
        monkeypatch,
        bot,
        items_by_email={"gap@acme.example": []},
        status_by_email={"gap@acme.example": (False, None)},
    )
    assert result.users_missing_card == ["gap@acme.example"]
    assert result.users_empty_no_signals == []
    ops_dms = [(e, t) for (e, t) in bot.dms if e == "ops@acme.example"]
    assert len(ops_dms) == 1
    assert "gap@acme.example" in ops_dms[0][1]


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_delivery_mixed_only_alerts_the_real_gap(monkeypatch):
    bot = _FakeBot()
    result = _deliver(
        monkeypatch,
        bot,
        items_by_email={
            "gap@acme.example": [],  # missing → alert
            "quiet@acme.example": [],  # empty card → no alert
            "gone@acme.example": [],  # deactivated → dropped
            "rep@acme.example": [_item()],  # delivered
        },
        deactivated={"gone@acme.example"},
        status_by_email={
            "gap@acme.example": (False, None),
            "quiet@acme.example": (True, "no_signals"),
        },
    )
    assert result.users_delivered == 1
    assert result.users_deactivated == ["gone@acme.example"]
    assert result.users_missing_card == ["gap@acme.example"]
    assert result.users_empty_no_signals == ["quiet@acme.example"]
    ops_text = "\n".join(t for (e, t) in bot.dms if e == "ops@acme.example")
    assert "gap@acme.example" in ops_text
    # The quiet + deactivated users appear only as non-alarming FYI context.
    assert "generation gap" in ops_text


# ─── Layer 4: Google channel-agent auth-failure propagation ──────────────────


def test_require_live_service_raises_only_when_configured(monkeypatch):
    from rubik_core.priorities.agents._google_auth import (
        ChannelAuthError,
        require_live_service,
    )

    # Workspace configured + service failed to build → a real auth failure.
    with patch(
        "rubik_core.priorities.agents._google_auth.workspace_configured",
        return_value=True,
    ):
        with pytest.raises(ChannelAuthError):
            require_live_service(None, channel="gmail", user_email="gone@acme.example")

    # A live service is always fine.
    require_live_service(object(), channel="gmail", user_email="a@acme.example")

    # No DWD configuration → a missing service is expected.
    with patch(
        "rubik_core.priorities.agents._google_auth.workspace_configured",
        return_value=False,
    ):
        require_live_service(None, channel="gmail", user_email="a@acme.example")


def test_is_auth_error_recognises_auth_and_ignores_transient():
    from rubik_core.priorities.agents._google_auth import is_auth_error

    class _RefreshError(Exception):
        pass

    assert is_auth_error(RuntimeError("invalid_grant: Invalid email or User ID"))
    assert is_auth_error(_RefreshError("token refresh failed"))
    assert is_auth_error(RuntimeError("Gmail service not initialized."))

    class _Http:
        def __init__(self, status):
            self.resp = _FakeResp(status)

    assert is_auth_error(_Http(403))
    assert not is_auth_error(_Http(503))
    assert not is_auth_error(ValueError("empty LLM output"))


def test_gmail_agent_records_fetch_failed_on_dead_service(monkeypatch):
    """A deactivated account (service=None, key set) → digest.error, not empty."""
    from rubik_core.priorities.agents.gmail_agent import GmailChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig

    monkeypatch.setattr(
        "rubik_core.priorities.agents._google_auth.workspace_configured",
        lambda: True,
    )

    class _DeadGmailClient:
        def __init__(self, user_email):
            self.service = None  # DWD build failed (deactivated / revoked)

    monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", _DeadGmailClient)

    agent = GmailChannelAgent(
        user_email="gone@acme.example",
        config=PrioritiesConfig.default(),
        temporal=None,
    )
    digest = agent.run()
    assert digest.error is not None
    assert digest.error.startswith("fetch_failed")
    assert not digest.signals
