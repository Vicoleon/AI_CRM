"""Loose Ends — the click actions and the judge's parser.

* ``HubSpotWriteClient.create_email`` — Loose Ends' one HubSpot write: its
  property fence, outgoing-only, the owner stamped from the AE's address, the
  association type ids, the live pipeline gate on a deal, and the refusal of
  an email attached to no contact. There is no note path (Fathom owns call
  notes).
* ``find_logged_email`` / ``logged_email_candidates`` — the duplicate probe:
  a window around the sent time, the subject, and an association to the
  item's contact, closest first, one HubSpot email per sent email, backing
  off on a 429.
* ``_with_backoff`` — a create is retried on a 429 only; a deleted deal is a
  ``HubSpotDealGone`` refusal, not a 404 retried four times.
* ``crm_log`` — reading the AE's own sent emails out of a ``format="full"``
  thread (MIME walking, attachments ignored, base64url, HTML-only bodies,
  HubSpot's 65,536-character limit), which messages count (never a forward to
  a personal mailbox; only the contact's own emails start ticked), and "Log
  it in HubSpot" writing exactly those emails: never one the server did not
  re-read, never one HubSpot already has, never one Cubie wrote a moment ago
  (``detail[CRM_LOGGED_DETAIL_KEY]``), and never the whole log lost to a deal in
  someone else's pipeline or one deleted since the scan.
* ``drafts`` — "Draft a reply" creates a draft in the thread and never sends;
  every failure is a sentence the AE can read.
* ``judge.parse_verdicts`` — the model can only answer for tags it was handed.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from rubik_core.ai_providers.registry import ModelRoute
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.identifiers import CRM_LOGGED_DETAIL_KEY
from rubik_core.loop_closure import crm_log, drafts, judge
from rubik_core.retrieval import hubspot_write
from rubik_core.retrieval.hubspot_api import HubSpotAPIError
from rubik_core.retrieval.hubspot_write import (
    HubSpotDealGone,
    HubSpotPipelineRefused,
    HubSpotWriteClient,
    HubSpotWriteError,
    HubSpotWriteRefused,
)
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

AE = "rep@acme.example"
UTC = timezone.utc


@pytest.fixture(autouse=True)
def _workspace_hubspot_config(monkeypatch):
    config = HubSpotConnectorConfig(pipeline_ids=("default",), portal_id="9100001")
    monkeypatch.setattr(hubspot_write, "get_hubspot_config", lambda: config)
    monkeypatch.setattr(
        "rubik_core.sales.rhythm_checks.get_hubspot_config", lambda: config
    )


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# ── create_email ─────────────────────────────────────────────────────────────


class _Recorder(HubSpotWriteClient):
    def __init__(self, pipeline="default", owners=None, errors=(), deal_error=None):
        super().__init__(
            token="test-token",
            base_url="https://hs.example",
            read_client=_ReadClient(),
        )
        self.requests: list[tuple[str, str, dict]] = []
        self.pipeline = pipeline
        self.owners = {AE: "77"} if owners is None else owners
        #: Raised by the next POSTs, in order, before one succeeds.
        self.errors = list(errors)
        self.deal_error = deal_error
        self.deal_reads = 0

    def _request(self, method, path, payload):
        self.requests.append((method, path, payload))
        if self.errors:
            raise self.errors.pop(0)
        return {"id": "email-1"}

    def get_object(self, object_type, object_id, properties):
        self.deal_reads += 1
        if self.deal_error is not None:
            raise self.deal_error
        return {"properties": {"pipeline": self.pipeline}}

    def resolve_owner_id(self, email):
        return self.owners.get((email or "").strip().lower())


GOOD = {
    "hs_timestamp": "1791385200000",
    "hs_email_direction": "EMAIL",
    "hs_email_status": "SENT",
    "hs_email_subject": "Onboarding pricing",
    "hs_email_text": "Hi Jane,\n\nPricing attached.",
}


def test_create_email_posts_the_email_with_its_owner_and_associations(monkeypatch):
    client = _Recorder()
    out = client.create_email(
        GOOD,
        owner_email=AE,
        contact_ids=["c1", "c2", "c1"],
        company_id="co1",
        deal_id="d1",
    )
    assert out == {"id": "email-1", "_mode": "created"}
    [(method, path, body)] = client.requests
    assert (method, path) == ("POST", "/crm/v3/objects/emails")
    assert body["properties"] == {**GOOD, "hubspot_owner_id": "77"}
    targets = [
        (a["to"]["id"], a["types"][0]["associationTypeId"])
        for a in body["associations"]
    ]
    # HubSpot's defined email → contact / company / deal ids; a repeated
    # contact is associated once.
    assert targets == [("c1", 198), ("c2", 198), ("co1", 186), ("d1", 210)]


def test_a_caller_supplied_owner_is_overwritten_by_the_aes_own():
    client = _Recorder()
    client.create_email(
        {**GOOD, "hubspot_owner_id": "999"}, owner_email=AE, contact_ids=["c1"]
    )
    assert client.requests[0][2]["properties"]["hubspot_owner_id"] == "77"


def test_an_ae_who_is_not_a_hubspot_owner_still_logs_the_email_unowned(caplog):
    client = _Recorder(owners={})
    with caplog.at_level(logging.WARNING):
        client.create_email(
            {**GOOD, "hubspot_owner_id": "999"}, owner_email=AE, contact_ids=["c1"]
        )
    [(_m, _p, body)] = client.requests
    assert "hubspot_owner_id" not in body["properties"]
    assert "HubSpot and Workspace addresses differ" in caplog.text
    assert AE not in caplog.text  # the case is named, the address never is


@pytest.mark.parametrize(
    "props",
    [
        {**GOOD, "hs_note_body": "x"},  # not in EMAIL_WRITABLE
        {**GOOD, "hs_email_direction": "INCOMING_EMAIL"},  # Cubie logs what was SENT
        {**GOOD, "hs_email_direction": "FORWARDED_EMAIL"},
        {k: v for k, v in GOOD.items() if k != "hs_email_direction"},
        {k: v for k, v in GOOD.items() if k != "hs_timestamp"},
        {},
    ],
    ids=["stray-property", "incoming", "forwarded", "no-direction", "no-time", "empty"],
)
def test_create_email_refuses_what_the_fence_does_not_allow(props):
    client = _Recorder()
    with pytest.raises(HubSpotWriteRefused):
        client.create_email(props, owner_email=AE, contact_ids=["c1"])
    assert client.requests == []


@pytest.mark.parametrize("contacts", [[], [""], ["  "], None])
def test_an_email_attached_to_no_contact_is_refused(contacts):
    client = _Recorder()
    with pytest.raises(HubSpotWriteRefused, match="contact"):
        client.create_email(
            GOOD, owner_email=AE, contact_ids=contacts, company_id="co1"
        )
    assert client.requests == []


def test_a_deal_in_a_client_pipeline_is_refused_as_a_pipeline_refusal(monkeypatch):
    client = _Recorder(pipeline="initech")
    with pytest.raises(HubSpotPipelineRefused):
        client.create_email(GOOD, owner_email=AE, contact_ids=["c1"], deal_id="d9")
    assert client.requests == []
    # Still a HubSpotWriteRefused for every existing caller.
    assert issubclass(HubSpotPipelineRefused, HubSpotWriteRefused)


def test_a_deal_deleted_since_the_scan_is_a_refusal_read_once(monkeypatch):
    """A 404 on the deal is ``HubSpotDealGone`` — a pipeline refusal, so Loose
    Ends drops the deal and keeps the contact — and is not retried: the deal
    id starts with 5, which the old ``" 5"`` test took for a 5xx."""
    monkeypatch.setattr(
        hubspot_write.time, "sleep", lambda _s: pytest.fail("a 404 was retried")
    )
    client = _Recorder(
        deal_error=HubSpotWriteError(
            "HubSpot read of deals 5123 failed: HTTP 404: not found"
        )
    )
    with pytest.raises(HubSpotDealGone, match="no longer exists"):
        client.create_email(GOOD, owner_email=AE, contact_ids=["c1"], deal_id="5123")
    assert client.requests == [] and client.deal_reads == 1
    assert issubclass(HubSpotDealGone, HubSpotPipelineRefused)
    # Any other read failure is still the error it was.
    broken = _Recorder(
        deal_error=HubSpotWriteError("HubSpot read of deals 9 failed: HTTP 403: x")
    )
    with pytest.raises(HubSpotWriteError) as raised:
        broken.create_email(GOOD, owner_email=AE, contact_ids=["c1"], deal_id="9")
    assert not isinstance(raised.value, HubSpotWriteRefused)


def test_the_email_post_is_retried_on_a_rate_limit_only(monkeypatch):
    """No idempotency key: a timeout or a 5xx can come back after HubSpot
    stored the email, so only a 429 (refused before anything happened) is
    sent again."""
    monkeypatch.setattr(hubspot_write.time, "sleep", lambda _s: None)
    limited = _Recorder(
        errors=[HubSpotWriteError("HubSpot POST /x failed: HTTP 429: slow down")]
    )
    assert limited.create_email(GOOD, owner_email=AE, contact_ids=["c1"])["id"] == (
        "email-1"
    )
    assert len(limited.requests) == 2
    for error in (
        HubSpotWriteError("HubSpot POST /x failed: HTTP 502: bad gateway"),
        HubSpotWriteError("HubSpot transport error: ReadTimeout"),
    ):
        client = _Recorder(errors=[error])
        with pytest.raises(HubSpotWriteError):
            client.create_email(GOOD, owner_email=AE, contact_ids=["c1"])
        assert len(client.requests) == 1, str(error)


def test_transient_means_a_429_a_5xx_or_transport_not_any_five():
    assert hubspot_write._is_transient("HubSpot POST /x failed: HTTP 503: down")
    assert hubspot_write._is_transient("HubSpot emails search failed: HTTP 429: x")
    assert hubspot_write._is_transient("HubSpot transport error: ConnectError")
    assert not hubspot_write._is_transient(
        "HubSpot read of deals 5123 failed: HTTP 404: x"
    )
    assert not hubspot_write._is_transient("HubSpot POST /x failed: HTTP 400: row 5")
    assert hubspot_write._is_rate_limited("HubSpot POST /x failed: HTTP 429: x")
    assert not hubspot_write._is_rate_limited("HubSpot POST /x failed: HTTP 503: x")


def test_the_owner_list_is_read_once_per_client():
    class _Owners:
        calls = 0

        def owner_directory(self):
            _Owners.calls += 1
            return {"77": {"email": AE}}

    client = HubSpotWriteClient(token="t", read_client=_Owners())
    assert [client.resolve_owner_id(AE.upper()) for _ in range(3)] == ["77"] * 3
    assert client.resolve_owner_id("nobody@acme.example") is None
    assert client.resolve_owner_id("nobody@acme.example") is None
    assert _Owners.calls == 2  # once per address, not once per email logged


# ── find_logged_email ────────────────────────────────────────────────────────


class _ReadClient:
    def __init__(self, rows=(), links=None, fail=False):
        # A row is an id, or (id, hs_timestamp as HubSpot's search returns it).
        self.rows = [
            {"id": r[0], "properties": {"hs_timestamp": r[1]}}
            if isinstance(r, tuple)
            else {"id": r}
            for r in rows
        ]
        self.links = links or {}
        #: True: every search fails; a list: the next searches fail with these.
        self.fail = fail
        self.attempts = 0
        self.searches: list[tuple[str, list, list]] = []
        self.assoc_calls: list[tuple[str, str, list]] = []
        self.full_proof_calls: list[tuple[str, list[str]]] = []

    def search_all(self, object_type, filters, properties, sorts=None):
        self.attempts += 1
        if self.fail is True:
            raise HubSpotAPIError("HubSpot emails search failed: HTTP 500: x")
        if self.fail:
            raise self.fail.pop(0)
        self.searches.append((object_type, filters, properties))
        return self.rows

    def batch_associations(self, from_type, to_type, ids, **_k):
        self.assoc_calls.append((from_type, to_type, list(ids)))
        if from_type == "contacts" and to_type == "deals":
            return {i: ["owned-deal"] for i in ids}
        if from_type == "companies" and to_type == "deals":
            return {i: ["owned-deal"] for i in ids}
        return {i: list(self.links.get(i, [])) for i in ids}

    def fully_owned_deal_associations(self, object_type, ids):
        assert object_type in ("contacts", "companies")
        self.full_proof_calls.append((object_type, list(ids)))
        return {i: ["owned-deal"] for i in ids}


SENT = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)


def test_the_probe_finds_an_email_logged_near_the_sent_time_on_the_contact():
    read = _ReadClient(rows=["e1", "e2"], links={"e1": ["c9"], "e2": ["c1"]})
    client = HubSpotWriteClient(token="t", read_client=read)
    found = client.find_logged_email(
        contact_ids=["c1"], subject=" Onboarding pricing ", sent_at=SENT
    )
    assert found == "e2"
    [(object_type, filters, _props)] = read.searches
    assert object_type == "emails"
    ms = int(SENT.timestamp() * 1000)
    assert filters == [
        {
            "propertyName": "hs_timestamp",
            "operator": "BETWEEN",
            "value": str(ms - 300_000),
            "highValue": str(ms + 300_000),
        },
        {
            "propertyName": "hs_email_subject",
            "operator": "EQ",
            "value": "Onboarding pricing",
        },
    ]
    assert read.full_proof_calls == [("contacts", ["c1"])]
    assert read.assoc_calls == [("emails", "contacts", ["e1", "e2"])]


def test_the_probe_misses_an_email_on_someone_elses_contact():
    read = _ReadClient(rows=["e1"], links={"e1": ["c9"]})
    client = HubSpotWriteClient(token="t", read_client=read)
    assert (
        client.find_logged_email(contact_ids=["c1"], subject="s", sent_at=SENT) is None
    )


def test_the_probe_without_a_subject_matches_on_time_and_contact_alone():
    read = _ReadClient(rows=["e1"], links={"e1": ["c1"]})
    client = HubSpotWriteClient(token="t", read_client=read)
    assert (
        client.find_logged_email(contact_ids=["c1"], subject="", sent_at=SENT) == "e1"
    )
    [(_t, filters, _p)] = read.searches
    assert [f["propertyName"] for f in filters] == ["hs_timestamp"]


def test_the_probe_with_no_contact_asks_nothing_and_errors_raise(monkeypatch):
    monkeypatch.setattr(hubspot_write.time, "sleep", lambda _s: None)
    read = _ReadClient(rows=["e1"])
    client = HubSpotWriteClient(token="t", read_client=read)
    assert client.find_logged_email(contact_ids=[], subject="s", sent_at=SENT) is None
    assert read.searches == []
    failing = _ReadClient(fail=True)
    broken = HubSpotWriteClient(token="t", read_client=failing)
    with pytest.raises(HubSpotAPIError):
        broken.find_logged_email(contact_ids=["c1"], subject="s", sent_at=SENT)
    assert failing.attempts == 4  # a 5xx is retried before it raises


def test_the_probe_backs_off_on_a_rate_limit(monkeypatch):
    """HubSpot's search limit is per second; one 429 on a busy thread must not
    turn the whole preview into "could not check"."""
    slept: list[float] = []
    monkeypatch.setattr(hubspot_write.time, "sleep", slept.append)
    read = _ReadClient(
        rows=["e1"],
        links={"e1": ["c1"]},
        fail=[HubSpotAPIError("HubSpot emails search failed: HTTP 429: x")],
    )
    client = HubSpotWriteClient(token="t", read_client=read)
    assert client.find_logged_email(contact_ids=["c1"], subject="s", sent_at=SENT) == (
        "e1"
    )
    assert read.attempts == 2 and slept == [1.0]


def test_the_closest_email_wins_and_a_claimed_one_is_skipped():
    """Cubie stamps the exact sent time, so its own write is distance 0; the
    Gmail extension's is seconds off. ISO (the search API) and epoch-ms both
    read; an unreadable stamp ranks last."""
    ms = int(SENT.timestamp() * 1000)
    read = _ReadClient(
        rows=[
            ("far", "2026-10-05T15:04:00.000Z"),
            ("unknown", "not a time"),
            ("exact", str(ms)),
            ("near", "2026-10-05T14:59:30Z"),
        ],
        links={k: ["c1"] for k in ("far", "unknown", "exact", "near")},
    )
    client = HubSpotWriteClient(token="t", read_client=read)
    candidates = client.logged_email_candidates(
        contact_ids=["c1"], subject="s", sent_at=SENT
    )
    assert [c for c, _d in candidates] == ["exact", "near", "far", "unknown"]
    assert [d for _c, d in candidates][:3] == [0, 30_000, 240_000]
    assert client.find_logged_email(contact_ids=["c1"], subject="s", sent_at=SENT) == (
        "exact"
    )
    assert (
        client.find_logged_email(
            contact_ids=["c1"], subject="s", sent_at=SENT, exclude={"exact", "near"}
        )
        == "far"
    )


# ── crm_log: reading the AE's sent emails ────────────────────────────────────


def _part(mime, text=None, *, filename="", attachment_id=None, headers=()):
    body = {}
    if text is not None:
        body["data"] = _b64(text)
    if attachment_id:
        body["attachmentId"] = attachment_id
    return {
        "mimeType": mime,
        "filename": filename,
        "headers": list(headers),
        "body": body,
    }


def _multi(mime, *parts):
    return {
        "mimeType": mime,
        "filename": "",
        "headers": [],
        "body": {},
        "parts": list(parts),
    }


def test_multipart_alternative_gives_the_plain_text_and_keeps_the_html():
    payload = _multi(
        "multipart/alternative",
        _part("text/plain", "Hi Jane,\r\nPricing below."),
        _part("text/html", "<p>Hi Jane,</p><p>Pricing below.</p>"),
    )
    text, html = crm_log.message_bodies(payload)
    assert text == "Hi Jane,\nPricing below."
    assert html == "<p>Hi Jane,</p><p>Pricing below.</p>"


def test_an_attachment_is_never_taken_for_the_body_even_a_text_one():
    payload = _multi(
        "multipart/mixed",
        # First in the tree on purpose: a .txt attachment with inline data.
        _part("text/plain", "TERMS AND CONDITIONS", filename="terms.txt"),
        _part("application/pdf", filename="quote.pdf", attachment_id="att-1"),
        _part(
            "text/plain",
            "ATTACHED NOTES",
            headers=[{"name": "Content-Disposition", "value": "attachment"}],
        ),
        _multi(
            "multipart/alternative",
            _part("text/plain", "The real body."),
            _part("text/html", "<div>The real body.</div>"),
        ),
    )
    text, html = crm_log.message_bodies(payload)
    assert text == "The real body."
    assert html == "<div>The real body.</div>"


def test_base64url_bodies_decode_without_padding():
    raw = "Hi Jane, ÿ?>~ total: $4,500?>>"
    data = _b64(raw)
    assert "_" in data or "-" in data  # really base64url, not base64
    assert not data.endswith("=")
    text, html = crm_log.message_bodies(
        {"mimeType": "text/plain", "headers": [], "body": {"data": data}}
    )
    assert text == raw and html is None


def test_an_html_only_email_gets_its_text_from_the_html():
    payload = _part(
        "text/html",
        "<html><head><style>p{color:red}</style></head>"
        "<body><p>Hi Jane,</p><p>Pricing &amp; dates.</p></body></html>",
    )
    text, html = crm_log.message_bodies(payload)
    assert "Hi Jane," in text and "Pricing & dates." in text
    assert "color:red" not in text and "<p>" not in text
    assert html.startswith("<html>")


def test_a_declared_charset_is_honoured():
    data = base64.urlsafe_b64encode("Señor".encode("latin-1")).decode()
    payload = {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}
        ],
        "body": {"data": data},
    }
    assert crm_log.message_bodies(payload)[0] == "Señor"


OPENED = datetime(2026, 10, 5, 15, 0)  # naive UTC, as loop_items holds it


def _msg(
    mid,
    *,
    sent,
    frm=f"Rita Rep <{AE}>",
    to="Jane Doe <jane@acmecorp.example>",
    cc="",
    labels=("SENT",),
    subject="Onboarding pricing",
    body=None,
    payload=None,
):
    headers = [
        {"name": "From", "value": frm},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
    ]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    if payload is None:
        payload = _part("text/plain", body if body is not None else f"Body of {mid}.")
    payload = {**payload, "headers": headers + list(payload.get("headers") or [])}
    return {
        "id": mid,
        "labelIds": list(labels),
        "internalDate": str(int(sent.replace(tzinfo=UTC).timestamp() * 1000)),
        "payload": payload,
    }


def _thread(*messages):
    return {"id": "t1", "messages": list(messages)}


def _unlogged(**overrides):
    base = dict(
        id="it1",
        owner_email=AE,
        kind="unlogged",
        source="gmail",
        status="open",
        title="Onboarding pricing",
        thread_id="t1",
        counterparty_label="Jane Doe",
        counterparty_email="jane@acmecorp.example",
        company_label="Acme Supply",
        hubspot_contact_id="c1",
        hubspot_company_id="co1",
        hubspot_deal_id="d1",
        opened_at=OPENED,
        detail={"crm_contact_name": "Jane Doe"},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def internal(set_test_workspace_domains):
    set_test_workspace_domains("acme.example")


def test_only_the_aes_own_sent_client_emails_since_the_item_opened_count(internal):
    thread = _thread(
        _msg("too-early", sent=OPENED - timedelta(minutes=5)),
        _msg("just-in", sent=OPENED - timedelta(seconds=30)),  # inside the minute
        _msg(
            "client",
            sent=OPENED + timedelta(hours=1),
            frm="Jane <jane@acmecorp.example>",
            to=f"Rita <{AE}>",
            labels=("INBOX",),
        ),
        _msg("draft", sent=OPENED + timedelta(hours=2), labels=("DRAFT", "SENT")),
        _msg("trash", sent=OPENED + timedelta(hours=2), labels=("SENT", "TRASH")),
        _msg("not-sent", sent=OPENED + timedelta(hours=2), labels=("INBOX",)),
        _msg(
            "internal",
            sent=OPENED + timedelta(hours=3),
            to="Col League <col@acme.example>",
        ),
        _msg(
            "cc-client",
            sent=OPENED + timedelta(hours=4),
            to="col@acme.example",
            cc="Bob <BOB@Beta.com>",
        ),
        _msg(
            "other-sender",
            sent=OPENED + timedelta(hours=5),
            frm="Col <col@acme.example>",
        ),
    )
    emails = crm_log.emails_in_thread(thread, _unlogged(), owner_email=AE.upper())
    assert [e.message_id for e in emails] == ["just-in", "cc-client"]
    cc_client = emails[1]
    assert cc_client.to == ["col@acme.example"] and cc_client.cc == ["bob@beta.com"]
    assert cc_client.from_email == AE and cc_client.from_name == "Rita Rep"
    assert cc_client.sent_at.tzinfo is not None
    # Only the email that went to the item's own contact starts ticked.
    assert [e.to_contact for e in emails] == [True, False]


def test_a_forward_to_a_personal_mailbox_or_a_robot_is_never_loggable(internal):
    """Gmail keeps a forward in the sender's conversation. One to a personal
    address (the AE's own included) or to a robot never makes an email
    loggable on the client's record; one to another business address is
    loggable but starts unticked; the contact's own counts on any address."""
    thread = _thread(
        _msg("to-jane", sent=OPENED, to="Jane <jane@acmecorp.example>"),
        _msg("fwd-gmail", sent=OPENED + timedelta(hours=1), to="me <rita@gmail.com>"),
        _msg(
            "fwd-icloud-cc",
            sent=OPENED + timedelta(hours=2),
            to="col@acme.example",
            cc="someone@icloud.com",
        ),
        _msg("to-robot", sent=OPENED + timedelta(hours=3), to="noreply@vendor.com"),
        _msg("fwd-vendor", sent=OPENED + timedelta(hours=4), to="bob@vendor.com"),
    )
    emails = crm_log.emails_in_thread(thread, _unlogged(), owner_email=AE)
    assert [(e.message_id, e.to_contact) for e in emails] == [
        ("to-jane", True),
        ("fwd-vendor", False),
    ]
    # A client who writes from a personal address is still the contact.
    personal = _unlogged(counterparty_email="jane.doe@gmail.com")
    thread = _thread(
        _msg("to-jane", sent=OPENED, to="jane.doe@gmail.com"),
        _msg("fwd-gmail", sent=OPENED + timedelta(hours=1), to="rita@gmail.com"),
    )
    emails = crm_log.emails_in_thread(thread, personal, owner_email=AE)
    assert [(e.message_id, e.to_contact) for e in emails] == [("to-jane", True)]


def test_an_internal_looking_counterparty_still_counts(internal):
    # The scan matched this person to a HubSpot contact; their address decides.
    thread = _thread(_msg("m1", sent=OPENED, to="partner@acme.example"))
    item = _unlogged(counterparty_email="partner@acme.example")
    assert [
        e.message_id for e in crm_log.emails_in_thread(thread, item, owner_email=AE)
    ] == ["m1"]


def test_a_huge_body_is_cut_and_says_so(internal):
    # HubSpot caps a text property at 65,536 characters.
    assert crm_log.MAX_BODY_CHARS < 65_536
    thread = _thread(_msg("m1", sent=OPENED, body="x" * (crm_log.MAX_BODY_CHARS + 50)))
    [email] = crm_log.emails_in_thread(thread, _unlogged(), owner_email=AE)
    assert len(email.text) == crm_log.MAX_BODY_CHARS and email.truncated is True
    assert email.plain_text_only is False


def test_html_too_long_for_hubspot_is_left_out_never_cut_mid_tag(internal):
    """Quoted history in an Outlook-styled reply grows the HTML past the limit
    long before the text: the email is logged as its full text, unformatted,
    and the text is not marked truncated because none of it was cut."""
    html = "<div style='x'>" + "<p>quoted</p>" * 6_000 + "</div>"
    assert len(html) > crm_log.MAX_BODY_CHARS
    payload = _multi(
        "multipart/alternative",
        _part("text/plain", "Short reply."),
        _part("text/html", html),
    )
    thread = _thread(_msg("m1", sent=OPENED, payload=payload))
    [email] = crm_log.emails_in_thread(thread, _unlogged(), owner_email=AE)
    assert email.html is None and email.plain_text_only is True
    assert email.text == "Short reply." and email.truncated is False
    props = crm_log.email_properties(email)
    assert "hs_email_html" not in props
    assert all(len(v) <= crm_log.MAX_BODY_CHARS for v in props.values())


# ── crm_log: the preview and the write ───────────────────────────────────────


class _Mailbox:
    def __init__(self, thread=None, fail=False):
        self.thread = thread or _thread()
        self.fail = fail
        self.reads: list[str] = []

    def get_thread(self, thread_id):
        if self.fail:
            raise RuntimeError("delegation failed")
        self.reads.append(thread_id)
        return self.thread


class _EmailClient:
    """A write client double: the probe, the deal read and ``create_email``."""

    def __init__(
        self,
        *,
        logged=None,
        hubspot=(),
        fail_on=None,
        refuse_deal=False,
        deal_gone=False,
        probe_error=False,
        pipeline="default",
    ):
        self.logged = logged or {}  # message subject → HubSpot id already there
        #: HubSpot emails as (id, subject, stamped at): matched like the real
        #: probe, by subject within five minutes, closest first.
        self.hubspot = list(hubspot)
        self.fail_on = fail_on
        self.refuse_deal = refuse_deal
        self.deal_gone = deal_gone
        self.probe_error = probe_error
        self.pipeline = pipeline
        self.probes: list[tuple] = []
        self.calls: list[dict] = []

    def logged_email_candidates(self, *, contact_ids, subject, sent_at):
        if self.probe_error:
            raise HubSpotAPIError("HubSpot emails search failed: HTTP 500: x")
        self.probes.append((tuple(contact_ids), subject, sent_at))
        hits = [(self.logged[subject], 0)] if subject in self.logged else []
        for hubspot_id, hs_subject, stamped in self.hubspot:
            apart = abs(int((stamped - sent_at).total_seconds() * 1000))
            if hs_subject == subject and apart <= 300_000:
                hits.append((hubspot_id, apart))
        return sorted(hits, key=lambda hit: hit[1])

    def get_object(self, object_type, object_id, properties):
        return {
            "properties": {"pipeline": self.pipeline, "dealname": "Onboarding 2026"}
        }

    def create_email(
        self, properties, *, owner_email, contact_ids, company_id=None, deal_id=None
    ):
        call = {
            "properties": properties,
            "owner_email": owner_email,
            "contact_ids": list(contact_ids),
            "company_id": company_id,
            "deal_id": deal_id,
        }
        self.calls.append(call)
        if self.refuse_deal and deal_id:
            raise HubSpotPipelineRefused("Deal writes are not allowed in pipeline 'x'.")
        if self.deal_gone and deal_id:
            raise HubSpotDealGone("That deal no longer exists in HubSpot.")
        if self.fail_on and properties["hs_email_subject"] == self.fail_on:
            raise HubSpotWriteError(
                "HubSpot POST /crm/v3/objects/emails failed: HTTP 403: x"
            )
        return {"id": f"h-{properties['hs_email_subject']}", "_mode": "created"}


def _two_emails():
    return _thread(
        _msg(
            "m1",
            sent=OPENED,
            subject="First",
            cc="Bob <bob@beta.com>",
            payload=_multi(
                "multipart/alternative",
                _part("text/plain", "Plain one."),
                _part("text/html", "<b>One</b>"),
            ),
        ),
        _msg("m2", sent=OPENED + timedelta(hours=1), subject="Second"),
    )


def test_the_preview_shows_each_email_as_it_will_be_logged(internal, monkeypatch):
    long_body = "y" * 900
    thread = _thread(
        _msg("m1", sent=OPENED, subject="First", body=long_body),
        _msg("m2", sent=OPENED + timedelta(hours=1), subject="Second"),
    )
    client = _EmailClient(logged={"Second": "h-old"})
    out = crm_log.log_preview(
        _unlogged(), owner_email=AE, gmail=_Mailbox(thread), read_client=client
    )
    assert set(out) == {"emails", "contact", "company", "deal", "hubspot_url"}
    first, second = out["emails"]
    assert first == {
        "message_id": "m1",
        "subject": "First",
        "to": ["jane@acmecorp.example"],
        "cc": [],
        "sent_at": "2026-10-05T15:00:00",  # naive UTC
        "excerpt": "y" * crm_log.EXCERPT_CHARS,
        "chars": 900,
        "truncated": False,
        "plain_text_only": False,
        "to_contact": True,
        "already_logged": False,
    }
    assert second["already_logged"] is True
    assert out["contact"] == "Jane Doe" and out["company"] == "Acme Supply"
    assert out["deal"] == "Onboarding 2026"
    assert out["hubspot_url"] == "https://app.hubspot.com/contacts/9100001/contact/c1"
    # Probed against the item's contact, once per email.
    assert [p[:2] for p in client.probes] == [(("c1",), "First"), (("c1",), "Second")]


def test_the_preview_names_no_deal_outside_the_companys_pipelines(
    internal, monkeypatch
):
    out = crm_log.log_preview(
        _unlogged(),
        owner_email=AE,
        gmail=_Mailbox(_two_emails()),
        read_client=_EmailClient(pipeline="initech"),
    )
    assert out["deal"] is None


@pytest.mark.parametrize(
    "item,owner,match",
    [
        (_unlogged(hubspot_contact_id=None), AE, "Add the contact in HubSpot first"),
        (_unlogged(), "other@acme.example", "someone else"),
        (_unlogged(source="slack"), AE, "open the record in HubSpot"),
        (_unlogged(kind="open_loop"), AE, "not in HubSpot"),
        (_unlogged(status="closed"), AE, "already closed"),
        (_unlogged(thread_id=None), AE, "which email thread"),
    ],
    ids=["no-contact", "not-yours", "slack", "open-loop", "closed", "no-thread"],
)
def test_an_item_that_cannot_be_logged_is_a_sentence_before_any_read(
    item, owner, match
):
    mailbox = _Mailbox(_two_emails())
    with pytest.raises(ValueError, match=match):
        crm_log.loggable_emails(
            item, owner_email=owner, gmail=mailbox, read_client=_EmailClient()
        )
    assert mailbox.reads == []


def test_an_unreadable_mailbox_or_hubspot_is_a_log_error(internal):
    with pytest.raises(crm_log.LogError, match="mailbox"):
        crm_log.loggable_emails(
            _unlogged(),
            owner_email=AE,
            gmail=_Mailbox(fail=True),
            read_client=_EmailClient(),
        )
    with pytest.raises(crm_log.LogError, match="HubSpot"):
        crm_log.loggable_emails(
            _unlogged(),
            owner_email=AE,
            gmail=_Mailbox(_two_emails()),
            read_client=_EmailClient(probe_error=True),
        )


def test_log_writes_each_email_verbatim_as_an_outgoing_hubspot_email(
    internal, monkeypatch
):
    client = _EmailClient()
    mailbox = _Mailbox(_two_emails())
    out = crm_log.log_emails(
        _unlogged(),
        owner_email=AE,
        message_ids=["m1", "m2"],
        gmail=mailbox,
        write_client=client,
    )
    assert out == {
        "logged": [
            {"message_id": "m1", "hubspot_id": "h-First", "mode": "created"},
            {"message_id": "m2", "hubspot_id": "h-Second", "mode": "created"},
        ],
        "hubspot_url": "https://app.hubspot.com/contacts/9100001/contact/c1",
    }
    assert mailbox.reads == ["t1"]  # re-read server-side, never trusted from the page
    first = client.calls[0]
    assert first["owner_email"] == AE
    assert first["contact_ids"] == ["c1"]
    assert (first["company_id"], first["deal_id"]) == ("co1", "d1")
    props = first["properties"]
    assert props["hs_timestamp"] == str(
        int(OPENED.replace(tzinfo=UTC).timestamp() * 1000)
    )
    assert props["hs_email_direction"] == "EMAIL"
    assert props["hs_email_status"] == "SENT"
    assert props["hs_email_subject"] == "First"
    assert props["hs_email_text"] == "Plain one."
    assert props["hs_email_html"] == "<b>One</b>"
    assert "hubspot_owner_id" not in props  # create_email stamps it
    assert json.loads(props["hs_email_headers"]) == {
        "from": {"email": AE, "firstName": "Rita", "lastName": "Rep"},
        "to": [{"email": "jane@acmecorp.example"}],
        "cc": [{"email": "bob@beta.com"}],
    }
    assert "hs_email_html" not in client.calls[1]["properties"]  # plain-only email
    from rubik_core.retrieval.hubspot_write import EMAIL_WRITABLE

    for call in client.calls:
        assert set(call["properties"]) <= EMAIL_WRITABLE


def test_log_refuses_an_email_it_did_not_read_itself(internal):
    client = _EmailClient()
    with pytest.raises(ValueError, match="not one Cubie can log"):
        crm_log.log_emails(
            _unlogged(),
            owner_email=AE,
            message_ids=["m1", "forged"],
            gmail=_Mailbox(_two_emails()),
            write_client=client,
        )
    assert client.calls == []
    with pytest.raises(ValueError, match="at least one"):
        crm_log.log_emails(
            _unlogged(),
            owner_email=AE,
            message_ids=[" "],
            gmail=_Mailbox(_two_emails()),
            write_client=client,
        )


def test_an_email_hubspot_already_has_is_counted_and_not_written_again(internal):
    client = _EmailClient(logged={"First": "h-extension"})
    out = crm_log.log_emails(
        _unlogged(),
        owner_email=AE,
        message_ids=["m1", "m2"],
        gmail=_Mailbox(_two_emails()),
        write_client=client,
    )
    assert out["logged"] == [
        {"message_id": "m1", "hubspot_id": "h-extension", "mode": "already_exists"},
        {"message_id": "m2", "hubspot_id": "h-Second", "mode": "created"},
    ]
    assert [c["properties"]["hs_email_subject"] for c in client.calls] == ["Second"]


def test_a_deal_in_a_client_pipeline_drops_the_deal_not_the_log(internal):
    client = _EmailClient(refuse_deal=True)
    out = crm_log.log_emails(
        _unlogged(),
        owner_email=AE,
        message_ids=["m1", "m2"],
        gmail=_Mailbox(_two_emails()),
        write_client=client,
    )
    assert [r["mode"] for r in out["logged"]] == ["created", "created"]
    # One refused try with the deal, its retry without, then no deal at all.
    assert [c["deal_id"] for c in client.calls] == ["d1", None, None]
    assert all(c["company_id"] == "co1" for c in client.calls)


def test_a_failure_partway_raises_what_was_written(internal):
    client = _EmailClient(fail_on="Second")
    with pytest.raises(crm_log.PartialLogError) as raised:
        crm_log.log_emails(
            _unlogged(),
            owner_email=AE,
            message_ids=["m1", "m2"],
            gmail=_Mailbox(_two_emails()),
            write_client=client,
        )
    assert [c["properties"]["hs_email_subject"] for c in client.calls] == [
        "First",
        "Second",
    ]
    assert raised.value.created == [{"message_id": "m1", "hubspot_id": "h-First"}]
    assert isinstance(raised.value.cause, HubSpotWriteError)
    assert "First" not in str(raised.value)  # counts only, never a subject


def test_a_quick_retry_skips_what_the_app_wrote_even_before_search_shows_it(internal):
    """HubSpot's search lags a fresh write: right after a failure partway the
    probe does NOT find the first email. The ids the router stored on the item
    (``detail[CRM_LOGGED_DETAIL_KEY]``) do, so it is not written twice."""
    item = _unlogged(
        detail={
            "crm_contact_name": "Jane Doe",
            CRM_LOGGED_DETAIL_KEY: {"m1": "h-First"},
        }
    )
    retry = _EmailClient()  # the search has not caught up: finds nothing
    out = crm_log.log_emails(
        item,
        owner_email=AE,
        message_ids=["m1", "m2"],
        gmail=_Mailbox(_two_emails()),
        write_client=retry,
    )
    assert out["logged"][0] == {
        "message_id": "m1",
        "hubspot_id": "h-First",
        "mode": "already_exists",
    }
    assert [r["mode"] for r in out["logged"]] == ["already_exists", "created"]
    assert [c["properties"]["hs_email_subject"] for c in retry.calls] == ["Second"]
    assert [p[1] for p in retry.probes] == ["Second"]  # no probe for the known one
    # And a failure before anything was written is the plain error.
    with pytest.raises(HubSpotWriteError) as raised:
        crm_log.log_emails(
            _unlogged(),
            owner_email=AE,
            message_ids=["m1"],
            gmail=_Mailbox(_two_emails()),
            write_client=_EmailClient(fail_on="First"),
        )
    assert not isinstance(raised.value, crm_log.PartialLogError)


def _resend_thread():
    """The "forgot the attachment" resend: same subject, three minutes on."""
    return _thread(
        _msg("a", sent=OPENED, subject="Re: Proposal"),
        _msg("b", sent=OPENED + timedelta(minutes=3), subject="Re: Proposal"),
    )


@pytest.mark.parametrize(
    "in_hubspot,expected",
    [
        ({"a": 0}, {"a": "HS-a", "b": None}),
        ({"b": 3}, {"a": None, "b": "HS-b"}),
        ({"a": 0, "b": 3}, {"a": "HS-a", "b": "HS-b"}),
        ({}, {"a": None, "b": None}),
    ],
    ids=["only-first", "only-resend", "both", "neither"],
)
def test_one_hubspot_email_never_stands_for_two_sent_ones(
    internal, in_hubspot, expected
):
    """Each sent email's window holds the other's HubSpot email. Matching one
    to one, closest first, keeps the one HubSpot already has from hiding the
    one it does not."""
    stamped = {
        k: (OPENED + timedelta(minutes=m)).replace(tzinfo=UTC)
        for k, m in in_hubspot.items()
    }
    client = _EmailClient(
        hubspot=[(f"HS-{k}", "Re: Proposal", at) for k, at in stamped.items()]
    )
    emails = crm_log.loggable_emails(
        _unlogged(),
        owner_email=AE,
        gmail=_Mailbox(_resend_thread()),
        read_client=client,
    )
    assert {e.message_id: e.hubspot_id for e in emails} == expected
    assert {e.message_id: e.already_logged for e in emails} == {
        k: v is not None for k, v in expected.items()
    }


def test_the_resend_is_written_when_only_the_first_is_in_hubspot(internal):
    client = _EmailClient(
        hubspot=[("HS-a", "Re: Proposal", OPENED.replace(tzinfo=UTC))]
    )
    out = crm_log.log_emails(
        _unlogged(),
        owner_email=AE,
        message_ids=["a", "b"],
        gmail=_Mailbox(_resend_thread()),
        write_client=client,
    )
    assert [(r["message_id"], r["mode"]) for r in out["logged"]] == [
        ("a", "already_exists"),
        ("b", "created"),
    ]
    assert len(client.calls) == 1


def test_a_deal_deleted_since_the_scan_drops_the_deal_not_the_log(internal):
    client = _EmailClient(deal_gone=True)
    out = crm_log.log_emails(
        _unlogged(),
        owner_email=AE,
        message_ids=["m1", "m2"],
        gmail=_Mailbox(_two_emails()),
        write_client=client,
    )
    assert [r["mode"] for r in out["logged"]] == ["created", "created"]
    assert [c["deal_id"] for c in client.calls] == ["d1", None, None]
    assert all(c["contact_ids"] == ["c1"] for c in client.calls)


def test_nothing_from_an_email_reaches_the_logs(internal, caplog):
    client = _EmailClient(fail_on="First")
    with caplog.at_level(logging.INFO), pytest.raises(HubSpotWriteError):
        crm_log.log_emails(
            _unlogged(),
            owner_email=AE,
            message_ids=["m1"],
            gmail=_Mailbox(_two_emails()),
            write_client=client,
        )
    for secret in ("First", "Plain one", "jane@acmecorp.example", "bob@beta.com"):
        assert secret not in caplog.text


# ── drafts ───────────────────────────────────────────────────────────────────


class _Gmail:
    def __init__(self, *, fail_read=False, fail_draft=False, extra_headers=()):
        self.fail_read = fail_read
        self.fail_draft = fail_draft
        self.extra_headers = list(extra_headers)
        self.drafts = []
        self.threading: list[dict] = []

    def get_thread(self, thread_id):
        if self.fail_read:
            raise RuntimeError("403")
        return {
            "id": thread_id,
            "messages": [
                {
                    "snippet": "Hi Rita",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {
                                "name": "From",
                                "value": "Jane Doe <jane@acmecorp.example>",
                            },
                            {"name": "Subject", "value": "Pricing question"},
                            {"name": "Date", "value": "Mon, 5 Oct 2026 10:00:00 -0500"},
                            *self.extra_headers,
                        ],
                        "body": {
                            "data": _b64(
                                "Can you send pricing by Friday?\n\n"
                                "On Fri, Oct 2, 2026 at 9:00 AM Rita wrote:\n> old"
                            )
                        },
                    },
                }
            ],
        }

    def create_draft(
        self,
        to,
        subject,
        body_markdown,
        cc=None,
        thread_id=None,
        attachments=None,
        **threading,
    ):
        if self.fail_draft:
            raise RuntimeError("compose scope")
        self.drafts.append((to, subject, body_markdown, thread_id))
        self.threading.append(threading)
        return {"draft_id": "r-1", "gmail_draft_url": "https://mail.google.com/d/r-1"}


def _loop(**overrides):
    base = dict(
        owner_email=AE,
        kind="open_loop",
        thread_id="t1",
        counterparty_email="jane@acmecorp.example",
        counterparty_label="Jane Doe",
        title="Pricing question",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_draft_reply_creates_a_draft_in_the_thread():
    gmail = _Gmail()
    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        return '{"body": "Hi Jane,\\n\\nHere is the pricing [confirm].\\n\\nBest,"}'

    out = drafts.draft_reply(_loop(), owner_email=AE, gmail=gmail, invoke=invoke)
    assert out == {"draft_id": "r-1", "draft_url": "https://mail.google.com/d/r-1"}
    [(to, subject, body, thread_id)] = gmail.drafts
    assert to == ["jane@acmecorp.example"] and thread_id == "t1"
    assert subject == "Re: Pricing question"
    assert body.startswith("Hi Jane,")
    # The model saw the new words, not the quoted history.
    assert "Can you send pricing by Friday?" in prompts[0]
    assert "> old" not in prompts[0]


@pytest.mark.parametrize(
    "gmail,invoke",
    [
        (_Gmail(fail_read=True), lambda p: '{"body": "x"}'),
        (_Gmail(), lambda p: '{"body": ""}'),
        (_Gmail(), lambda p: (_ for _ in ()).throw(RuntimeError("vertex"))),
        (_Gmail(fail_draft=True), lambda p: '{"body": "Hi"}'),
    ],
    ids=["thread-unreadable", "empty-body", "model-failed", "gmail-refused"],
)
def test_every_draft_failure_is_a_sentence(gmail, invoke):
    with pytest.raises(drafts.DraftError) as err:
        drafts.draft_reply(_loop(), owner_email=AE, gmail=gmail, invoke=invoke)
    assert str(err.value).endswith(".")
    assert gmail.drafts == []


def test_the_draft_is_a_real_reply_to_the_clients_message():
    gmail = _Gmail(
        extra_headers=[
            {"name": "Message-ID", "value": "<jane-2@acmecorp.example>"},
            {"name": "References", "value": "<rita-1@acme.example>"},
        ]
    )
    drafts.draft_reply(
        _loop(), owner_email=AE, gmail=gmail, invoke=lambda p: '{"body": "Hi Jane,"}'
    )
    [threading] = gmail.threading
    assert threading == {
        "in_reply_to": "<jane-2@acmecorp.example>",
        "references": "<rita-1@acme.example> <jane-2@acmecorp.example>",
    }
    assert gmail.drafts[0][1] == "Re: Pricing question"  # never "Re: Re:"


def test_a_thread_without_a_message_id_drafts_exactly_as_before():
    gmail = _Gmail()
    drafts.draft_reply(
        _loop(), owner_email=AE, gmail=gmail, invoke=lambda p: '{"body": "Hi"}'
    )
    assert gmail.threading == [{}]  # no threading kwargs passed at all


def _client_with_service(service):
    from unittest.mock import patch

    from rubik_core.tools.gmail import GmailClient

    with patch.object(GmailClient, "_build_service", return_value=service):
        client = GmailClient(AE)
    client.service = service
    return client


def test_gmail_create_draft_sets_the_threading_headers():
    from unittest.mock import MagicMock

    service = MagicMock()
    users = service.users.return_value
    users.settings.return_value.sendAs.return_value.list.return_value.execute.return_value = {
        "sendAs": []
    }
    create = users.drafts.return_value.create
    create.return_value.execute.return_value = {"id": "d9"}
    client = _client_with_service(service)

    client.create_draft(
        to=["jane@acmecorp.example"],
        subject="Re: Pricing question",
        body_markdown="Hi Jane,",
        thread_id="t1",
        in_reply_to="<jane-2@acmecorp.example>",
        references="<rita-1@acme.example>  <jane-2@acmecorp.example>",
    )
    body = create.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(body["message"]["raw"]).decode()
    assert "In-Reply-To: <jane-2@acmecorp.example>" in raw
    assert "References: <rita-1@acme.example> <jane-2@acmecorp.example>" in raw
    assert body["message"]["threadId"] == "t1"

    # Existing callers, unchanged: no threading headers at all.
    client.create_draft(to=["a@b.com"], subject="s", body_markdown="Body")
    raw = base64.urlsafe_b64decode(
        create.call_args.kwargs["body"]["message"]["raw"]
    ).decode()
    assert "In-Reply-To" not in raw and "References" not in raw


def test_gmail_lists_one_page_of_threads_with_its_next_token():
    from unittest.mock import MagicMock

    service = MagicMock()
    listing = service.users.return_value.threads.return_value.list
    listing.return_value.execute.return_value = {
        "threads": [{"id": "a"}, {"id": "b"}],
        "nextPageToken": "tok2",
    }
    client = _client_with_service(service)
    assert client.list_thread_page("in:inbox", max_results=900, page_token="tok1") == (
        ["a", "b"],
        "tok2",
    )
    assert listing.call_args.kwargs == {
        "userId": "me",
        "q": "in:inbox",
        "maxResults": 500,  # Gmail's page ceiling
        "pageToken": "tok1",
    }


def test_draft_refuses_the_wrong_item():
    with pytest.raises(drafts.DraftError):
        drafts.draft_reply(_loop(kind="unlogged"), owner_email=AE, gmail=_Gmail())
    with pytest.raises(drafts.DraftError):
        drafts.draft_reply(_loop(), owner_email="other@acme.example", gmail=_Gmail())


# ── the judge ────────────────────────────────────────────────────────────────


def test_the_judge_answers_only_for_tags_it_was_handed():
    raw = (
        '```json\n{"emails": ['
        '{"tag": "E1", "expects_reply": true, "business_request": false, '
        '"headline": "' + "x" * 200 + '"},'
        '{"tag": "E1", "expects_reply": false},'
        '{"tag": "E9", "expects_reply": true}],'
        '"slack": [{"tag": "s1", "reports_client_touch": "true", "headline": "**Call**"}]}\n```'
    )
    result = judge.parse_verdicts(raw, {"E1", "E2"}, {"S1"})
    assert set(result.emails) == {"E1"}
    assert result.emails["E1"].expects_reply is True
    assert len(result.emails["E1"].headline) == judge.HEADLINE_CHARS
    assert result.slack["S1"].reports_client_touch is True
    assert result.slack["S1"].headline == "Call"


def test_a_reply_wrapped_in_a_list_is_unwrapped():
    raw = '[{"emails": [{"tag": "E1", "expects_reply": true, "headline": "x"}]}]'
    result = judge.parse_verdicts(raw, {"E1"}, set())
    assert result.emails["E1"].expects_reply is True


@pytest.mark.parametrize(
    "reply",
    ['{"items": [{"tag": "E1", "expects_reply": true}]}', "[]", '{"emails": []}'],
    ids=["wrong-keys", "bare-list", "no-verdicts"],
)
def test_a_reply_with_no_usable_verdict_is_a_failed_call(reply):
    email = judge.EmailInput(
        "E1", "Jane", "acmecorp.example", "HubSpot contact", "Hi", ()
    )
    # Not "everything declined" with ai: ok — could not judge is a coverage state.
    assert judge.judge([email], [], invoke=lambda p: reply) is None


def test_a_failed_judge_call_is_none_and_no_question_is_no_call():
    calls = []

    def boom(prompt):
        calls.append(prompt)
        raise RuntimeError("429")

    email = judge.EmailInput(
        "E1", "Jane", "acmecorp.example", "HubSpot contact", "Hi", ()
    )
    assert judge.judge([email], [], invoke=boom) is None
    assert len(calls) == 1
    assert judge.judge([], [], invoke=boom) == judge.JudgeResult()
    assert len(calls) == 1


def _selected_loose_ends_route() -> ModelRoute:
    return ModelRoute("acme", "loop_closure", "meta", "muse-spark-1.3", "client")


def test_judge_uses_selected_workspace_json_route_without_vertex(monkeypatch):
    route = _selected_loose_ends_route()
    seen = []
    monkeypatch.setattr(judge, "resolve_model_route", lambda *a, **kw: route)
    monkeypatch.setattr(
        judge,
        "generate_text_on_route",
        lambda selected, prompt, **kw: (
            seen.append((selected, prompt, kw))
            or SimpleNamespace(
                text='{"emails":[{"tag":"E1","expects_reply":true,"business_request":true,"headline":"Needs pricing"}]}'
            )
        ),
    )
    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("selected judge fell back to managed Vertex"),
    )
    email = judge.EmailInput(
        "E1", "Jane", "acmecorp.example", "HubSpot contact", "Pricing", ()
    )

    result = judge.judge([email], [])

    assert result is not None
    assert result.emails["E1"].expects_reply is True
    assert seen[0][0] == route
    assert seen[0][2] == {"json_mode": True, "temperature": 0}


def test_judge_selected_provider_failure_is_unavailable_without_leak(
    monkeypatch, caplog
):
    route = _selected_loose_ends_route()
    monkeypatch.setattr(judge, "resolve_model_route", lambda *a, **kw: route)

    def fail(*a, **kw):
        raise ProviderRequestError("PRIVATE CLIENT MAIL AND KEY")

    monkeypatch.setattr(judge, "generate_text_on_route", fail)
    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("selected judge fell back to managed Vertex"),
    )
    email = judge.EmailInput(
        "E1", "Jane", "acmecorp.example", "HubSpot contact", "Pricing", ()
    )

    assert judge.judge([email], []) is None
    assert "PRIVATE CLIENT MAIL AND KEY" not in caplog.text


def test_judge_managed_route_uses_selected_model_and_refuses_switch(monkeypatch):
    managed = ModelRoute(
        "acme", "loop_closure", "vertex_managed", "gemini-2.5-pro", "cubie"
    )
    calls = []
    monkeypatch.setattr(judge, "resolve_model_route", lambda *a, **kw: managed)
    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kw: (
                    calls.append(kw)
                    or SimpleNamespace(
                        text='{"emails":[{"tag":"E1","expects_reply":true,"headline":"Needs pricing"}]}'
                    )
                )
            )
        ),
    )
    email = judge.EmailInput(
        "E1", "Jane", "acmecorp.example", "HubSpot contact", "Pricing", ()
    )

    assert judge.judge([email], []) is not None
    assert calls[0]["model"] == managed.model

    selected = _selected_loose_ends_route()
    routes = iter((managed, selected))
    monkeypatch.setattr(judge, "resolve_model_route", lambda *a, **kw: next(routes))
    calls.clear()
    assert judge.judge([email], []) is None
    assert calls == []


def test_draft_uses_selected_workspace_json_route_without_vertex(monkeypatch):
    route = _selected_loose_ends_route()
    seen = []
    monkeypatch.setattr(drafts, "resolve_model_route", lambda *a, **kw: route)
    monkeypatch.setattr(
        drafts,
        "generate_text_on_route",
        lambda selected, prompt, **kw: (
            seen.append((selected, prompt, kw))
            or SimpleNamespace(text='{"body":"Hi Jane, I will check pricing."}')
        ),
    )
    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("selected draft fell back to managed Vertex"),
    )
    gmail = _Gmail()

    result = drafts.draft_reply(_loop(), owner_email=AE, gmail=gmail)

    assert result["draft_id"] == "r-1"
    assert gmail.drafts[0][2] == "Hi Jane, I will check pricing."
    assert seen[0][0] == route
    assert seen[0][2] == {"json_mode": True, "temperature": 0.2}


def test_draft_managed_route_uses_selected_model(monkeypatch):
    managed = ModelRoute(
        "acme", "loop_closure", "vertex_managed", "gemini-2.5-pro", "cubie"
    )
    calls = []
    monkeypatch.setattr(drafts, "resolve_model_route", lambda *a, **kw: managed)
    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kw: (
                    calls.append(kw)
                    or SimpleNamespace(text='{"body":"Hi Jane, I will check pricing."}')
                )
            )
        ),
    )
    gmail = _Gmail()

    drafts.draft_reply(_loop(), owner_email=AE, gmail=gmail)

    assert calls[0]["model"] == managed.model
    assert gmail.drafts[0][2] == "Hi Jane, I will check pricing."


def test_draft_rejects_invalid_selected_json_without_creating_draft(
    monkeypatch, caplog
):
    route = _selected_loose_ends_route()
    monkeypatch.setattr(drafts, "resolve_model_route", lambda *a, **kw: route)
    monkeypatch.setattr(
        drafts,
        "generate_text_on_route",
        lambda *a, **kw: SimpleNamespace(text="PRIVATE CLIENT MAIL, not JSON"),
    )
    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("selected draft fell back to managed Vertex"),
    )
    gmail = _Gmail()

    with pytest.raises(drafts.DraftError, match="selected AI provider") as error:
        drafts.draft_reply(_loop(), owner_email=AE, gmail=gmail)

    assert gmail.drafts == []
    assert "PRIVATE CLIENT MAIL" not in str(error.value)
    assert "PRIVATE CLIENT MAIL" not in caplog.text


# ── AI Cost attribution ──────────────────────────────────────────────────────


def test_both_model_calls_are_billed_to_loose_ends():
    from backend.routers.admin import _FEATURE_LABELS
    from rubik_core.cost.usage import current_feature
    from rubik_core.loop_closure.constants import COST_FEATURE

    assert COST_FEATURE == "loop_closure"
    assert _FEATURE_LABELS[COST_FEATURE] == "Loose Ends (morning scan + reply drafts)"

    seen: list[str] = []

    def spy(prompt):
        seen.append(current_feature.get())
        return '{"emails": [], "slack": [], "body": "Hi Jane,"}'

    email = judge.EmailInput(
        "E1", "Jane", "acmecorp.example", "HubSpot contact", "Hi", ()
    )
    judge.judge([email], [], invoke=spy)
    drafts.draft_reply(_loop(), owner_email=AE, gmail=_Gmail(), invoke=spy)
    assert seen == [COST_FEATURE, COST_FEATURE]
