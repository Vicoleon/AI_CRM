"""Deep Prep system email sender — transport seam and workspace delivery gates.

Exercises the send path entirely through the swappable fake transport, so no
test ever contacts Gmail.
"""

from types import SimpleNamespace

import pytest

from rubik_core.company.operations import OperationsConfig
from rubik_core.config import settings
from rubik_core.tools import email_sender as es


class FakeTransport:
    def __init__(self, raise_with: Exception | None = None):
        self.raise_with = raise_with
        self.calls: list[dict] = []

    def send(self, *, sender, impersonate, spec):
        self.calls.append({"sender": sender, "impersonate": impersonate, "spec": spec})
        if self.raise_with:
            raise self.raise_with
        return "gmail-msg-123"


@pytest.fixture
def fake():
    t = FakeTransport()
    es.set_transport_factory(lambda: t)
    yield t
    es.reset_transport_factory()


@pytest.fixture(autouse=True)
def _reset_flags(monkeypatch):
    # The sender name is workspace policy, not a process variable.
    monkeypatch.setattr(settings, "CUBIE_DEEP_PREP_EMAIL_AUTOSEND", True)
    monkeypatch.setattr(
        es,
        "get_operations_config",
        lambda: OperationsConfig(deep_prep_sender_name="Acme Sales"),
    )


def _spec():
    return es.EmailMessageSpec(
        to=["pjensen@acme.example"],
        cc=["ifoster@acme.example"],
        subject="Your Deep Prep: Initech Rollout SOW",
        text_body="Attached.",
        html_body="<p>Attached.</p>",
        pdf=b"%PDF-1.4 fake",
        pdf_filename="initech-deep-prep.pdf",
    )


def test_autosend_off_returns_disabled_and_never_calls_transport(fake, monkeypatch):
    monkeypatch.setattr(settings, "CUBIE_DEEP_PREP_EMAIL_AUTOSEND", False)
    result = es.send_email(_spec(), impersonate="pjensen@acme.example")
    assert result.status == "disabled"
    assert fake.calls == []  # kill switch short-circuits before the transport


def test_sends_via_transport_and_returns_id(fake):
    result = es.send_email(_spec(), impersonate="pjensen@acme.example")
    assert result.status == "sent"
    assert result.message_id == "gmail-msg-123"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    # From header carries the display name + impersonated mailbox.
    assert call["sender"] == "Acme Sales <pjensen@acme.example>"
    assert call["impersonate"] == "pjensen@acme.example"
    assert call["spec"].to == ["pjensen@acme.example"]
    assert call["spec"].pdf == b"%PDF-1.4 fake"


def test_workspace_sender_policy_does_not_redirect_an_artifact(fake, monkeypatch):
    monkeypatch.setattr(
        es,
        "get_operations_config",
        lambda: OperationsConfig(deep_prep_sender_name="Acme Delivery"),
    )
    result = es.send_email(_spec(), impersonate="pjensen@acme.example")
    assert result.status == "sent"
    assert result.redirected_to is None
    assert fake.calls[0]["sender"] == "Acme Delivery <pjensen@acme.example>"
    sent = fake.calls[0]["spec"]
    assert sent.to == ["pjensen@acme.example"]
    assert sent.cc == ["ifoster@acme.example"]


def test_foreign_recipient_or_sender_is_refused_before_transport(
    fake, set_test_workspace_domains
):
    set_test_workspace_domains("acme.example")
    outside = _spec()
    outside.to = ["other@bravo.example"]
    result = es.send_email(outside, impersonate="pjensen@acme.example")
    assert result.status == "failed"
    assert fake.calls == []

    result = es.send_email(_spec(), impersonate="other@bravo.example")
    assert result.status == "failed"
    assert fake.calls == []

    outside_cc = _spec()
    outside_cc.cc = ["other@bravo.example"]
    result = es.send_email(outside_cc, impersonate="pjensen@acme.example")
    assert result.status == "failed"
    assert fake.calls == []


def test_mail_header_injection_is_refused_before_transport(fake):
    spec = _spec()
    spec.to = ["pjensen@acme.example\r\nBcc: foreign@bravo.example"]
    assert es.send_email(spec, impersonate="pjensen@acme.example").status == "failed"
    assert (
        es.send_email(
            _spec(),
            impersonate="pjensen@acme.example",
            sender_name="Acme\r\nBcc: foreign@bravo.example",
        ).status
        == "failed"
    )
    assert fake.calls == []


def test_exact_current_workspace_user_can_receive_even_without_a_verified_domain(
    tmp_path, monkeypatch
):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.control import repository
    from rubik_core.database import db as tenant_db
    from rubik_core.database.models import User
    from rubik_core.tenancy.context import use_workspace

    engine = create_engine(f"sqlite:///{tmp_path / 'people.db'}")
    User.__table__.create(engine)
    maker = sessionmaker(bind=engine)
    with maker() as db:
        db.add_all(
            [
                User(email="contractor@outside.example", id="c1", name="Contractor"),
                User(
                    email="support@outside.example",
                    id="s1",
                    name="Support",
                    is_platform_support=True,
                ),
            ]
        )
        db.commit()
    monkeypatch.setattr(tenant_db, "SessionLocal", maker)
    monkeypatch.setattr(
        repository, "get_workspace", lambda _slug: SimpleNamespace(status="active")
    )
    monkeypatch.setattr(repository, "domain_belongs_to_workspace", lambda *_: False)
    try:
        with use_workspace("acme"):
            assert es._workspace_address_allowed("contractor@outside.example")
            assert not es._workspace_address_allowed("support@outside.example")
            assert not es._workspace_address_allowed("stranger@outside.example")
    finally:
        engine.dispose()


def test_workspace_policy_outage_refuses_to_send(fake, monkeypatch):
    def unavailable():
        raise RuntimeError("tenant DB down")

    monkeypatch.setattr(es, "get_operations_config", unavailable)
    result = es.send_email(_spec(), impersonate="pjensen@acme.example")
    assert result.status == "failed"
    assert fake.calls == []


def test_transport_error_is_caught_not_raised(monkeypatch):
    boom = FakeTransport(raise_with=RuntimeError("gmail 500"))
    es.set_transport_factory(lambda: boom)
    try:
        result = es.send_email(_spec(), impersonate="pjensen@acme.example")
    finally:
        es.reset_transport_factory()
    assert result.status == "failed"
    assert "gmail 500" in (result.error or "")
