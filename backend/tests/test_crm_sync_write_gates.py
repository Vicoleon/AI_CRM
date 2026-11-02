"""UC-46 — the HubSpot write layer's hard gates.

These are the checks that hold when everything upstream has failed: a bad
suggestion got past validation, or a future caller wires the write client up
directly. Every test asserts a refusal AND that no mutating HTTP call was made.

Covers:
  - deal writes into the client (Initech) pipeline → refused
  - empty pipeline allowlist → protected reads and writes refused with an
    admin-setup reason
  - deal create without an explicit pipeline → refused
  - owner mismatch / caller is not a HubSpot owner → refused
  - non-whitelisted property → refused even bypassing the validators
  - missing write token → refused before any HTTP
  - backoff retries 429/5xx and gives up honestly
  - idempotency probes stop a create from duplicating
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from rubik_core.retrieval import hubspot_write
from rubik_core.retrieval.hubspot_write import (
    MODE_ALREADY_EXISTS,
    MODE_EXISTING_UPDATED,
    PIPELINES_NOT_SET_UP,
    HubSpotPipelineRefused,
    HubSpotWriteClient,
    HubSpotWriteError,
    HubSpotWriteRefused,
    friendly_write_error,
)
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

OWN_PIPELINE = "default"
INITECH_PIPELINE = "9100004"
AE = "ae@acme.example"
AE_OWNER_ID = "77"

_STAGES = {
    OWN_PIPELINE: {
        "appointmentscheduled": "Discovery",
        "presentationscheduled": "Proposal",
    },
    INITECH_PIPELINE: {"clientstage": "Client Stage"},
}


def _read_client(*, owner_email: str = AE, owner_id: str = AE_OWNER_ID) -> MagicMock:
    client = MagicMock()
    client.owner_directory.return_value = {
        owner_id: {"email": owner_email, "name": "A E"}
    }
    client.deal_pipeline_stages.return_value = _STAGES
    client.search_all.return_value = []
    client._headers.return_value = {"Authorization": "Bearer read"}
    client.fully_owned_deal_associations.side_effect = lambda _from_type, ids: {
        str(i): ["owned-deal"] for i in ids
    }
    return client


def _writer(read_client=None, token: str = "write-token") -> HubSpotWriteClient:
    return HubSpotWriteClient(token=token, read_client=read_client or _read_client())


@pytest.fixture(autouse=True)
def workspace_config(monkeypatch):
    from rubik_core.crm_sync import hubspot_context

    current = HubSpotConnectorConfig()

    def get_config():
        return current

    monkeypatch.setattr(hubspot_write, "get_hubspot_config", get_config)
    monkeypatch.setattr(hubspot_context, "get_hubspot_config", get_config)

    def configure(**changes):
        nonlocal current
        current = replace(current, **changes)
        return current

    return configure


@pytest.fixture
def allowlist(workspace_config):
    """The company's own pipelines only — Initech deliberately excluded."""
    workspace_config(pipeline_ids=(OWN_PIPELINE, "9100002"))


def _live_deal(pipeline=OWN_PIPELINE, owner=AE_OWNER_ID, stage="appointmentscheduled"):
    return {
        "id": "555",
        "properties": {
            "pipeline": pipeline,
            "dealstage": stage,
            "hubspot_owner_id": owner,
            "dealname": "Acme renewal",
        },
    }


# ─── Pipeline gate ───────────────────────────────────────────────────────────


def test_update_deal_in_client_pipeline_is_refused(allowlist, monkeypatch):
    writer = _writer()
    monkeypatch.setattr(
        writer, "get_object", lambda *a, **k: _live_deal(INITECH_PIPELINE)
    )
    request = MagicMock()
    monkeypatch.setattr(writer, "_request", request)

    with pytest.raises(HubSpotWriteRefused, match="not allowed in pipeline"):
        writer.update_deal("555", {"amount": "5000"}, acting_owner_email=AE)
    request.assert_not_called()


def test_empty_allowlist_refuses_every_deal_write_and_says_who_can_fix_it(
    workspace_config,
):
    """An empty workspace pipeline list refuses every deal write.

    A blank setting must never unlock writes into a client's pipeline, so until
    an admin sets the pipelines up every deal write is refused, with a reason
    the person who clicked can act on.
    """
    workspace_config(pipeline_ids=())

    for pipeline in (OWN_PIPELINE, INITECH_PIPELINE, None, ""):
        with pytest.raises(HubSpotPipelineRefused) as refused:
            HubSpotWriteClient._pipeline_gate(pipeline)
        assert str(refused.value) == PIPELINES_NOT_SET_UP
    assert "set up which HubSpot pipelines" in PIPELINES_NOT_SET_UP
    assert "admin" in PIPELINES_NOT_SET_UP
    # It is the sentence the person sees, not a raw error.
    assert friendly_write_error("the deal", refused.value) == PIPELINES_NOT_SET_UP


def test_empty_allowlist_refuses_a_deal_update_before_any_write(
    monkeypatch, workspace_config
):
    workspace_config(pipeline_ids=())
    writer = _writer()
    monkeypatch.setattr(writer, "get_object", lambda *a, **k: _live_deal())
    request = MagicMock()
    monkeypatch.setattr(writer, "_request", request)

    with pytest.raises(HubSpotPipelineRefused, match="set up which HubSpot pipelines"):
        writer.update_deal("555", {"amount": "5000"}, acting_owner_email=AE)
    request.assert_not_called()


def test_a_deal_with_no_pipeline_is_refused_by_the_allowlist(allowlist):
    for missing in (None, ""):
        with pytest.raises(HubSpotPipelineRefused, match="not allowed in pipeline"):
            HubSpotWriteClient._pipeline_gate(missing)


def test_empty_allowlist_refuses_the_read_path(monkeypatch, workspace_config):
    """A blank pipeline list cannot read client deals from a shared portal."""
    from rubik_core.crm_sync.hubspot_context import fetch_deals_for_companies

    workspace_config(pipeline_ids=())
    client = _read_client()
    client.search_all.return_value = [
        {
            "id": "1",
            "properties": {"pipeline": INITECH_PIPELINE, "dealname": "Client deal"},
        }
    ]

    deals = fetch_deals_for_companies(
        client, ["co-1"], ae_owner_id=AE_OWNER_ID, stage_labels={}, owner_names={}
    )

    assert deals == []
    client.search_all.assert_not_called()


def test_create_deal_requires_an_explicit_pipeline(allowlist):
    writer = _writer()
    with pytest.raises(HubSpotWriteRefused, match="without an explicit pipeline"):
        writer.create_deal({"dealname": "No pipeline"}, owner_email=AE)


# ─── Owner gate ──────────────────────────────────────────────────────────────


def test_update_deal_refused_when_the_ae_does_not_own_it(allowlist, monkeypatch):
    writer = _writer()
    monkeypatch.setattr(writer, "get_object", lambda *a, **k: _live_deal(owner="999"))
    request = MagicMock()
    monkeypatch.setattr(writer, "_request", request)

    with pytest.raises(HubSpotWriteRefused, match="only updates deals you own"):
        writer.update_deal("555", {"amount": "5000"}, acting_owner_email=AE)
    request.assert_not_called()


def test_deal_writes_refused_when_the_caller_is_not_a_hubspot_owner(
    allowlist, monkeypatch
):
    writer = _writer(read_client=_read_client(owner_email="someone.else@acme.example"))
    monkeypatch.setattr(writer, "get_object", lambda *a, **k: _live_deal())

    with pytest.raises(HubSpotWriteRefused, match="not a HubSpot deal owner"):
        writer.update_deal("555", {"amount": "1"}, acting_owner_email=AE)
    with pytest.raises(HubSpotWriteRefused, match="not a HubSpot deal owner"):
        writer.create_deal({"dealname": "X", "pipeline": OWN_PIPELINE}, owner_email=AE)


# ─── Property + stage gates ──────────────────────────────────────────────────


def test_non_whitelisted_property_is_refused_at_the_write_boundary(
    allowlist, monkeypatch
):
    """Even bypassing the validators entirely, the API never sees this key."""
    writer = _writer()
    monkeypatch.setattr(writer, "get_object", lambda *a, **k: _live_deal())
    request = MagicMock()
    monkeypatch.setattr(writer, "_request", request)

    with pytest.raises(HubSpotWriteRefused, match="non-approved deal properties"):
        writer.update_deal(
            "555", {"hs_all_owner_ids": "1", "amount": "10"}, acting_owner_email=AE
        )
    request.assert_not_called()


def test_contact_email_cannot_be_updated(allowlist):
    writer = _writer()
    with pytest.raises(HubSpotWriteRefused, match="non-approved contact properties"):
        writer.update_contact("42", {"email": "new@acmecorp.example"})


def test_stage_from_another_pipeline_is_refused(allowlist, monkeypatch):
    writer = _writer()
    monkeypatch.setattr(writer, "get_object", lambda *a, **k: _live_deal())
    request = MagicMock()
    monkeypatch.setattr(writer, "_request", request)

    with pytest.raises(HubSpotWriteRefused, match="does not belong to pipeline"):
        writer.update_deal("555", {"dealstage": "clientstage"}, acting_owner_email=AE)
    request.assert_not_called()


# ─── Token gate ──────────────────────────────────────────────────────────────


def test_missing_write_token_is_refused_before_any_http(allowlist, monkeypatch):
    from rubik_core.retrieval import hubspot_write

    monkeypatch.setattr(
        hubspot_write, "workspace_secret", lambda *_: None, raising=False
    )
    writer = HubSpotWriteClient(token=None, read_client=_read_client())

    with pytest.raises(HubSpotWriteRefused, match="write token is unset"):
        writer.update_contact("42", {"phone": "555"})


def test_write_token_is_never_the_read_token(monkeypatch):
    from rubik_core.retrieval import hubspot_write

    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "process-read-token")
    monkeypatch.setenv("HUBSPOT_WRITE_TOKEN", "process-write-token")
    seen = []
    monkeypatch.setattr(
        hubspot_write,
        "workspace_secret",
        lambda connector, field: seen.append((connector, field)) or None,
        raising=False,
    )
    assert HubSpotWriteClient().token is None
    assert seen == [("hubspot", "write_token")]


def test_job_cannot_use_workspace_write_token(monkeypatch):
    from rubik_core.retrieval import hubspot_write

    monkeypatch.setenv("CLOUD_RUN_JOB", "cubie-worker")
    monkeypatch.setattr(
        hubspot_write,
        "workspace_secret",
        lambda *_: "workspace-write-token",
        raising=False,
    )
    writer = HubSpotWriteClient(read_client=_read_client())

    with pytest.raises(HubSpotWriteRefused, match="Cloud Run Job"):
        writer._headers()


def test_interactive_write_uses_only_workspace_write_token(monkeypatch):
    from rubik_core.retrieval import hubspot_write

    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    seen = []
    monkeypatch.setattr(
        hubspot_write,
        "workspace_secret",
        lambda connector, field: (
            seen.append((connector, field)) or "workspace-write-token"
        ),
    )

    writer = HubSpotWriteClient(read_client=_read_client())

    assert writer._headers()["Authorization"] == "Bearer workspace-write-token"
    assert seen == [("hubspot", "write_token")]


# ─── Backoff ─────────────────────────────────────────────────────────────────


def test_backoff_retries_rate_limits_then_succeeds(monkeypatch):
    from rubik_core.retrieval import hubspot_write

    monkeypatch.setattr(hubspot_write.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise HubSpotWriteError("HTTP 429: rate limited")
        return {"ok": True}

    assert hubspot_write._with_backoff(flaky) == {"ok": True}
    assert calls["n"] == 3


def test_backoff_gives_up_and_never_retries_a_refusal(monkeypatch):
    from rubik_core.retrieval import hubspot_write

    monkeypatch.setattr(hubspot_write.time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    def always_500():
        attempts["n"] += 1
        raise HubSpotWriteError("HTTP 503: unavailable")

    with pytest.raises(HubSpotWriteError):
        hubspot_write._with_backoff(always_500)
    assert attempts["n"] == 4

    refusals = {"n": 0}

    def refused():
        refusals["n"] += 1
        raise HubSpotWriteRefused("gate said no")

    with pytest.raises(HubSpotWriteRefused):
        hubspot_write._with_backoff(refused)
    assert refusals["n"] == 1


# ─── Idempotency ─────────────────────────────────────────────────────────────


def test_create_contact_updates_the_existing_record_instead_of_duplicating(allowlist):
    read = _read_client()
    read.search_all.return_value = [
        {"id": "42", "properties": {"email": "b@acmecorp.example"}}
    ]
    writer = _writer(read_client=read)
    sent: list[tuple] = []
    writer._request = lambda method, path, payload: (
        sent.append((method, path, payload)) or {"id": "42"}
    )

    result = writer.create_contact(
        {"email": "b@acmecorp.example", "firstname": "Bo"}, owner_email=AE
    )

    assert result["_mode"] == MODE_EXISTING_UPDATED
    assert [m for m, _p, _b in sent] == ["PATCH"]
    assert "email" not in sent[0][2]["properties"]


def test_create_contact_refuses_unbound_prospect_before_post(allowlist):
    """A new contact has no deal proving this workspace owns the person."""
    writer = _writer()
    writer._request = MagicMock()
    with pytest.raises(HubSpotWriteRefused, match="unbound HubSpot contact"):
        writer.create_contact(
            {"email": "b@acmecorp.example", "firstname": "Bo"}, owner_email=AE
        )
    writer._request.assert_not_called()


def test_existing_contact_update_never_reassigns_owner(allowlist):
    """The existing record keeps its owner — we only fix the fields we know."""
    read = _read_client()
    read.search_all.return_value = [
        {"id": "42", "properties": {"email": "b@acmecorp.example"}}
    ]
    writer = _writer(read_client=read)
    patched: dict = {}

    def fake_request(method, path, payload):
        assert method == "PATCH"
        patched.update(payload["properties"])
        return {"id": "42"}

    writer._request = fake_request
    writer.create_contact(
        {"email": "b@acmecorp.example", "phone": "555"}, owner_email=AE
    )

    assert patched == {"phone": "555"}


def test_create_deal_probe_prevents_a_duplicate(allowlist):
    read = _read_client()
    read.search_all.return_value = [
        {"id": "900", "properties": {"dealname": "Acme renewal"}}
    ]
    writer = _writer(read_client=read)
    request = MagicMock()
    writer._request = request

    result = writer.create_deal(
        {"dealname": "Acme renewal", "pipeline": OWN_PIPELINE}, owner_email=AE
    )

    assert result["_mode"] == MODE_ALREADY_EXISTS
    request.assert_not_called()


def test_a_created_deal_is_associated_to_its_company_and_contacts(allowlist):
    writer = _writer()
    captured: dict = {}
    writer._request = lambda method, path, payload: (
        captured.update(payload) or {"id": "d9"}
    )

    writer.create_deal(
        {"dealname": "Acme renewal", "pipeline": OWN_PIPELINE},
        owner_email=AE,
        company_id="co1",
        contact_ids=["c1", "c2"],
    )

    assert {a["to"]["id"] for a in captured["associations"]} == {"co1", "c1", "c2"}


# ─── Error messages an AE can act on ─────────────────────────────────────────


def test_a_validation_400_surfaces_hubspots_own_complaint():
    """Transcripts produce malformed phone numbers; "request failed" is useless.

    The body has the shape HubSpot gives a 400 for an invalid phone number.
    """
    from rubik_core.retrieval.hubspot_write import friendly_write_error

    exc = HubSpotWriteError(
        "HubSpot PATCH /crm/v3/objects/contacts/1001 failed: HTTP 400: "
        '{"status":"error","message":"Property values were not valid: '
        '[{\\"isValid\\":false,\\"message\\":\\"Number isn\'t valid for country '
        'code: +1.\\",\\"error\\":\\"INVALID_PHONE_NUMBER\\"}]"}'
    )

    text = friendly_write_error("the contact", exc)

    assert "Number isn't valid for country code" in text
    assert "HTTP 400" not in text  # never leak the raw body to the UI


def test_a_403_still_points_at_the_missing_scope():
    from rubik_core.retrieval.hubspot_write import friendly_write_error

    text = friendly_write_error("the deal", HubSpotWriteError("HTTP 403: forbidden"))

    assert "missing the write scope" in text


def test_a_gate_refusal_is_passed_through_verbatim():
    from rubik_core.retrieval.hubspot_write import friendly_write_error

    text = friendly_write_error(
        "the deal",
        HubSpotWriteRefused("CRM Sync only updates deals you own in HubSpot."),
    )

    assert text == "CRM Sync only updates deals you own in HubSpot."
