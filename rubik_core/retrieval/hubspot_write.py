"""HubSpot CRM **write** surface — the only place in Cubie that mutates HubSpot.

Everything else that touches HubSpot (chat, AI Priorities, Customer X-Ray) is
read-only and uses this workspace's ``hubspot/read_token``. This module is
deliberately separate and uses ``hubspot/write_token``, issued by a second
private app with write scopes: a read path should never hold write-capable
credentials, and an unset workspace write token means every write here is
refused rather than silently inheriting the read token.

Three of the five callers write only after a human clicked Approve on one
specific proposed change: UC-46 CRM Sync's apply step, the Sales Call Capture
wrap-up approval, and the Sales Cockpit nudge.

The fourth — UC-45's ``update_company_research_link`` — has no approval step
because it carries no proposed *content*: the value is a URL Cubie builds from
its own config and the HubSpot company id, so there is no model output and no
human judgement for a person to approve. It gets its own one-property fence
(``RESEARCH_LINK_WRITABLE``) rather than a seat in :func:`company_writable`, so the
absence of an approval step can never spread to a property that needs one.

The fifth — Loose Ends' "Log it in HubSpot" (``create_email``, called from
``rubik_core/loop_closure/crm_log.py``) — is a click-approved write of the
AE's own SENT email, verbatim: the page shows the AE a preview of exactly those
emails first, the server re-reads them from the AE's own mailbox (never from
the browser), and each lands as a HubSpot EMAIL engagement on the contact /
company / deal the morning scan matched — the same record HubSpot's Gmail
extension writes. Its fence is ``EMAIL_WRITABLE`` and it refuses anything but
an outgoing email. It never writes notes and never model text: call notes are
Fathom's (which is why CRM Sync has no note path).

All five write **inside the request that a person triggered, never from a job**.
Cloud Run Jobs are refused in code even if their workspace has a write token.
That is why
UC-45 writes the research link from the *request* that starts a Deep run rather
than from the ``cubie-xray-deep`` Job that finishes it.

**The gates live in here, not in the callers.** The suggestions that reach this
module were drafted by an LLM reading a meeting transcript, and a transcript is
untrusted input: anyone on a sales call can say words that end up in the model's
context. The validation at extraction time (``rubik_core/crm_sync/validators.py``)
is the first filter and the AE's approval is the second, but neither is allowed
to be the last. Every write re-checks, from live HubSpot state:

* **Pipeline and ownership** — deal reads and writes require the workspace's
  configured pipelines. Company and contact writes require a live association
  to at least one deal in those pipelines. A shared portal can contain client
  records outside the workspace; an empty list refuses deal reads and writes.
* **Ownership** — an AE may only update deals they own in HubSpot.
* **Properties** — a closed whitelist per object and mode, so no property name
  the model invented can ever reach the API.

HubSpot has no idempotency keys. Deal creation probes for an existing record;
``create_email``'s caller probes with ``find_logged_email`` before posting.
Call and task creation rely on the caller's per-action ledger.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.logsafe import safe_error
from rubik_core.retrieval.hubspot_api import HubSpotAPIClient, HubSpotAPIError
from rubik_core.tenancy.hubspot_config import get_hubspot_config
from rubik_core.tenancy.secrets import workspace_secret

logger = logging.getLogger(__name__)


class HubSpotWriteError(RuntimeError):
    """A write could not be completed (transport, HTTP, or exhausted retries).

    Never carries a token: transport failures are masked and raised from None,
    for the reason given on ``HubSpotAPIError``.
    """


#: User-facing reason for refusing protected HubSpot access while this workspace
#: has no configured deal pipelines. A company or contact also needs an
#: association to an in-scope deal, so the empty setting blocks those records.
PIPELINES_NOT_SET_UP = (
    f"Ask an admin to set up which HubSpot pipelines {PRODUCT_NAME} may access. "
    f"Until then, {PRODUCT_NAME} cannot read or change linked HubSpot records."
)


def write_pipelines() -> frozenset[str]:
    """Deal pipelines visible and writable in the active workspace."""
    return frozenset(get_hubspot_config().pipeline_ids)


def pipeline_writable(pipeline: object) -> bool:
    """Whether a deal write may touch a deal in ``pipeline``.

    Fails CLOSED: with no pipeline set up (:func:`write_pipelines` empty) the
    answer is always no, and a deal with no pipeline is never writable.
    """
    allowed = write_pipelines()
    return bool(allowed) and str(pipeline or "") in allowed


class HubSpotWriteRefused(HubSpotWriteError):
    """A hard gate rejected the write. Never retried — retrying can't help."""


class HubSpotPipelineRefused(HubSpotWriteRefused):
    """The pipeline gate refused a deal write or a deal association.

    A subclass, so every existing ``except HubSpotWriteRefused`` still catches
    it; it exists so a caller attaching an activity to a deal can tell "this
    deal is not in one of the company's pipelines" (drop the deal, keep the
    contact and the company) from every other refusal (stop).
    """


class HubSpotDealGone(HubSpotPipelineRefused):
    """The deal an activity would be attached to no longer exists (HTTP 404).

    A deal id stored on a row is a snapshot: the deal can be deleted or merged
    away afterwards. Raised by ``_gate_deal_association`` instead of the raw
    404, as a ``HubSpotPipelineRefused``, so a caller that already drops a
    refused deal and keeps the contact and company (Loose Ends) does the same
    here, while every other caller still sees a refusal it cannot retry.
    """


# ── Property whitelists ──────────────────────────────────────────────────────
# The single source of truth for "what CRM Sync is allowed to change". Defined
# here (at the boundary that enforces them) and imported by the scan-time
# validators, so the two can never drift apart.
#
# Anything absent is unwritable by design, not by oversight — lifecycle stage,
# lead status, owner reassignment and the like are deliberately excluded: they
# drive HubSpot automation and reporting, and a call transcript is not good
# enough evidence to move them.

CONTACT_WRITABLE = frozenset(
    {"firstname", "lastname", "email", "phone", "mobilephone", "jobtitle", "company"}
)
# ``email`` is creatable but NOT updatable: changing it re-identifies the person
# rather than correcting a field, and HubSpot treats it as the dedupe key.
CONTACT_UPDATE_WRITABLE = CONTACT_WRITABLE - {"email"}

DEAL_UPDATE_WRITABLE = frozenset({"dealstage", "amount", "closedate"})
DEAL_CREATE_WRITABLE = frozenset(
    {"dealname", "pipeline", "dealstage", "amount", "closedate"}
)

# ── Sales Call Capture / Sales Cockpit whitelists ────────────────────────────
# Four more objects (company, call, task, lead), same rule as above: a closed
# vocabulary per object, enforced at this boundary.
#
# **These stay on /crm/v3/.** HubSpot documents the dated
# `/crm/objects/2026-03/` endpoints as current and v3 as legacy, and
# we deliberately did NOT adopt them for these four methods. This client is one
# `base_url` plus a concatenated path, and every other method, caller and test
# mock in the repo is v3 — a per-method version mix does not raise, it 404s.
# Migrating is a whole-module change with its own test sweep, not a side effect
# of adding four methods.

# The company properties Call Capture may write: the company's OWN vendor /
# firmographic custom properties ("who do you use for X" is the thing a sales
# call is actually evidence about), which are different on every portal. So the
# fence is configuration, not code: the names an admin lists in
# the workspace's default/extended vendor fields (empty by default, so a
# fresh workspace writes no company property at all). Those same two fields
# are what the extractor is asked to fill, so it never proposes a field the
# write would refuse.
#
# Whatever is configured, HubSpot's own name, domain, owner, lifecycle and lead
# status never enter: they drive HubSpot automation and reporting, exactly as
# for contacts above, and an AI-inferred value must not become indistinguishable
# from a fact there. Nor does X-Ray's research link, which has its own fence.
COMPANY_NEVER_WRITABLE = frozenset(
    {"name", "domain", "hubspot_owner_id", "lifecyclestage", "hs_lead_status"}
)


def configured_vendor_fields() -> list[str]:
    """The configured vendor fields, DEFAULT then EXTENDED, each once."""
    config = get_hubspot_config()
    # A property can appear in both lists while an admin moves it from
    # extended to default; expose it once to the extractor and write fence.
    return list(
        dict.fromkeys(
            (*config.ccs_vendor_fields_default, *config.ccs_vendor_fields_extended)
        )
    )


def company_writable() -> frozenset[str]:
    """The company properties ``update_company`` may write (see above)."""
    return (
        frozenset(configured_vendor_fields())
        - COMPANY_NEVER_WRITABLE
        - {RESEARCH_LINK_PROPERTY}
    )


# ── Customer X-Ray research link (UC-45) ─────────────────────────────────────
# Deliberately NOT part of company_writable(). That set is the fence around
# what Sales Call Capture's LLM extractor may propose from a transcript; this
# property is not a vendor fact anyone says out loud on a call, and nothing an
# extractor produces should ever be able to reach it. Keeping it in its own
# one-element fence means widening the CCS surface can never widen this one, and
# widening this one can never widen CCS.
#
# The value is a URL Cubie builds itself from FRONTEND_URL + the HubSpot company
# id — never model output. See rubik_core/xray/crm_link.py for the caller and
# the reasons it is the only one.
RESEARCH_LINK_PROPERTY = "cubie_research_link"
RESEARCH_LINK_WRITABLE = frozenset({RESEARCH_LINK_PROPERTY})

# Call engagements. HubSpot requires only `hs_timestamp`.
# `hs_call_duration` is MILLISECONDS: a caller that passes seconds logs every
# call as under a second, and nothing on either side rejects it.
CALL_WRITABLE = frozenset(
    {
        "hs_timestamp",
        "hs_call_title",
        "hs_call_body",
        "hs_call_duration",
        "hs_call_direction",
        "hs_call_status",
        "hs_call_source",
        "hubspot_owner_id",
    }
)

# Tasks. `hs_timestamp` on a task is the DUE DATE, not a creation time —
# HubSpot reuses one property name across activity objects with different
# meanings, so a task stamped "now" is overdue the moment it exists.
TASK_WRITABLE = frozenset(
    {
        "hs_timestamp",
        "hs_task_subject",
        "hs_task_body",
        "hs_task_status",
        "hs_task_priority",
        "hs_task_type",
        "hubspot_owner_id",
    }
)

# Email engagements (Loose Ends, "Log it in HubSpot"). The AE's own SENT email,
# verbatim — subject, full text, HTML and the from / to / cc headers — exactly
# what HubSpot's Gmail extension logs (in a sample month, 648 of 727 logged
# outgoing emails came through that integration, with the body). Never a
# note and never model text: Fathom owns call notes. `hs_timestamp` is the
# moment the email was SENT, so the contact's "last contacted" moves to the
# real date. `hs_email_direction` must be "EMAIL" (outgoing); `create_email`
# refuses anything else. `hubspot_owner_id` is whitelisted so the method can
# stamp it, and is always overwritten from the AE's address, never trusted.
EMAIL_WRITABLE = frozenset(
    {
        "hs_timestamp",
        "hs_email_direction",
        "hs_email_status",
        "hs_email_subject",
        "hs_email_text",
        "hs_email_html",
        "hs_email_headers",
        "hubspot_owner_id",
    }
)

# Leads: HubSpot's built-in lead properties only. `hs_lead_label` is HOT /
# WARM / COLD. A portal's own lead picklists stay out until a caller needs one:
# its options would have to be matched live through a leads accessor on
# `rubik_core/call_capture/hubspot_schema.py`, which today constrains companies
# only, because free-texting an enumeration is exactly what that module exists
# to stop.
LEAD_WRITABLE = frozenset({"hs_lead_name", "hubspot_owner_id", "hs_lead_label"})

# NOT added here, on purpose: a second deal-writing method for the cockpit's
# one-click stage advance. `DEAL_UPDATE_WRITABLE` above already contains
# `dealstage`, and `update_deal` already re-checks pipeline, ownership and
# stage-belongs-to-pipeline against LIVE state — so the cockpit calls that.
# A parallel stage path would be a second place to forget one of those gates.

# Association type ids (HUBSPOT_DEFINED). Verify once against the live portal
# before enabling the feature flag — they are portal-independent for standard
# objects, but a wrong id silently produces an unassociated record.
_ASSOC_DEAL_TO_CONTACT = 3
_ASSOC_DEAL_TO_COMPANY = 341

# Same UNVERIFIED status, from HubSpot's documentation ("Association type IDs") and not
# a read of the portal — and note that reference lists a DIFFERENT set from the two ids
# above, which is itself a reason to distrust both until a read-only preflight confirms
# them. A wrong id here does not fail: the call or task is created, lands attached to
# nothing, and the AE is told the write succeeded.
_ASSOC_CALL_TO_CONTACT = 194
_ASSOC_CALL_TO_COMPANY = 182
_ASSOC_CALL_TO_DEAL = 206
_ASSOC_TASK_TO_CONTACT = 204
_ASSOC_TASK_TO_COMPANY = 192
_ASSOC_TASK_TO_DEAL = 216
# Email → contact / company / deal, HubSpot's documented HUBSPOT_DEFINED
# defaults — unverified on this portal, the same status as the call and task
# ids above, with the same failure mode: a wrong id creates an email attached
# to nothing while the AE is told it was logged.
_ASSOC_EMAIL_TO_CONTACT = 198
_ASSOC_EMAIL_TO_COMPANY = 186
_ASSOC_EMAIL_TO_DEAL = 210

# Lead → Contact. HubSpot's docs give both 578 and 608, and a portal can carry
# both: `GET
# /crm/v4/associations/leads/contacts/labels` returns 578 = HUBSPOT_DEFINED
# "Primary" and 608 = HUBSPOT_DEFINED with no label. HubSpot deletes a lead that
# ends up with no PRIMARY association, so 578 is the only safe choice; 608
# would return 200 with an id for a record that evaporates. The `None` guard in
# `create_lead` stays: a future portal survey that cannot confirm the label can
# set this back to None and the writer fails closed again.
_ASSOC_LEAD_TO_CONTACT_CANDIDATES = (578, 608)
_ASSOC_LEAD_TO_CONTACT: int | None = 578

# Returned in ``_mode`` so the caller can tell "we created it" from "it was
# already there" — the difference between a real write and a resolved retry.
MODE_CREATED = "created"
MODE_UPDATED = "updated"
MODE_ALREADY_EXISTS = "already_exists"
MODE_EXISTING_UPDATED = "existing_updated"

# How far back the create-deal probe looks for an identical deal. Long enough to
# swallow a retry or a same-week duplicate scan, short enough that legitimately
# re-opening a similar deal next quarter still creates a new record.
_DEAL_PROBE_WINDOW_DAYS = 14

# How far either side of an email's sent time the duplicate probe looks for an
# email engagement already logged for it (by HubSpot's Gmail extension, or by
# an earlier "Log it in HubSpot" whose response was lost). The extension stamps
# the send time; five minutes absorbs clock and queue skew without catching a
# different email to the same person.
_EMAIL_PROBE_WINDOW = timedelta(minutes=5)


def _is_transient(msg: str) -> bool:
    """A rate limit, a 5xx or a transport error — worth another try.

    Matches ``"HTTP 5"`` (every error here is formatted ``HTTP <status>``),
    never a bare ``" 5"``, which also matched a record id or body text that
    happened to contain one ("read of deals 5…" retried a 404 four times).
    """
    return "429" in msg or "HTTP 5" in msg or "transport" in msg.lower()


def _is_rate_limited(msg: str) -> bool:
    """Only a 429: HubSpot refused the request before doing anything.

    The retry rule for a create, which has no idempotency key. A timeout or a
    5xx can arrive AFTER HubSpot stored the record, so retrying those would
    write it twice; they raise instead and the caller's probe decides.
    """
    return "HTTP 429" in msg


def _with_backoff(
    fn,
    *args,
    retries: int = 4,
    base_delay: float = 1.0,
    retry_if=_is_transient,
    **kwargs,
):
    """Retry transient HubSpot failures with exponential backoff.

    Deliberately a local copy of the X-Ray fetcher's helper rather than an
    import: ``retrieval`` is infrastructure and must not depend on a feature
    package. ``HubSpotWriteRefused`` is never retried — a gate rejection is a
    verdict, not a blip. ``retry_if`` decides which error messages are worth
    another try (``_is_transient`` by default; ``_is_rate_limited`` for a
    create that must not be sent twice).
    """
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except HubSpotWriteRefused:
            raise
        except (HubSpotWriteError, HubSpotAPIError) as exc:
            last = exc
            if not retry_if(str(exc)) or attempt == retries:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))
    if last:
        raise last


def friendly_write_error(label: str, exc: Exception) -> str:
    """Turn a raw HubSpot error into something an AE can act on.

    A raw 403 body tells a salesperson nothing; the actual cause is almost
    always a missing scope on the write app. This string is what gets stored on
    the suggestion row and shown in the UI — the raw body never is.
    """
    msg = str(exc)
    if isinstance(exc, HubSpotWriteRefused):
        return str(exc)
    if "403" in msg:
        return (
            f"Couldn't update {label} — the HubSpot write token is missing the "
            f"write scope for {label}. An admin needs to add it to the {PRODUCT_NAME} "
            f"write private app."
        )
    if "401" in msg:
        return f"Couldn't update {label} — the HubSpot write token was rejected."
    if "429" in msg:
        return f"Couldn't update {label} — HubSpot is rate-limiting; try again in a minute."
    if "404" in msg:
        return f"Couldn't update {label} — the record no longer exists in HubSpot."
    if "400" in msg:
        # HubSpot rejects values its own property validation doesn't like — a
        # phone number it can't parse, a malformed date. Transcripts produce
        # those routinely, so the AE needs to know WHICH value it objected to;
        # a bare "the request failed" leaves them with nothing to correct.
        reason = _validation_reason(msg)
        if reason:
            return f"HubSpot rejected the value for {label}: {reason}"
        return f"HubSpot rejected the value for {label} as invalid."
    return f"Couldn't update {label} — the HubSpot request failed."


def _validation_reason(raw: str) -> str | None:
    """Pull HubSpot's own validation complaint out of a 400 body.

    The useful text is nested inside the error body's per-property list, so the
    LAST ``"message"`` is the specific one ("Number isn't valid for country
    code: +1.") while the first is the generic wrapper.
    """
    found = re.findall(r'\\?"message\\?":\s*\\?"(.*?)\\?"', raw)
    if not found:
        return None
    specific = found[-1].strip()
    if specific.lower().startswith("property values were not valid") and len(found) > 1:
        specific = found[-2].strip()
    return specific[:200] or None


def _epoch_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _hs_ms(value: Any) -> int | None:
    """A HubSpot datetime property (epoch ms, or ISO 8601 as the search API
    returns ``hs_timestamp``) as epoch ms; None when it cannot be read."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    try:
        return _epoch_ms(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _in_cloud_run_job() -> bool:
    return bool(
        os.environ.get("CLOUD_RUN_JOB") or os.environ.get("CLOUD_RUN_EXECUTION")
    )


class HubSpotWriteClient:
    """Synchronous, gated writer over the HubSpot CRM v3 REST API."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
        read_client: HubSpotAPIClient | None = None,
    ):
        # No fallback to the read token or process settings, by design.
        if token is not None:
            self.token = token
        elif _in_cloud_run_job():
            self.token = None
        else:
            self.token = workspace_secret("hubspot", "write_token")
        self.token = self.token.strip() if self.token else None
        self.base_url = (base_url or settings.HUBSPOT_API_BASE).rstrip("/")
        self._read_client = read_client
        # ``resolve_owner_id`` answers, per address, for this client's life (one
        # request): a click that logs twenty emails reads the owner list once,
        # not twenty times.
        self._owner_ids: dict[str, str | None] = {}

    # ── plumbing ─────────────────────────────────────────────────────────────

    @property
    def read_client(self) -> HubSpotAPIClient:
        """Read-token client used for gates and idempotency probes."""
        if self._read_client is None:
            self._read_client = HubSpotAPIClient()
        return self._read_client

    def _require_token(self) -> str:
        if _in_cloud_run_job():
            raise HubSpotWriteRefused(
                "HubSpot writes are unavailable in a Cloud Run Job."
            )
        if not self.token:
            raise HubSpotWriteRefused(
                "HubSpot writes are not configured (workspace write token is unset)."
            )
        return self.token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._require_token()}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.request(
                    method, url, headers=self._headers(), json=payload
                )
                resp.raise_for_status()
                return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            body_txt = e.response.text[:300] if e.response is not None else ""
            raise HubSpotWriteError(
                f"HubSpot {method} {path} failed: HTTP {status}: {body_txt}"
            ) from e
        except httpx.HTTPError as e:
            raise HubSpotWriteError(
                f"HubSpot transport error: {safe_error(e)}"
            ) from None

    def get_object(
        self, object_type: str, object_id: str, properties: list[str]
    ) -> dict:
        """Fetch live state only after the workspace ownership proof succeeds."""
        record_id = str(object_id or "").strip()
        if not record_id or not re.fullmatch(r"[A-Za-z0-9_-]+", record_id):
            raise HubSpotWriteRefused("Invalid HubSpot record ID.")
        if object_type == "deals":
            if not write_pipelines():
                raise HubSpotPipelineRefused(PIPELINES_NOT_SET_UP)
            wanted_properties = list(dict.fromkeys([*properties, "pipeline"]))
        elif object_type in ("companies", "contacts"):
            self._require_owned_record(object_type, record_id)
            wanted_properties = properties
        else:
            raise HubSpotWriteRefused("This HubSpot object type cannot be read here.")
        url = f"{self.base_url}/crm/v3/objects/{object_type}/{record_id}"
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(
                    url,
                    headers=self.read_client._headers(),
                    params={"properties": ",".join(wanted_properties)},
                )
                resp.raise_for_status()
                record = resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            # Carry the body like `_request` does. A 400 here is usually "you
            # asked for a property that does not exist on this portal", and
            # without the body that is indistinguishable from a malformed id.
            body_txt = e.response.text[:300] if e.response is not None else ""
            raise HubSpotWriteError(
                f"HubSpot read of {object_type} {object_id} failed: "
                f"HTTP {status}: {body_txt}"
            ) from e
        except httpx.HTTPError as e:
            raise HubSpotWriteError(
                f"HubSpot transport error: {safe_error(e)}"
            ) from None
        if not isinstance(record, dict):
            raise HubSpotWriteError("HubSpot returned an invalid record")
        if object_type == "deals":
            self._pipeline_gate((record.get("properties") or {}).get("pipeline"))
        return record

    # ── gates ────────────────────────────────────────────────────────────────

    @staticmethod
    def _pipeline_gate(pipeline: str | None) -> None:
        """Confine deal writes to the company's own pipelines.

        Fails CLOSED on an empty allowlist, with :data:`PIPELINES_NOT_SET_UP`.
        A blank setting also blocks deal reads, so neither side can expose or
        mutate a client's pipeline before an admin chooses the allowed list.
        A deal with no pipeline is refused too: there is nothing to check it
        against.
        """
        if not write_pipelines():
            raise HubSpotPipelineRefused(PIPELINES_NOT_SET_UP)
        if not pipeline_writable(pipeline):
            raise HubSpotPipelineRefused(
                f"Deal writes are not allowed in pipeline {pipeline!r} — "
                f"CRM Sync only writes to the company's own pipelines."
            )

    def _require_owned_record(self, object_type: str, object_id: str) -> None:
        """Prove a company/contact has a live deal in this workspace's pipelines."""
        self._require_owned_records(object_type, [object_id])

    def _require_owned_records(self, object_type: str, object_ids: list[str]) -> None:
        """Prove every requested association target belongs to this workspace."""
        if object_type not in ("companies", "contacts"):
            raise ValueError("Ownership proof requires a company or contact")
        wanted = list(dict.fromkeys(str(value or "").strip() for value in object_ids))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in wanted):
            raise HubSpotWriteRefused("Invalid HubSpot record ID.")
        if not wanted:
            return
        if not write_pipelines():
            raise HubSpotPipelineRefused(PIPELINES_NOT_SET_UP)
        try:
            linked = _with_backoff(
                self.read_client.fully_owned_deal_associations,
                object_type,
                wanted,
            )
        except HubSpotAPIError:
            raise HubSpotWriteRefused(
                "Could not verify HubSpot record ownership for this workspace; "
                "nothing was changed."
            ) from None
        if not isinstance(linked, dict) or any(
            not isinstance(linked.get(record_id), (tuple, list))
            or not linked[record_id]
            for record_id in wanted
        ):
            kind = "company" if object_type == "companies" else "contact"
            raise HubSpotWriteRefused(
                f"A HubSpot {kind} has no deal in this workspace's pipelines."
            )

    @staticmethod
    def _property_gate(label: str, properties: dict, whitelist: frozenset) -> None:
        if not properties:
            raise HubSpotWriteRefused(f"No {label} properties to write.")
        unknown = sorted(set(properties) - set(whitelist))
        if unknown:
            raise HubSpotWriteRefused(
                f"Refusing to write non-approved {label} properties: {', '.join(unknown)}"
            )

    def resolve_owner_id(self, email: str) -> str | None:
        """HubSpot owner id for a company email (exact, lowercase). None if absent."""
        want = (email or "").strip().lower()
        if not want:
            return None
        cache = getattr(self, "_owner_ids", None)
        if cache is not None and want in cache:
            return cache[want]
        found: str | None = None
        for oid, info in self.read_client.owner_directory().items():
            if (info.get("email") or "").lower() == want:
                found = str(oid)
                break
        if cache is not None:
            cache[want] = found
        return found

    def _stage_gate(self, pipeline: str, stage_id: str) -> None:
        """A stage id must belong to the pipeline it is being written into.

        Stage ids are shared across the company's pipelines, so "is this a real stage"
        is not a sufficient check — it has to be a real stage *of this deal's
        pipeline* or the deal lands in an incoherent state.
        """
        stages = self.read_client.deal_pipeline_stages().get(str(pipeline)) or {}
        if not stages:
            raise HubSpotWriteRefused(
                f"Couldn't verify deal stages for pipeline {pipeline!r}; refusing the write."
            )
        if str(stage_id) not in stages:
            raise HubSpotWriteRefused(
                f"Stage {stage_id!r} does not belong to pipeline {pipeline!r}."
            )

    def _gate_deal_association(self, deal_id: str | None) -> None:
        """Fence a write that attaches an activity to a deal.

        Associating a call or a task to a deal writes onto that deal's record,
        so it gets the same pipeline gate as editing one — otherwise activities
        are the writes that could land on a pipeline the company does not own
        while the deal-property paths are fenced. Reads live state on purpose: a
        stored deal id is only ever a claim about which pipeline the deal is in.

        Until an admin sets up the pipelines, every deal-touching read and
        write here is refused before a HubSpot request.

        A deal that no longer exists (HTTP 404: deleted or merged since its id
        was stored) is ``HubSpotDealGone``, a ``HubSpotPipelineRefused``: the
        activity cannot go on that deal, and a caller that drops a refused deal
        and keeps the contact can do the same here instead of failing forever.
        """
        if not deal_id:
            return
        if not write_pipelines():
            raise HubSpotPipelineRefused(PIPELINES_NOT_SET_UP)
        try:
            live = _with_backoff(self.get_object, "deals", deal_id, ["pipeline"])
        except HubSpotWriteRefused:
            raise
        except HubSpotWriteError as exc:
            if "HTTP 404" in str(exc):
                raise HubSpotDealGone(
                    "That deal no longer exists in HubSpot (it was deleted or "
                    "merged), so nothing can be attached to it."
                ) from None
            raise
        self._pipeline_gate((live.get("properties") or {}).get("pipeline"))

    # ── idempotency probes ───────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> dict | None:
        if not write_pipelines():
            raise HubSpotPipelineRefused(PIPELINES_NOT_SET_UP)
        results = self.read_client.search_all(
            "contacts",
            [{"propertyName": "email", "operator": "EQ", "value": email.lower()}],
            ["email", "firstname", "lastname", "phone", "mobilephone", "jobtitle"],
        )
        if not results:
            return None
        self._require_owned_record("contacts", str(results[0].get("id") or ""))
        return results[0]

    def find_recent_deal(
        self, dealname: str, pipeline: str, owner_id: str
    ) -> dict | None:
        """An identical deal this AE created very recently — i.e. a duplicate."""
        cutoff = _epoch_ms(
            datetime.now(timezone.utc) - timedelta(days=_DEAL_PROBE_WINDOW_DAYS)
        )
        results = self.read_client.search_all(
            "deals",
            [
                {"propertyName": "dealname", "operator": "EQ", "value": dealname},
                {"propertyName": "pipeline", "operator": "EQ", "value": str(pipeline)},
                {
                    "propertyName": "hubspot_owner_id",
                    "operator": "EQ",
                    "value": str(owner_id),
                },
                {
                    "propertyName": "createdate",
                    "operator": "GTE",
                    "value": str(cutoff),
                },
            ],
            ["dealname", "pipeline", "dealstage", "amount", "closedate"],
        )
        return results[0] if results else None

    def find_logged_email(
        self,
        *,
        contact_ids: list[str],
        subject: str,
        sent_at: datetime,
        exclude: set[str] | frozenset[str] = frozenset(),
    ) -> str | None:
        """The id of an email engagement already logged for this sent email.

        HubSpot has no idempotency keys, so ``create_email`` callers MUST ask
        this first (Loose Ends does, both for the preview's "Already in
        HubSpot" and right before the write). A hit is an email engagement
        stamped within ``_EMAIL_PROBE_WINDOW`` of ``sent_at``, with the same
        subject, associated with any of ``contact_ids`` — whether HubSpot's
        Gmail extension logged it or an earlier click whose response was lost.
        The closest in time wins (Cubie stamps the exact sent time, so its own
        write is an exact match); ids in ``exclude`` — already claimed by
        another email of the same thread — are skipped, so one HubSpot email
        never stands for two sent ones. See :meth:`logged_email_candidates`.

        Reads with the READ token. Errors raise (``HubSpotAPIError``): the
        caller decides, and "could not check" must never read as "not there",
        which would log the email twice.
        """
        for email_id, _distance in self.logged_email_candidates(
            contact_ids=contact_ids, subject=subject, sent_at=sent_at
        ):
            if email_id not in exclude:
                return email_id
        return None

    def logged_email_candidates(
        self, *, contact_ids: list[str], subject: str, sent_at: datetime
    ) -> list[tuple[str, int]]:
        """Every email engagement that could be this sent email, closest first.

        ``[(hubspot_email_id, |hs_timestamp − sent_at| in ms)]``: stamped within
        ``_EMAIL_PROBE_WINDOW`` of ``sent_at``, with the same subject, and
        associated with any of ``contact_ids``. A caller matching several sent
        emails at once (two "Re: Proposal" sent three minutes apart) assigns
        them one to one from these lists; :meth:`find_logged_email` is the
        single-email form.

        A blank subject cannot be matched with EQ, so it is left out of the
        search and the window plus the contact decide alone. A row whose
        timestamp cannot be read ranks after every row whose can.

        The search and the association read back off on a 429 or a 5xx (both
        are reads, safe to repeat; HubSpot's search limit is per second). Any
        error that survives raises.
        """
        wanted = {str(c).strip() for c in contact_ids or [] if str(c or "").strip()}
        if not wanted:
            return []
        self._require_owned_records("contacts", sorted(wanted))
        sent_ms = _epoch_ms(sent_at)
        lo = _epoch_ms(sent_at - _EMAIL_PROBE_WINDOW)
        hi = _epoch_ms(sent_at + _EMAIL_PROBE_WINDOW)
        filters: list[dict[str, Any]] = [
            {
                "propertyName": "hs_timestamp",
                "operator": "BETWEEN",
                "value": str(lo),
                "highValue": str(hi),
            }
        ]
        if (subject or "").strip():
            filters.append(
                {
                    "propertyName": "hs_email_subject",
                    "operator": "EQ",
                    "value": subject.strip(),
                }
            )
        rows = _with_backoff(
            self.read_client.search_all,
            "emails",
            filters,
            ["hs_timestamp", "hs_email_subject"],
        )
        unknown = int(_EMAIL_PROBE_WINDOW.total_seconds() * 1000) + 1
        distance: dict[str, int] = {}
        for row in rows:
            if not row.get("id"):
                continue
            stamped = _hs_ms((row.get("properties") or {}).get("hs_timestamp"))
            distance.setdefault(
                str(row["id"]),
                abs(stamped - sent_ms) if stamped is not None else unknown,
            )
        if not distance:
            return []
        ids = list(distance)
        linked = _with_backoff(
            self.read_client.batch_associations, "emails", "contacts", ids
        )
        hits = [
            (email_id, distance[email_id])
            for email_id in ids
            if wanted.intersection(str(c) for c in linked.get(email_id) or [])
        ]
        # Stable: equal distances keep HubSpot's order.
        return sorted(hits, key=lambda hit: hit[1])

    # ── writes ───────────────────────────────────────────────────────────────

    def create_contact(self, properties: dict, *, owner_email: str) -> dict:
        """Update an owned match; new unbound contacts need a future binding."""
        self._property_gate("contact", properties, CONTACT_WRITABLE)
        self._require_token()
        if not write_pipelines():
            raise HubSpotPipelineRefused(PIPELINES_NOT_SET_UP)
        email = (properties.get("email") or "").strip().lower()
        if not email:
            raise HubSpotWriteRefused("Cannot create a contact without an email.")
        props = {**properties, "email": email}

        # What we'd send if this turns out to be an existing contact. Identity
        # (email) and ownership are excluded: the record already has an owner,
        # and silently reassigning someone else's contact is not a correction.
        def _correctable() -> dict:
            return {
                k: v for k, v in props.items() if k not in ("email", "hubspot_owner_id")
            }

        existing = _with_backoff(self.find_contact_by_email, email)
        if existing:
            updated = self.update_contact(str(existing["id"]), _correctable())
            return {**updated, "_mode": MODE_EXISTING_UPDATED}

        # A new contact has no in-scope deal to prove ownership in a shared
        # portal. A future explicit workspace prospect binding may permit it.
        raise HubSpotWriteRefused(
            "Cannot create an unbound HubSpot contact until this workspace has "
            "an explicit prospect binding."
        )

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        self._property_gate("contact", properties, CONTACT_UPDATE_WRITABLE)
        self._require_token()
        self._require_owned_record("contacts", contact_id)
        updated = _with_backoff(
            self._request,
            "PATCH",
            f"/crm/v3/objects/contacts/{contact_id}",
            {"properties": properties},
        )
        return {**updated, "_mode": MODE_UPDATED}

    def create_deal(
        self,
        properties: dict,
        *,
        owner_email: str,
        company_id: str | None = None,
        contact_ids: list[str] | None = None,
    ) -> dict:
        self._property_gate("deal", properties, DEAL_CREATE_WRITABLE)
        self._require_token()
        pipeline = properties.get("pipeline")
        if not pipeline:
            # Never defaulted: picking a pipeline for the user is exactly the
            # decision the allowlist exists to keep explicit.
            raise HubSpotWriteRefused(
                "Cannot create a deal without an explicit pipeline."
            )
        self._pipeline_gate(pipeline)
        if company_id:
            self._require_owned_record("companies", company_id)
        self._require_owned_records("contacts", contact_ids or [])
        if properties.get("dealstage"):
            self._stage_gate(str(pipeline), str(properties["dealstage"]))

        owner_id = self.resolve_owner_id(owner_email)
        if not owner_id:
            raise HubSpotWriteRefused(
                f"Cannot create a deal: {owner_email} is not a HubSpot deal owner."
            )

        dealname = properties.get("dealname") or ""
        existing = _with_backoff(
            self.find_recent_deal, dealname, str(pipeline), owner_id
        )
        if existing:
            return {**existing, "_mode": MODE_ALREADY_EXISTS}

        props = {**properties, "hubspot_owner_id": owner_id}
        associations = []
        for cid in contact_ids or []:
            associations.append(_assoc(cid, _ASSOC_DEAL_TO_CONTACT))
        if company_id:
            associations.append(_assoc(company_id, _ASSOC_DEAL_TO_COMPANY))

        body: dict[str, Any] = {"properties": props}
        if associations:
            body["associations"] = associations
        created = _with_backoff(self._request, "POST", "/crm/v3/objects/deals", body)
        return {**created, "_mode": MODE_CREATED}

    def update_deal(
        self, deal_id: str, properties: dict, *, acting_owner_email: str
    ) -> dict:
        """Update a deal the acting AE owns, in one of the company's pipelines.

        Every gate here reads live HubSpot state rather than trusting whatever
        the suggestion row recorded at scan time — the deal may have moved,
        changed hands, or (in the worst case) never been what the row claimed.
        """
        self._property_gate("deal", properties, DEAL_UPDATE_WRITABLE)
        self._require_token()
        if not write_pipelines():
            raise HubSpotPipelineRefused(PIPELINES_NOT_SET_UP)
        live = _with_backoff(
            self.get_object,
            "deals",
            deal_id,
            ["pipeline", "dealstage", "hubspot_owner_id", "dealname"],
        )
        live_props = live.get("properties") or {}
        self._pipeline_gate(live_props.get("pipeline"))

        owner_id = self.resolve_owner_id(acting_owner_email)
        if not owner_id:
            raise HubSpotWriteRefused(
                f"Cannot update a deal: {acting_owner_email} is not a HubSpot deal owner."
            )
        if str(live_props.get("hubspot_owner_id") or "") != owner_id:
            raise HubSpotWriteRefused("CRM Sync only updates deals you own in HubSpot.")
        if properties.get("dealstage"):
            self._stage_gate(
                str(live_props.get("pipeline")), str(properties["dealstage"])
            )

        updated = _with_backoff(
            self._request,
            "PATCH",
            f"/crm/v3/objects/deals/{deal_id}",
            {"properties": properties},
        )
        return {**updated, "_mode": MODE_UPDATED}

    # ── Sales Call Capture / Sales Cockpit writes ────────────────────────────

    def update_company(self, company_id: str, properties: dict) -> dict:
        """Write vendor / firmographic fields onto one company.

        A company has no pipeline property, so its live associated deals must
        prove that it belongs to this workspace before the PATCH.

        One PATCH carrying every accepted field, never one request per field:
        a dozen configured properties at one call each is exactly the pattern
        that burns the 190-per-10s budget.

        Overwrite protection is *not* here. Whether a proposed value replaces
        an existing one (`current_value` / `is_overwrite`, and the amber
        confirm the wrap-up screen requires for it) is decided at extraction
        and approval time; by the time a value reaches this method a human has
        already said yes to that specific change.
        """
        self._property_gate("company", properties, company_writable())
        self._require_token()
        self._require_owned_record("companies", company_id)
        updated = _with_backoff(
            self._request,
            "PATCH",
            f"/crm/v3/objects/companies/{company_id}",
            {"properties": properties},
        )
        return {**updated, "_mode": MODE_UPDATED}

    def update_company_research_link(self, company_id: str, url: str) -> dict:
        """Point one company at its Customer X-Ray page in Cubie.

        Its own method rather than a `update_company` call with one key, because
        the fence differs: `RESEARCH_LINK_WRITABLE`, not `company_writable()`. A
        caller that gets the property name wrong is refused here rather than
        writing a stray field, and the CCS extractor can never reach this
        property no matter what it proposes.

        Refuses a non-numeric `company_id`: HubSpot object ids are numeric, and
        this value is interpolated into both the request path and the URL that
        lands in the CRM. Anything else is a caller bug worth failing loudly on.
        """
        if not str(company_id).isdigit():
            raise HubSpotWriteRefused(
                f"Refusing a research-link write for non-numeric company id "
                f"{company_id!r}."
            )
        if not (url or "").startswith(("http://", "https://")):
            raise HubSpotWriteRefused(
                "Refusing to write a research link that is not an http(s) URL."
            )
        properties = {RESEARCH_LINK_PROPERTY: url}
        self._property_gate("company research link", properties, RESEARCH_LINK_WRITABLE)
        self._require_token()
        self._require_owned_record("companies", company_id)
        updated = _with_backoff(
            self._request,
            "PATCH",
            f"/crm/v3/objects/companies/{company_id}",
            {"properties": properties},
        )
        return {**updated, "_mode": MODE_UPDATED}

    def create_call(
        self,
        properties: dict,
        *,
        contact_id: str | None = None,
        company_id: str | None = None,
        deal_id: str | None = None,
    ) -> dict:
        """Log the call engagement itself — the write that always happens.

        First in the per-session write order and performed even
        when the extraction proposed nothing: the record that a call took place
        is the point, the proposals are the bonus.

        Only `hs_timestamp` is required. `hs_call_duration` is **milliseconds** —
        it is not converted here, because a bare integer carries no units and
        guessing at this boundary would corrupt correct callers.

        **No idempotency probe, unlike `create_deal`.** A call engagement has no
        natural uniqueness (two real calls to one company on one day are
        legitimate) and HubSpot has no idempotency key, so there is nothing
        honest to probe for. The duplicate guard is the caller's
        `call_hubspot_writes` row keyed on the extraction item; a retry path
        that skips that check logs the call twice.
        """
        self._property_gate("call", properties, CALL_WRITABLE)
        self._require_token()
        if not properties.get("hs_timestamp"):
            # The one property HubSpot requires. Its 400 body is unreadable to
            # an AE, so catch it here where the message can say what to fix.
            raise HubSpotWriteRefused("Cannot log a call without hs_timestamp.")
        self._gate_deal_association(deal_id)
        self._require_owned_records("contacts", [contact_id] if contact_id else [])
        self._require_owned_records("companies", [company_id] if company_id else [])

        associations = []
        if contact_id:
            associations.append(_assoc(contact_id, _ASSOC_CALL_TO_CONTACT))
        if company_id:
            associations.append(_assoc(company_id, _ASSOC_CALL_TO_COMPANY))
        if deal_id:
            associations.append(_assoc(deal_id, _ASSOC_CALL_TO_DEAL))
        if not associations:
            # Allowed, because "always log the call" outranks it — but an
            # engagement attached to nothing appears on no record in the CRM,
            # so it must not pass silently.
            logger.warning(
                "[ccs] logging a HubSpot call with no associations; it will not "
                "appear on any contact, company or deal record"
            )

        body: dict[str, Any] = {"properties": dict(properties)}
        if associations:
            body["associations"] = associations
        created = _with_backoff(self._request, "POST", "/crm/v3/objects/calls", body)
        return {**created, "_mode": MODE_CREATED}

    def create_task(
        self,
        properties: dict,
        *,
        owner_email: str,
        contact_id: str | None = None,
        company_id: str | None = None,
        deal_id: str | None = None,
    ) -> dict:
        """Create a task assigned to a named person — the cockpit's nudge write.

        `hs_timestamp` is the **due date** here, not a creation time. Stamping
        "now" produces a task that is overdue the instant it appears, which is
        how a nudge reads as an accusation instead of a reminder.

        Documented `hs_task_status` values are `COMPLETED` and `NOT_STARTED`
        only. The HubSpot UI offers more; the API reference does not, so anything
        else must be read off the portal's property options first — which is
        what `rubik_core/call_capture/hubspot_schema.py` is for.
        """
        self._property_gate("task", properties, TASK_WRITABLE)
        self._require_token()
        if not properties.get("hs_timestamp"):
            raise HubSpotWriteRefused(
                "Cannot create a task without hs_timestamp (the due date)."
            )
        self._gate_deal_association(deal_id)
        self._require_owned_records("contacts", [contact_id] if contact_id else [])
        self._require_owned_records("companies", [company_id] if company_id else [])

        # An unowned task sits in nobody's queue: it exists, and the person it
        # was meant for is never told. Landing in the AE's HubSpot worklist is
        # the entire purpose of the nudge, so an email that does not resolve to
        # a HubSpot owner is a refusal rather than a task created for no one.
        # An AE whose HubSpot email differs from their Workspace email is a
        # known live case, and it must never fail quietly.
        owner_id = self.resolve_owner_id(owner_email)
        if not owner_id:
            raise HubSpotWriteRefused(
                f"Cannot create a task: {owner_email} is not a HubSpot owner, so "
                f"the task would be assigned to nobody."
            )
        # `owner_email` is the authority. A caller-supplied hubspot_owner_id is
        # overwritten rather than trusted — the property is whitelisted so the
        # validators share one vocabulary, not so an upstream value can reassign
        # someone else's work.
        props = {**properties, "hubspot_owner_id": owner_id}

        associations = []
        if contact_id:
            associations.append(_assoc(contact_id, _ASSOC_TASK_TO_CONTACT))
        if company_id:
            associations.append(_assoc(company_id, _ASSOC_TASK_TO_COMPANY))
        if deal_id:
            associations.append(_assoc(deal_id, _ASSOC_TASK_TO_DEAL))

        body: dict[str, Any] = {"properties": props}
        if associations:
            body["associations"] = associations
        created = _with_backoff(self._request, "POST", "/crm/v3/objects/tasks", body)
        return {**created, "_mode": MODE_CREATED}

    def create_email(
        self,
        properties: dict,
        *,
        owner_email: str,
        contact_ids: list[str],
        company_id: str | None = None,
        deal_id: str | None = None,
    ) -> dict:
        """Log one email the AE SENT as a HubSpot email engagement.

        Loose Ends' one write ("Log it in HubSpot"). The properties carry the
        AE's own email verbatim — subject, full text, HTML, from / to / cc —
        read server-side from the AE's mailbox after the AE saw a preview of
        exactly these emails (``rubik_core/loop_closure/crm_log.py``). The same
        record HubSpot's Gmail extension writes, and it moves the contact's
        "last contacted" date, which a note never did.

        Gates, in order:

        * ``EMAIL_WRITABLE`` — nothing else can be written.
        * ``hs_timestamp`` is required (the sent time).
        * ``hs_email_direction`` must be ``"EMAIL"`` (outgoing). Cubie logs
          only what the AE sent; an inbound or forwarded direction is refused.
        * At least one contact: an email attached to nothing shows on no
          record while the AE is told it was logged.
        * A deal association passes the live pipeline gate
          (``HubSpotPipelineRefused`` when it does not, and its subclass
          ``HubSpotDealGone`` when the deal no longer exists — the caller may
          retry without the deal).

        ``hubspot_owner_id`` is always resolved from ``owner_email``, never
        taken from the caller. When the AE is not a HubSpot owner (their
        HubSpot address differs from their Workspace one) the email is still
        logged, unowned — it is a record of mail that was sent, and it still
        shows on the contact — and a warning says so.

        **No probe in here.** Callers MUST call :meth:`find_logged_email`
        (or :meth:`logged_email_candidates`) first and answer ``{"id":
        existing, "_mode": MODE_ALREADY_EXISTS}`` on a hit, because HubSpot
        has no idempotency key. For the same reason the POST is retried on a
        429 only, never after a timeout or a 5xx.
        """
        self._property_gate("email", properties, EMAIL_WRITABLE)
        self._require_token()
        if not properties.get("hs_timestamp"):
            raise HubSpotWriteRefused("Cannot log an email without hs_timestamp.")
        if properties.get("hs_email_direction") != "EMAIL":
            raise HubSpotWriteRefused(
                f"{PRODUCT_NAME} only logs an email the account executive sent "
                "(hs_email_direction must be EMAIL)."
            )
        contacts = list(
            dict.fromkeys(
                str(c).strip() for c in contact_ids or [] if str(c or "").strip()
            )
        )
        if not contacts:
            raise HubSpotWriteRefused(
                "Cannot log an email that is not attached to a contact: it would "
                "show on no record in HubSpot."
            )
        self._gate_deal_association(deal_id)
        self._require_owned_records("contacts", contacts)
        self._require_owned_records("companies", [company_id] if company_id else [])

        props = {k: v for k, v in properties.items() if k != "hubspot_owner_id"}
        owner_id = self.resolve_owner_id(owner_email)
        if owner_id:
            props["hubspot_owner_id"] = owner_id
        else:
            # The address is deliberately not logged (house rule: no client or
            # colleague addresses in logs); an AE whose HubSpot address differs
            # from their Workspace one is the known cause.
            logger.warning(
                "[hubspot-write] logging an email with no owner: the sender is "
                "not a HubSpot owner (HubSpot and Workspace addresses differ)"
            )

        associations = [_assoc(c, _ASSOC_EMAIL_TO_CONTACT) for c in contacts]
        if company_id:
            associations.append(_assoc(company_id, _ASSOC_EMAIL_TO_COMPANY))
        if deal_id:
            associations.append(_assoc(deal_id, _ASSOC_EMAIL_TO_DEAL))

        # Retried on a 429 only: a timeout or a 5xx can come back after HubSpot
        # stored the email, and a blind second POST would log it twice. Those
        # raise, and the caller's next probe decides.
        created = _with_backoff(
            self._request,
            "POST",
            "/crm/v3/objects/emails",
            {"properties": props, "associations": associations},
            retry_if=_is_rate_limited,
        )
        return {**created, "_mode": MODE_CREATED}

    def create_lead(
        self, properties: dict, *, contact_id: str, owner_email: str
    ) -> dict:
        """Create a lead, associated as PRIMARY to an existing contact.

        Two guards, both fail-closed:

        * `settings.CCS_LEADS_ENABLED` — the kill switch for every lead write.
          The LEAD object reads `REQUIRES_REAUTHORIZATION` until the lead
          scopes are granted on the private app; the flag defaults on.
        * `_ASSOC_LEAD_TO_CONTACT` — 578 ("Primary", HubSpot-defined). If it
          is ever unset again this method refuses rather than guessing between
          the two documented ids.

        The second guard is the dangerous one, and it is why this method fails
        closed twice. A lead **must** be associated to an existing contact at
        creation, and HubSpot's documentation states that if you remove all
        primary associations to a lead, the lead is automatically deleted. So a
        wrong association id does not raise — it returns 200 and an id for a
        record that has already evaporated.

        That same rule is why there is deliberately **no `update_lead` and no
        association-removal path** in this module: the only safe lead mutation
        is one that cannot orphan the record, and no caller needs one yet.
        """
        if not settings.CCS_LEADS_ENABLED:
            raise HubSpotWriteRefused(
                "Lead creation is disabled (CCS_LEADS_ENABLED is off)."
            )
        if _ASSOC_LEAD_TO_CONTACT is None:
            raise HubSpotWriteRefused(
                "Lead creation is blocked: the Lead→Contact association type id "
                "is unverified (candidates "
                f"{' or '.join(str(c) for c in _ASSOC_LEAD_TO_CONTACT_CANDIDATES)}), "
                "and HubSpot auto-deletes a lead that has no primary association."
            )
        self._property_gate("lead", properties, LEAD_WRITABLE)
        self._require_token()
        if not (properties.get("hs_lead_name") or "").strip():
            raise HubSpotWriteRefused("Cannot create a lead without hs_lead_name.")
        if not str(contact_id or "").strip():
            raise HubSpotWriteRefused(
                "Cannot create a lead without a contact to associate it to — "
                "HubSpot deletes a lead that has no primary association."
            )
        self._require_owned_record("contacts", contact_id)

        props = dict(properties)
        owner_id = self.resolve_owner_id(owner_email)
        if owner_id:
            props["hubspot_owner_id"] = owner_id
        else:
            # Not a refusal, unlike a task: a lead with no owner is still
            # visible in the CRM, and whether `hubspot_owner_id` is even
            # required on lead creation is undocumented — refusing on an
            # undocumented requirement would be inventing one.
            logger.warning(
                "[ccs] creating a HubSpot lead with no owner: %s is not a "
                "HubSpot owner",
                owner_email,
            )

        created = _with_backoff(
            self._request,
            "POST",
            "/crm/v3/objects/leads",
            {
                "properties": props,
                "associations": [_assoc(contact_id, _ASSOC_LEAD_TO_CONTACT)],
            },
        )
        return {**created, "_mode": MODE_CREATED}


def _assoc(target_id: str, type_id: int) -> dict:
    return {
        "to": {"id": str(target_id)},
        "types": [
            {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}
        ],
    }
