"""Sales Call Capture / Sales Cockpit — the four new HubSpot write paths.

``update_company`` / ``create_call`` / ``create_task`` / ``create_lead`` were
added to ``rubik_core/retrieval/hubspot_write.py`` (one sanctioned writer, gates
in the writer and not in the callers), plus
``rubik_core/call_capture/hubspot_schema.py`` (the live property option
cache that keeps "Sendra" from being written as "Cemdra").

``backend/tests/test_crm_sync_write_gates.py`` already covers the contact / deal
/ note surface — the pipeline, owner, stage and token gates, backoff, and the
create-then-probe idempotency modes. Nothing here re-tests those. What is under
test is only what the CCS/cockpit work added:

  - the property whitelist is a CLOSED set on each of the four new methods, so
    no property name a model invented can reach the API
  - the company fence is the company's own configured vendor fields (empty by
    default), and never admits HubSpot's automation-driving built-ins
  - the wire format of the two easy-to-get-wrong values: `hs_call_duration` is
    MILLISECONDS, and a task's `hs_timestamp` is the DUE DATE
  - every path stays on `/crm/v3/` — a mixed version on one
    `base_url` + concatenated path does not raise, it 404s
  - deal-touching activity writes inherit `_pipeline_gate`'s fail-closed
    behaviour; company/contact associations also need a live in-scope deal
  - `create_lead` fails closed twice, and the unverified Lead→Contact
    association id (HubSpot documents both 578 and 608) lives in exactly one
    named constant so correcting it is a one-line change
  - no second dealstage write path was added — the cockpit's stage advance has
    to go through the existing `update_deal`
  - the idempotency key behind "approving twice writes once" (AC 6) is stable
  - options are fetched once per run and cached with a TTL, and an empty
    `options[]` means UNCONSTRAINED, not "no valid values"

No live HubSpot: the read client is a MagicMock, ``_request`` is a recorder, and
the schema module's one HTTP helper is patched by string target.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.config import settings
from rubik_core.database.call_capture import CallHubspotWrite
from rubik_core.retrieval import hubspot_write as hw
from rubik_core.retrieval.hubspot_api import HubSpotAPIError
from rubik_core.retrieval.hubspot_write import (
    CALL_WRITABLE,
    DEAL_CREATE_WRITABLE,
    DEAL_UPDATE_WRITABLE,
    LEAD_WRITABLE,
    MODE_CREATED,
    MODE_UPDATED,
    PIPELINES_NOT_SET_UP,
    TASK_WRITABLE,
    HubSpotPipelineRefused,
    HubSpotWriteClient,
    HubSpotWriteRefused,
)
from rubik_core.tenancy.context import NoWorkspaceContext, use_workspace
from rubik_core.tenancy.hubspot_config import (
    HubSpotConfigInvalid,
    HubSpotConnectorConfig,
)

OWN_PIPELINE = "default"
INITECH_PIPELINE = "9100004"  # the CLIENT pipeline hosted in the same portal
AE = "ae@acme.example"
AE_OWNER_ID = "77"
COMPANY_ID = "8001"
CONTACT_ID = "9001"
DEAL_ID = "555"

# A due date, not "now" — see `create_task`.
#
# Relative, because `test_task_hs_timestamp_is_the_due_date_not_the_creation_time`
# asserts this lands in the FUTURE. Pinned to a literal it is a time bomb: the
# suite goes red the moment that hour passes and stays red for good, on every
# branch at once, with a failure that points at HubSpot task writes rather
# than at the calendar. Anchored to the clock instead, so
# the property it is actually testing — that create_task sends the DUE date and
# not the creation time — holds whenever the suite runs.
_DUE = datetime.now(timezone.utc) + timedelta(days=1)
_DUE_MS = int(_DUE.timestamp() * 1000)


# ─── harness ─────────────────────────────────────────────────────────────────


class _Recorder:
    """Stands in for ``HubSpotWriteClient._request``; records what would be sent."""

    def __init__(self, response: dict | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self._response = response or {"id": "hs-new-1"}

    def __call__(self, method: str, path: str, payload: dict) -> dict:
        self.calls.append((method, path, payload))
        return dict(self._response)

    @property
    def last(self) -> tuple[str, str, dict]:
        assert self.calls, "no HubSpot request was made"
        return self.calls[-1]

    @property
    def props(self) -> dict:
        return self.last[2]["properties"]

    def assert_not_called(self) -> None:
        assert self.calls == [], f"a HubSpot write leaked: {self.calls}"


def _read_client() -> MagicMock:
    client = MagicMock()
    client.owner_directory.return_value = {AE_OWNER_ID: {"email": AE, "name": "A E"}}
    client.deal_pipeline_stages.return_value = {
        OWN_PIPELINE: {"appointmentscheduled": "Discovery"}
    }
    client.search_all.return_value = []
    client._headers.return_value = {"Authorization": "Bearer read-token"}
    client.fully_owned_deal_associations.side_effect = lambda _from_type, ids: {
        str(i): ["owned-deal"] for i in ids
    }
    return client


def _writer(
    monkeypatch,
    *,
    token: str = "write-token",
    deal_pipeline: str = OWN_PIPELINE,
) -> tuple[HubSpotWriteClient, _Recorder, MagicMock]:
    """A gated writer whose HTTP surface is a recorder.

    ``get_object`` is stubbed as a MagicMock rather than a lambda so a test can
    assert the LIVE deal read either happened or (for `update_company`) did not.
    """
    writer = HubSpotWriteClient(token=token, read_client=_read_client())
    recorder = _Recorder()
    get_object = MagicMock(
        return_value={"id": DEAL_ID, "properties": {"pipeline": deal_pipeline}}
    )
    monkeypatch.setattr(writer, "_request", recorder)
    monkeypatch.setattr(writer, "get_object", get_object)
    return writer, recorder, get_object


@pytest.fixture
def allowlist(vendor_fields):
    """The company's own pipelines only — the client (Initech) pipeline excluded."""
    vendor_fields(pipeline_ids=(OWN_PIPELINE, "9100002"))


@pytest.fixture
def leads_unblocked(monkeypatch):
    """Lift both of `create_lead`'s real-world blocks so the gates BEHIND them run.

    Both have been blockers (the scopes and the association id); they are
    pinned here so the tests do not depend on the shipped defaults, and
    patching the module constant by string target is also how
    `test_the_lead_contact_association_id_lives_in_one_place` proves the id is
    read from one place.
    """
    monkeypatch.setattr(settings, "CCS_LEADS_ENABLED", True)
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_write._ASSOC_LEAD_TO_CONTACT", 578
    )


#: The vendor fields these tests configure (Admin sets the HubSpot connector
#: per company; a fresh install has none, see the fence tests below).
CONFIGURED_VENDOR_FIELDS = ("supplier", "los")


@pytest.fixture(autouse=True)
def vendor_fields(monkeypatch):
    current = HubSpotConnectorConfig(
        pipeline_ids=(OWN_PIPELINE,),
        ccs_vendor_fields_default=CONFIGURED_VENDOR_FIELDS,
    )

    def get_config():
        return current

    monkeypatch.setattr(hw, "get_hubspot_config", get_config)

    def configure(**changes):
        nonlocal current
        current = replace(current, **changes)
        return current

    return configure


# ─── The property gate is a closed whitelist, on all four new methods ─────────


def test_update_company_refuses_a_property_outside_the_whitelist(monkeypatch):
    """`domain` is a real HubSpot property and still refused: closed, not validated."""
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused, match="non-approved company properties"):
        writer.update_company(COMPANY_ID, {"supplier": "Cendra", "domain": "x.example"})
    recorder.assert_not_called()


def test_create_call_refuses_a_property_outside_the_whitelist(monkeypatch):
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused, match="non-approved call properties"):
        writer.create_call(
            {"hs_timestamp": _DUE_MS, "hs_call_disposition": "f240bbac-87c9-4f6e"}
        )
    recorder.assert_not_called()


def test_create_task_refuses_a_property_outside_the_whitelist(monkeypatch):
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused, match="non-approved task properties"):
        writer.create_task(
            {"hs_timestamp": _DUE_MS, "hs_task_reminders": "1"}, owner_email=AE
        )
    recorder.assert_not_called()


def test_create_lead_refuses_a_property_outside_the_whitelist(
    monkeypatch, leads_unblocked
):
    """`hs_lead_temperature`-shaped guesses are exactly what must never be invented."""
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused, match="non-approved lead properties"):
        writer.create_lead(
            {
                "hs_lead_name": "Acme — staff training",
                "hs_lead_temperature": "H",
            },
            contact_id=CONTACT_ID,
            owner_email=AE,
        )
    recorder.assert_not_called()


def test_the_whitelists_are_immutable_frozensets():
    """A caller cannot widen the fence at runtime with `.add()`."""
    for name, whitelist in (
        ("company_writable()", hw.company_writable()),
        ("CALL_WRITABLE", CALL_WRITABLE),
        ("TASK_WRITABLE", TASK_WRITABLE),
        ("LEAD_WRITABLE", LEAD_WRITABLE),
    ):
        assert isinstance(whitelist, frozenset), name


def test_an_empty_property_dict_is_refused_not_silently_skipped(monkeypatch):
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused, match="No company properties to write"):
        writer.update_company(COMPANY_ID, {})
    recorder.assert_not_called()


# ─── The company fence is the company's own configured vendor fields ─────────


def test_a_fresh_install_may_write_no_company_property(monkeypatch, vendor_fields):
    """Vendor fields are each portal's own custom properties, so the fence is
    configuration and ships empty: nothing is writable until an admin lists
    the company's fields."""
    vendor_fields(ccs_vendor_fields_default=(), ccs_vendor_fields_extended=())
    from rubik_core.call_capture.extraction import _vendor_properties

    assert hw.company_writable() == frozenset()
    assert _vendor_properties() == []
    writer, recorder, _ = _writer(monkeypatch)
    with pytest.raises(HubSpotWriteRefused, match="non-approved company properties"):
        writer.update_company(COMPANY_ID, {"supplier": "Cendra"})
    recorder.assert_not_called()


def test_company_writable_is_exactly_the_configured_vendor_fields(vendor_fields):
    """DEFAULT and EXTENDED together, each name once."""
    vendor_fields(
        ccs_vendor_fields_default=("payroll_vendor",),
        ccs_vendor_fields_extended=("it_vendor", "payroll_vendor"),
    )

    assert hw.company_writable() == frozenset({"payroll_vendor", "it_vendor"})


def test_company_type_is_not_writable_by_call_capture():
    """Company Type is a classification, not a vendor relationship.

    Call capture fills vendor relationships from a transcript. An AI-inferred
    company type must never reach the CRM, so the property is outside the
    configured fields.
    """
    assert "company_rtype" not in hw.company_writable()


def test_company_writable_excludes_the_automation_driving_fields(vendor_fields):
    """Name, domain, owner, lifecycle stage and lead status drive HubSpot's own
    automation: refused even when somebody lists them as vendor fields."""
    never = ("name", "domain", "lifecyclestage", "hubspot_owner_id", "hs_lead_status")
    with pytest.raises(HubSpotConfigInvalid):
        vendor_fields(ccs_vendor_fields_default=never)

    for prop in never:
        assert prop not in hw.company_writable(), prop


def test_the_configured_vendor_fields_are_what_the_extractor_asks_for(vendor_fields):
    """Which fields the extractor fills is config, and so is the fence.

    The properties are the company's own, so config ships with none. A
    deployment's list is asked for in order, once each, and the automation-
    driving built-ins in it are dropped before the model ever sees them.
    """
    from rubik_core.call_capture.extraction import _vendor_properties

    inside = ["payroll_vendor", "it_vendor", "insurer"]
    outside = ["lifecyclestage", "hubspot_owner_id"]
    with pytest.raises(HubSpotConfigInvalid):
        vendor_fields(ccs_vendor_fields_default=(inside[0], outside[0]))
    vendor_fields(
        ccs_vendor_fields_default=(inside[0],),
        ccs_vendor_fields_extended=(*inside[1:], inside[0]),
    )

    proposed = _vendor_properties()

    assert proposed == inside  # in order, once each
    assert set(proposed) <= hw.company_writable()
    assert not set(outside) & set(proposed)


# ─── Wire format: the two values that are easy to get wrong ──────────────────


def test_call_duration_goes_on_the_wire_in_milliseconds_verbatim(
    monkeypatch, allowlist
):
    """`hs_call_duration` is MILLISECONDS and is NOT converted at this boundary.

    A caller that passes seconds logs every call as under a second, and neither
    side rejects it — so the pin is that the integer handed in is the integer
    sent, and that a 22-minute call is ~1.3 million, not 1_320.
    """
    writer, recorder, _ = _writer(monkeypatch)
    duration_ms = int(timedelta(minutes=22).total_seconds() * 1000)

    writer.create_call(
        {
            "hs_timestamp": _DUE_MS,
            "hs_call_title": "Acme — staff training",
            "hs_call_duration": duration_ms,
        },
        company_id=COMPANY_ID,
    )

    assert recorder.props["hs_call_duration"] == 1_320_000
    assert recorder.props["hs_call_duration"] == duration_ms


def test_task_hs_timestamp_is_the_due_date_not_the_creation_time(monkeypatch):
    """HubSpot reuses one property name with a different meaning per object.

    On a task `hs_timestamp` is the DUE DATE. The method must pass the caller's
    future date through untouched — a task stamped "now" is overdue the instant
    it exists, which turns a nudge into an accusation.
    """
    writer, recorder, _ = _writer(monkeypatch)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    writer.create_task(
        {"hs_timestamp": _DUE_MS, "hs_task_subject": "Send the SOW"},
        owner_email=AE,
        contact_id=CONTACT_ID,
    )

    assert recorder.props["hs_timestamp"] == _DUE_MS
    assert recorder.props["hs_timestamp"] > now_ms


def test_activity_writes_refuse_without_hs_timestamp(monkeypatch):
    """The one property HubSpot requires; its own 400 body is unreadable to an AE."""
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused, match="without hs_timestamp"):
        writer.create_call({"hs_call_title": "Acme"})
    with pytest.raises(HubSpotWriteRefused, match="hs_timestamp \\(the due date\\)"):
        writer.create_task({"hs_task_subject": "Send the SOW"}, owner_email=AE)
    recorder.assert_not_called()


def test_update_company_sends_one_patch_for_every_field(monkeypatch, vendor_fields):
    """A dozen properties at one request each burns the 190/10s budget."""
    fields = ("payroll_vendor", "it_vendor", "insurer", "auditor", "bank")
    vendor_fields(ccs_vendor_fields_default=fields)
    writer, recorder, _ = _writer(monkeypatch)
    properties = {
        "payroll_vendor": "Initech",
        "it_vendor": "Globex",
        "insurer": "Umbrella Corp",
        "auditor": "Acme Audit",
        "bank": "Example Bank",
    }

    result = writer.update_company(COMPANY_ID, properties)

    assert len(recorder.calls) == 1
    method, path, payload = recorder.last
    assert method == "PATCH"
    assert path == f"/crm/v3/objects/companies/{COMPANY_ID}"
    assert payload == {"properties": properties}
    assert result["_mode"] == MODE_UPDATED


# ─── Every new path stays on /crm/v3/ ────────────────────────────────────────


def test_every_new_write_path_stays_on_crm_v3(monkeypatch, allowlist, leads_unblocked):
    """HubSpot documents `/crm/objects/2026-03/` as current. We deliberately did not
    adopt it: one `base_url` plus a concatenated path with a mixed version does not
    raise, it 404s, and every other method, caller and test mock in the repo is v3.
    """
    writer, recorder, _ = _writer(monkeypatch)

    writer.update_company(COMPANY_ID, {"supplier": "Cendra"})
    writer.create_call({"hs_timestamp": _DUE_MS}, deal_id=DEAL_ID)
    writer.create_task(
        {"hs_timestamp": _DUE_MS, "hs_task_subject": "Send the SOW"},
        owner_email=AE,
        deal_id=DEAL_ID,
    )
    writer.create_lead({"hs_lead_name": "Acme"}, contact_id=CONTACT_ID, owner_email=AE)

    paths = [path for _, path, _ in recorder.calls]
    assert paths == [
        f"/crm/v3/objects/companies/{COMPANY_ID}",
        "/crm/v3/objects/calls",
        "/crm/v3/objects/tasks",
        "/crm/v3/objects/leads",
    ]
    for path in paths:
        assert "2026-03" not in path
        assert path.startswith("/crm/v3/")


# ─── The pipeline gate: fail-closed, and only where a pipeline exists ────────


def test_a_call_attached_to_a_deal_is_refused_on_an_empty_allowlist(
    monkeypatch, vendor_fields
):
    """An empty pipeline list refuses before a protected HubSpot read.

    Until an admin sets up the company's pipelines, every deal-touching write is
    refused, with a reason that says so — even for the company's own default
    pipeline.
    """
    vendor_fields(pipeline_ids=())
    writer, recorder, get_object = _writer(monkeypatch)

    with pytest.raises(HubSpotPipelineRefused) as refused:
        writer.create_call({"hs_timestamp": _DUE_MS}, deal_id=DEAL_ID)
    assert str(refused.value) == PIPELINES_NOT_SET_UP
    recorder.assert_not_called()
    get_object.assert_not_called()


def test_a_deal_with_no_pipeline_is_refused(monkeypatch, allowlist):
    writer, recorder, _ = _writer(monkeypatch, deal_pipeline="")

    with pytest.raises(HubSpotWriteRefused, match="not allowed in pipeline"):
        writer.create_call({"hs_timestamp": _DUE_MS}, deal_id=DEAL_ID)
    recorder.assert_not_called()


def test_a_task_attached_to_a_client_pipeline_deal_is_refused(monkeypatch, allowlist):
    """Initech Partners is a CLIENT's pipeline in the company's portal."""
    writer, recorder, _ = _writer(monkeypatch, deal_pipeline=INITECH_PIPELINE)

    with pytest.raises(HubSpotWriteRefused, match="not allowed in pipeline"):
        writer.create_task(
            {"hs_timestamp": _DUE_MS, "hs_task_subject": "Nudge"},
            owner_email=AE,
            deal_id=DEAL_ID,
        )
    recorder.assert_not_called()


def test_an_activity_with_no_deal_still_needs_workspace_ownership(
    monkeypatch, vendor_fields
):
    """A company/contact-only call still touches those protected records."""
    vendor_fields(pipeline_ids=())
    writer, recorder, get_object = _writer(monkeypatch)

    with pytest.raises(HubSpotPipelineRefused):
        writer.create_call(
            {"hs_timestamp": _DUE_MS}, contact_id=CONTACT_ID, company_id=COMPANY_ID
        )

    get_object.assert_not_called()
    recorder.assert_not_called()


def test_update_company_refuses_without_an_ownership_proof(monkeypatch, vendor_fields):
    """A company needs an in-scope associated deal before its fields change."""
    vendor_fields(pipeline_ids=())
    writer, recorder, get_object = _writer(monkeypatch)

    with pytest.raises(HubSpotPipelineRefused):
        writer.update_company(COMPANY_ID, {"supplier": "Cendra"})

    recorder.assert_not_called()
    get_object.assert_not_called()


# ─── create_lead fails closed, twice ─────────────────────────────────────────


def test_create_lead_refuses_while_the_leads_flag_is_off(monkeypatch):
    """The kill switch. The refusal has to NAME it — an AE seeing "the request
    failed" learns nothing, and the fix is an environment change.
    """
    monkeypatch.setattr(settings, "CCS_LEADS_ENABLED", False)
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused) as exc:
        writer.create_lead(
            {"hs_lead_name": "Acme"}, contact_id=CONTACT_ID, owner_email=AE
        )

    assert "CCS_LEADS_ENABLED" in str(exc.value)
    recorder.assert_not_called()


def test_create_lead_refuses_because_the_association_id_is_unverified(monkeypatch):
    """The second, independent block — and the dangerous one.

    A wrong Lead→Contact id does not raise: HubSpot returns 200 with an id for a
    lead that has no primary association, and auto-deletes it. So the refusal
    names both documented candidates rather than picking one.
    """
    monkeypatch.setattr(settings, "CCS_LEADS_ENABLED", True)
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_write._ASSOC_LEAD_TO_CONTACT", None
    )
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused) as exc:
        writer.create_lead(
            {"hs_lead_name": "Acme"}, contact_id=CONTACT_ID, owner_email=AE
        )

    message = str(exc.value)
    assert "unverified" in message
    assert "578" in message and "608" in message
    recorder.assert_not_called()


def test_the_lead_contact_association_id_lives_in_one_named_place(
    monkeypatch, leads_unblocked
):
    """Correcting the id must be a one-line change, not a grep for a literal.

    `leads_unblocked` patches the single module constant
    `_ASSOC_LEAD_TO_CONTACT`; if the value were inlined in `create_lead`, the
    payload below would carry something else.
    """
    assert hw._ASSOC_LEAD_TO_CONTACT_CANDIDATES == (578, 608)
    writer, recorder, _ = _writer(monkeypatch)

    writer.create_lead({"hs_lead_name": "Acme"}, contact_id=CONTACT_ID, owner_email=AE)

    associations = recorder.last[2]["associations"]
    assert associations == [
        {
            "to": {"id": CONTACT_ID},
            "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 578}
            ],
        }
    ]

    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_write._ASSOC_LEAD_TO_CONTACT", 608
    )
    writer.create_lead({"hs_lead_name": "Acme"}, contact_id=CONTACT_ID, owner_email=AE)
    assert recorder.last[2]["associations"][0]["types"][0]["associationTypeId"] == 608


def test_the_lead_association_id_ships_as_the_verified_primary_label():
    """578 is HUBSPOT_DEFINED "Primary" on portal 9100001.

    608 also exists there, unlabelled. HubSpot auto-deletes a lead with no
    PRIMARY association, so shipping 608 would create records that vanish.
    """
    assert hw._ASSOC_LEAD_TO_CONTACT == 578
    assert 578 in hw._ASSOC_LEAD_TO_CONTACT_CANDIDATES


def test_create_lead_refuses_without_a_contact_to_associate(
    monkeypatch, leads_unblocked
):
    """A lead with no primary association is auto-deleted by HubSpot."""
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused, match="no primary association"):
        writer.create_lead({"hs_lead_name": "Acme"}, contact_id="", owner_email=AE)
    with pytest.raises(HubSpotWriteRefused, match="without hs_lead_name"):
        writer.create_lead(
            {"hs_lead_name": "   "}, contact_id=CONTACT_ID, owner_email=AE
        )
    recorder.assert_not_called()


# ─── No second deal-writing path was added ───────────────────────────────────


def test_dealstage_is_writable_only_through_the_deal_whitelists():
    """The cockpit's one-click stage advance has to reuse `update_deal`.

    `update_deal` already re-checks pipeline, ownership AND
    stage-belongs-to-pipeline against live state. A parallel stage path would be
    a second place to forget one of those.
    """
    named = {
        "company_writable()": hw.company_writable(),
        "CALL_WRITABLE": CALL_WRITABLE,
        "TASK_WRITABLE": TASK_WRITABLE,
        "LEAD_WRITABLE": LEAD_WRITABLE,
        "DEAL_UPDATE_WRITABLE": DEAL_UPDATE_WRITABLE,
        "DEAL_CREATE_WRITABLE": DEAL_CREATE_WRITABLE,
    }

    stage_capable = {name for name, ws in named.items() if "dealstage" in ws}
    assert stage_capable == {"DEAL_UPDATE_WRITABLE", "DEAL_CREATE_WRITABLE"}


def test_no_new_mutating_method_was_added_beside_the_four():
    """The whole mutation surface of the sanctioned writer, spelled out.

    Also pins the two deliberate absences: no `update_lead` and no
    association-removal path, because the only safe lead mutation is one that
    cannot orphan (and therefore delete) the record.

    ``update_company_research_link`` (UC-45) is the one entry here
    that is NOT approval-gated, and it is listed deliberately rather than
    excluded from the pattern: it writes a URL Cubie derives from its own config
    plus the HubSpot company id, so there is no proposed content for a person to
    approve. Its fence is ``RESEARCH_LINK_WRITABLE``, not ``company_writable()``
    — see ``backend/tests/test_xray_crm_link.py``, which pins that separation.

    ``create_email`` (Loose Ends) IS click-approved: it runs only
    from Loose Ends "Log it in HubSpot", in the request the AE triggered, after
    the AE saw a preview of exactly those emails. What it writes is the AE's
    own sent email verbatim (re-read server-side from their mailbox, never
    model text and never a note), fenced by ``EMAIL_WRITABLE``, outgoing only
    (``hs_email_direction`` must be ``EMAIL``); ``test_loop_closure_actions.py``
    pins that. There is no ``create_note``: call notes are Fathom's.
    Anything else added here needs the same argument made explicitly.
    """
    mutators = {
        name
        for name in dir(HubSpotWriteClient)
        if not name.startswith("_")
        and name.startswith(
            ("create_", "update_", "delete_", "remove_", "set_", "advance_")
        )
    }

    assert mutators == {
        "create_contact",
        "update_contact",
        "create_deal",
        "update_deal",
        "update_company",
        "update_company_research_link",
        "create_call",
        "create_task",
        "create_lead",
        "create_email",
    }


# ─── Task ownership: the nudge has to land in someone's worklist ─────────────


def test_create_task_refuses_when_the_owner_email_is_not_a_hubspot_owner(monkeypatch):
    """An AE whose HubSpot email differs from their Workspace email is live.

    An unowned task exists and is never seen, which is worse than no task.
    """
    writer, recorder, _ = _writer(monkeypatch)

    with pytest.raises(HubSpotWriteRefused, match="assigned to nobody"):
        writer.create_task(
            {"hs_timestamp": _DUE_MS, "hs_task_subject": "Nudge"},
            owner_email="stranger@acme.example",
        )
    recorder.assert_not_called()


def test_create_task_ignores_a_caller_supplied_owner_id(monkeypatch):
    """`hubspot_owner_id` is whitelisted so the validators share one vocabulary —
    not so an upstream value can assign someone else's work."""
    writer, recorder, _ = _writer(monkeypatch)

    writer.create_task(
        {
            "hs_timestamp": _DUE_MS,
            "hs_task_subject": "Nudge",
            "hubspot_owner_id": "999",
        },
        owner_email=AE,
    )

    assert recorder.props["hubspot_owner_id"] == AE_OWNER_ID


def test_a_call_with_no_associations_is_still_logged_but_warned(monkeypatch, caplog):
    """ "Always log the call" outranks the association, but it must not pass quietly:
    an engagement attached to nothing appears on no record in the CRM."""
    writer, recorder, _ = _writer(monkeypatch)

    with caplog.at_level("WARNING"):
        result = writer.create_call({"hs_timestamp": _DUE_MS})

    assert result["_mode"] == MODE_CREATED
    assert "associations" not in recorder.last[2]
    assert any("no associations" in r.getMessage() for r in caplog.records)


def test_the_new_methods_refuse_before_any_http_without_a_write_token(monkeypatch):
    """No fallback to the read token: unset means refused, not silently read-scoped.

    The client object is constructed before `_headers()` raises (the header build
    is an argument to `client.request`), so the pin is that no REQUEST is issued.
    """
    monkeypatch.setattr(hw, "workspace_secret", lambda *_: None)
    http_client = MagicMock()
    monkeypatch.setattr(hw.httpx, "Client", http_client)
    writer = HubSpotWriteClient(token=None, read_client=_read_client())

    with pytest.raises(HubSpotWriteRefused, match="workspace write token is unset"):
        writer.update_company(COMPANY_ID, {"supplier": "Cendra"})
    with pytest.raises(HubSpotWriteRefused, match="workspace write token is unset"):
        writer.create_call({"hs_timestamp": _DUE_MS}, company_id=COMPANY_ID)
    http_client.return_value.__enter__.return_value.request.assert_not_called()


# ─── Idempotency: "approving twice writes once" (AC 6) ───────────────────────


def _key(*args) -> str:
    """`backend.routers.calls._idempotency_key`, resolved at call time.

    The router module is imported inside the helper rather than at collection so
    a mid-suite reload of the auth/db modules it pulls in cannot leave this file
    holding a stale function object.
    """
    from backend.routers.calls import _idempotency_key

    return _idempotency_key(*args)


def test_the_idempotency_key_is_stable_for_the_same_mutation():
    """HubSpot has no idempotency key, so this hash plus the UNIQUE column is
    the entire mechanism behind AC 6."""
    first = _key("sess-1", "companies", COMPANY_ID, "supplier")
    second = _key("sess-1", "companies", COMPANY_ID, "supplier")

    assert first == second
    assert len(first) == 64 and first.isalnum()


@pytest.mark.parametrize(
    "changed",
    [
        ("sess-2", "companies", COMPANY_ID, "supplier"),
        ("sess-1", "calls", COMPANY_ID, "supplier"),
        ("sess-1", "companies", "8002", "supplier"),
        ("sess-1", "companies", COMPANY_ID, "records_vendor"),
    ],
)
def test_any_change_in_the_tuple_changes_the_key(changed):
    """Session, object, record, property — the idempotency tuple, all four load-bearing.

    Two vendor fields on ONE company in ONE call are two separate writes; a key
    that collapsed them would write the first and silently drop the second.
    """
    baseline = _key("sess-1", "companies", COMPANY_ID, "supplier")

    assert _key(*changed) != baseline


def test_a_missing_target_folds_to_empty_rather_than_the_string_none():
    """A create has no `target_id`. Folding `None` to "None" would make the key
    of a create collide with a real record whose id is the literal "None"."""
    assert _key("sess-1", "leads", None, None) == _key("sess-1", "leads", "", "")
    assert _key("sess-1", "leads", None, None) != _key(
        "sess-1", "leads", "None", "None"
    )


def test_the_ledger_unique_column_rejects_the_second_identical_write():
    """The DB, not the UI, is what makes a double-click write once — the double
    submit that matters is the one where the first click appeared to do nothing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    CallHubspotWrite.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    key = _key("sess-1", "companies", COMPANY_ID, "supplier")

    def _row() -> CallHubspotWrite:
        return CallHubspotWrite(
            id=uuid.uuid4().hex,
            idempotency_key=key,
            method="PATCH",
            path=f"/crm/v3/objects/companies/{COMPANY_ID}",
        )

    try:
        session.add(_row())
        session.commit()
        session.add(_row())
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()
        engine.dispose()


# ─── hubspot_schema: fetched once per run, TTL-cached ────────────────────────

_PROPERTIES_PAYLOAD = [
    {
        "name": "supplier",
        "options": [
            {"label": "Cendra", "value": "Cendra"},
            {"label": "Ledgerly", "value": "Ledgerly"},
            {"label": "Sendra", "value": "Sendra", "hidden": True},
            {"label": "Old Vendor", "value": "Old Vendor", "archived": True},
            {"label": "No value", "value": None},
        ],
    },
    {"name": "los", "options": []},  # free text on this portal
    {"name": "license_id"},  # no options key at all
    {"name": "", "options": [{"value": "x"}]},  # nameless: skipped entirely
]


@pytest.fixture
def schema(monkeypatch):
    """The schema module with its module-level cache cleared and HTTP replaced.

    The cache is process-global, so clearing it on the way OUT matters as much as
    on the way in — a warm cache would leak into any other module's test.
    """
    from rubik_core.call_capture import hubspot_schema
    from rubik_core.retrieval import hubspot_api

    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "read-token")
    monkeypatch.setattr(hubspot_schema, "workspace_secret", lambda *_: "read-token")
    hubspot_schema.invalidate_cache()
    try:
        yield hubspot_schema
    finally:
        hubspot_schema.invalidate_cache()


class _Fetcher:
    """Stands in for the module's single HTTP helper; counts calls."""

    def __init__(self, payload=None, error: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._payload = payload if payload is not None else _PROPERTIES_PAYLOAD
        self._error = error

    def __call__(self, url: str, headers: dict) -> list[dict]:
        self.calls.append((url, headers))
        if self._error:
            raise self._error
        return list(self._payload)


def _install(monkeypatch, fetcher: _Fetcher) -> _Fetcher:
    monkeypatch.setattr("rubik_core.call_capture.hubspot_schema._get_results", fetcher)
    return fetcher


def test_options_are_fetched_once_per_run_not_once_per_record(monkeypatch, schema):
    """Account-wide lookups repeated per record are what burn the rate limit.

    One fetch answers every property and every caller for the whole TTL.
    """
    fetcher = _install(monkeypatch, _Fetcher())

    first = schema.allowed_values_for(["supplier", "los"])
    for _ in range(5):  # five more "records" in the same run
        schema.allowed_values_for(["supplier"])

    assert len(fetcher.calls) == 1
    assert first["supplier"] == ["Cendra", "Ledgerly"]


def test_the_schema_fetch_uses_the_v3_properties_path_and_the_read_token(
    monkeypatch, schema
):
    """Reading the schema is a READ; it must never touch the write token, and the
    path stays on /crm/v3/ like every other call in the repo (C8)."""
    monkeypatch.setattr(hw, "workspace_secret", lambda *_: "write-token")
    fetcher = _install(monkeypatch, _Fetcher())

    schema.allowed_values_for(["supplier"])

    url, headers = fetcher.calls[0]
    assert url.endswith("/crm/v3/properties/companies")
    assert "2026-03" not in url
    assert headers["Authorization"] == "Bearer read-token"
    assert "write-token" not in str(headers)


def test_a_property_with_no_options_is_unconstrained_not_forbidden(monkeypatch, schema):
    """An empty list means free text (`los`, `license_id`), NOT "no valid values".

    A caller that read `[]` as "nothing may be written here" would block every
    free-text vendor field while reporting success.
    """
    _install(monkeypatch, _Fetcher())

    got = schema.allowed_values_for(["los", "license_id"])

    assert got == {"los": [], "license_id": []}
    assert set(got) == {"los", "license_id"}  # one key per REQUESTED property


def test_hidden_and_archived_options_are_never_offered(monkeypatch, schema):
    """Offering a retired option invites the model to resurface a value the
    business deliberately stopped using — and a None value is not an option."""
    _install(monkeypatch, _Fetcher())

    values = schema.allowed_values_for(["supplier"])["supplier"]

    assert values == ["Cendra", "Ledgerly"]
    assert "Sendra" not in values and "Old Vendor" not in values
    assert None not in values


def test_an_unknown_property_returns_empty_and_says_so(monkeypatch, schema, caplog):
    """The usual cause is a typo in the configured vendor field list; the symptom
    would otherwise be the extractor proposing values that 400 at write time."""
    _install(monkeypatch, _Fetcher())

    with caplog.at_level("WARNING"):
        got = schema.allowed_values_for(["not_a_property"])

    assert got == {"not_a_property": []}
    assert any("has no property named" in r.getMessage() for r in caplog.records)


def test_the_cached_option_map_expires_after_its_ttl(monkeypatch, schema):
    """One hour — long enough to be cheap, short enough that Harper adding a new
    vendor option in HubSpot becomes visible without a restart."""
    fetcher = _install(monkeypatch, _Fetcher())
    assert schema._SCHEMA_CACHE_TTL_S == 3600

    schema.allowed_values_for(["supplier"])
    options, _, token_hash = schema._SCHEMA_CACHE["test"]
    schema._SCHEMA_CACHE["test"] = (
        options,
        datetime.now(timezone.utc) - timedelta(seconds=schema._SCHEMA_CACHE_TTL_S + 1),
        token_hash,
    )
    schema.allowed_values_for(["supplier"])

    assert len(fetcher.calls) == 2


def test_invalidate_cache_forces_a_refetch(monkeypatch, schema):
    """The fast path for the `needs_new_option` triage loop."""
    fetcher = _install(monkeypatch, _Fetcher())

    schema.allowed_values_for(["supplier"])
    schema.invalidate_cache()
    schema.allowed_values_for(["supplier"])

    assert len(fetcher.calls) == 2


def test_a_cold_fetch_failure_raises_instead_of_reading_as_unconstrained(
    monkeypatch, schema
):
    """Degrading to `{}` would say "every property is free text" — reintroducing
    exactly the "Sendra vs Cemdra" pollution this cache exists to prevent."""
    _install(monkeypatch, _Fetcher(error=HubSpotAPIError("HTTP 500: boom")))

    with pytest.raises(HubSpotAPIError):
        schema.allowed_values_for(["supplier"])


def test_a_refresh_failure_serves_the_stale_map(monkeypatch, schema):
    """An option list from an hour ago beats an unconstrained free-for-all."""
    _install(monkeypatch, _Fetcher())
    warm = schema.allowed_values_for(["supplier"])

    options, _, token_hash = schema._SCHEMA_CACHE["test"]
    schema._SCHEMA_CACHE["test"] = (
        options,
        datetime.now(timezone.utc) - timedelta(days=1),
        token_hash,
    )
    _install(monkeypatch, _Fetcher(error=HubSpotAPIError("HTTP 503: down")))

    assert schema.allowed_values_for(["supplier"]) == warm


def test_schema_cache_is_separate_for_each_workspace(monkeypatch, schema):
    a = _install(monkeypatch, _Fetcher())
    with use_workspace("test"):
        first = schema.allowed_values_for(["supplier"])

    b = _install(
        monkeypatch,
        _Fetcher(payload=[{"name": "supplier", "options": [{"value": "B-only"}]}]),
    )
    with use_workspace("test-b"):
        second = schema.allowed_values_for(["supplier"])
    with use_workspace("test"):
        again = schema.allowed_values_for(["supplier"])

    assert first == again == {"supplier": ["Cendra", "Ledgerly"]}
    assert second == {"supplier": ["B-only"]}
    assert len(a.calls) == len(b.calls) == 1
    assert set(schema._SCHEMA_CACHE) == {"test", "test-b"}


def test_schema_cache_requires_workspace_context_even_when_warm(monkeypatch, schema):
    from rubik_core.tenancy import context

    fetcher = _install(monkeypatch, _Fetcher())
    with use_workspace("test"):
        schema.allowed_values_for(["supplier"])
    monkeypatch.setattr(context, "_process_workspace", None)

    with pytest.raises(NoWorkspaceContext):
        schema.allowed_values_for(["supplier"])
    assert len(fetcher.calls) == 1


def test_cached_schema_is_unavailable_after_token_clear_or_rotation(
    monkeypatch, schema
):
    token = ["first-token"]
    monkeypatch.setattr(schema, "workspace_secret", lambda *_: token[0])
    a = _install(monkeypatch, _Fetcher())
    assert schema.allowed_values_for(["supplier"]) == {
        "supplier": ["Cendra", "Ledgerly"]
    }

    token[0] = None
    with pytest.raises(HubSpotAPIError, match="credential unavailable"):
        schema.allowed_values_for(["supplier"])
    assert len(a.calls) == 1

    token[0] = "second-token"
    b = _install(
        monkeypatch,
        _Fetcher(payload=[{"name": "supplier", "options": [{"value": "B-only"}]}]),
    )
    assert schema.allowed_values_for(["supplier"]) == {"supplier": ["B-only"]}
    assert len(b.calls) == 1


def test_injected_other_workspace_client_cannot_use_warm_schema_cache(
    monkeypatch, schema
):
    from rubik_core.tenancy.context import current_workspace

    token_by_workspace = {"test": "a-token", "test-b": "b-token"}
    monkeypatch.setattr(
        schema, "workspace_secret", lambda *_: token_by_workspace[current_workspace()]
    )
    a = _install(monkeypatch, _Fetcher())
    with use_workspace("test"):
        assert schema.allowed_values_for(["supplier"]) == {
            "supplier": ["Cendra", "Ledgerly"]
        }
        with pytest.raises(HubSpotAPIError, match="does not match workspace"):
            schema._option_map(client=MagicMock(token="b-token"))

    b = _install(
        monkeypatch,
        _Fetcher(payload=[{"name": "supplier", "options": [{"value": "B-only"}]}]),
    )
    with use_workspace("test-b"):
        with pytest.raises(HubSpotAPIError, match="does not match workspace"):
            schema._option_map(client=MagicMock(token="a-token"))
        assert schema.allowed_values_for(["supplier"]) == {"supplier": ["B-only"]}

    assert len(a.calls) == len(b.calls) == 1
