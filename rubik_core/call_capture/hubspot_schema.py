"""HubSpot property-option cache — the heard-spelling fix.

An auto transcript spells a vendor name the way it sounded, so a name spelled
with a C on the vendor's own site comes out with an S, and a later search of
the notes for the real spelling misses it.

A vendor name the model spells the way it was *heard* is worse than no value: it
writes a string nobody will ever search for into the field the whole feature
exists to fill. So the extractor is not allowed to free-text an enumeration
property. It is handed the live `options[]` for each one and must either map the
value it heard onto a real option or return null plus `needs_new_option` — the
heard spelling, routed to the sales supervisor's triage list rather than into
HubSpot.

Hardcoding a snapshot of those options would defeat the point, which is why no
contract lists them: the whole mechanism is that they are read live. Nothing
like this existed in the repo — no `/crm/v3/properties/*` or
`/crm-object-schemas/*` call anywhere — so this module is new (contract C7),
modelled on the 30-minute `_DIR_CACHE` in `backend/signals/hubspot.py`: a
module-level dict plus a TTL.

**Two traps, both silent:**

* An empty list means **unconstrained**, not "no valid values". A free-text
  property (a licence number, a system name) legitimately has no options, and
  a caller that reads `[]` as "nothing may be written here" blocks every
  free-text vendor field while reporting success.
* A failed fetch therefore may **not** degrade to `{}`. That would read as
  "everything is unconstrained" and reintroduce exactly the bug above, so a
  refresh that fails with no cached copy to fall back on **raises**.

Call `allowed_values_for` **once per run and pass the result down.** The TTL
cache makes a per-record call cheap inside one process, but the process boundary
is where that guarantee ends, and account-wide lookups repeated per record are
the calls that burn HubSpot's rate limit (`rubik_core/crm_sync/service.py`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from hashlib import sha256

import httpx

from rubik_core.retrieval.hubspot_api import HubSpotAPIClient, HubSpotAPIError
from rubik_core.retrieval.hubspot_write import _with_backoff
from rubik_core.tenancy.context import NoWorkspaceContext, current_workspace
from rubik_core.tenancy.secrets import workspace_secret

logger = logging.getLogger(__name__)

# Every property that goes through here today is a company vendor /
# firmographic field (`company_writable()` in `hubspot_write.py`). The object type
# is fixed rather than a parameter so no caller can quietly ask for company
# options and get a deal's: when a second object needs constraining, add an
# explicit accessor for it and give the cache a per-object key.
_OBJECT_TYPE = "companies"

# One hour. Longer than `_DIR_CACHE`'s 30 minutes because property definitions
# change when someone edits a picklist by hand — measured in weeks — and much
# shorter than "until the process restarts", because the `needs_new_option`
# triage loop ends with the sales supervisor adding an option in HubSpot and
# needing Cubie to see it. `invalidate_cache()` is the fast path for that.
_SCHEMA_CACHE_TTL_S = 3600

# Workspace -> (all company property options, fetched_at, read-token fingerprint).
# A portal's options must never be offered to a different workspace or after
# the current workspace's HubSpot token changes.
_SCHEMA_CACHE: dict[str, tuple[dict[str, list[str]], datetime, str]] = {}


def invalidate_cache() -> None:
    """Drop this workspace's option map so the next call re-fetches.

    Two real callers: tests, and the `needs_new_option` triage flow — once a
    new vendor option is added in HubSpot, the extractor keeps rejecting that
    value for up to an hour unless someone clears this.
    """
    try:
        workspace = current_workspace()
    except NoWorkspaceContext:
        _SCHEMA_CACHE.clear()
        return
    _SCHEMA_CACHE.pop(workspace, None)


def allowed_values_for(properties: list[str]) -> dict[str, list[str]]:
    """Live enumeration options for each requested company property.

    Returns ``{property_name: [option value, ...]}`` with **one key per
    requested property**, so a caller can iterate the request rather than
    guessing which names survived. An empty list means the property is
    unconstrained free text — see the module docstring; it does not mean
    "nothing may be written".

    Option *values* are returned, not labels: a value is what a write sends.
    On this portal the two are usually identical for vendor fields, but where
    they differ, the label is the thing a human typed and the value is the thing
    the API accepts.

    Raises ``HubSpotAPIError`` when the definitions cannot be fetched and no
    cached copy exists.
    """
    option_map = _option_map()
    out: dict[str, list[str]] = {}
    unknown: list[str] = []
    for name in properties:
        prop = (name or "").strip()
        if not prop:
            continue
        if prop in option_map:
            out[prop] = list(option_map[prop])
            continue
        # Absent from the fetched schema entirely. Indistinguishable from a
        # free-text property in the return value, which is why it is logged:
        # the usual cause is a typo in the workspace's vendor fields, and the
        # symptom would otherwise be the extractor happily proposing values for
        # a property that 400s at write time.
        unknown.append(prop)
        out[prop] = []
    if unknown:
        logger.warning(
            "[ccs] HubSpot %s has no property named %s — check the configured "
            "vendor field list",
            _OBJECT_TYPE,
            ", ".join(sorted(unknown)),
        )
    return out


def _option_map(client: HubSpotAPIClient | None = None) -> dict[str, list[str]]:
    """The cached ``{property: [values]}`` map, refreshed on miss or expiry.

    On a refresh failure with a cached copy in hand, the **stale** copy is
    served with a warning regardless of age: an option list from an hour ago is
    far better than the unconstrained free-for-all that an empty map would mean.
    Only a cold cache propagates the error.
    """
    workspace = current_workspace()
    token = workspace_secret("hubspot", "read_token")
    if not isinstance(token, str) or not token.strip():
        raise HubSpotAPIError("HubSpot read credential unavailable")
    token = token.strip()
    if client is not None and getattr(client, "token", None) != token:
        raise HubSpotAPIError("HubSpot client credential does not match workspace")
    token_hash = sha256(token.encode()).hexdigest()
    entry = _SCHEMA_CACHE.get(workspace)
    if entry is not None and entry[2] != token_hash:
        _SCHEMA_CACHE.pop(workspace, None)
        entry = None
    cached, ts = entry[:2] if entry is not None else (None, None)
    now = datetime.now(timezone.utc)
    if cached is not None and ts is not None:
        if (now - ts).total_seconds() <= _SCHEMA_CACHE_TTL_S:
            return cached

    try:
        fetched = _fetch_option_map(client or HubSpotAPIClient(token=token))
    except HubSpotAPIError as exc:
        if cached is not None:
            logger.warning(
                "[ccs] HubSpot property refresh failed (%s); serving the cached "
                "option map from %s",
                exc,
                ts.isoformat() if ts else "unknown",
            )
            return cached
        raise
    _SCHEMA_CACHE[workspace] = (fetched, now, token_hash)
    return fetched


def _fetch_option_map(client: HubSpotAPIClient | None = None) -> dict[str, list[str]]:
    """One GET for every property definition on the object.

    `/crm/v3/properties/{object}` returns the whole set in one page — cheaper
    and more predictable than one request per property, which at a dozen vendor
    fields is a dozen calls against a 190-per-10-second budget.

    Wrapped in `_with_backoff` because `HubSpotAPIClient` has no rate-limit
    handling at all: it raises `HubSpotAPIError` on any status including 429, and
    nothing in this repo reads `Retry-After`.
    """
    api = client or HubSpotAPIClient()
    # The read token, and its private header builder — the same shortcut
    # `HubSpotWriteClient.get_object` takes for its gate reads. Reading the
    # schema is a read; it must never touch the write token.
    url = f"{api.base_url}/crm/v3/properties/{_OBJECT_TYPE}"
    results = _with_backoff(_get_results, url, api._headers())

    out: dict[str, list[str]] = {}
    for prop in results:
        name = prop.get("name")
        if not name:
            continue
        values: list[str] = []
        for opt in prop.get("options") or []:
            # `hidden` marks an option the business retired from the UI, and
            # `archived` an option removed outright. Offering either to the
            # extractor invites it to resurface a value someone deliberately
            # stopped using.
            if opt.get("hidden") or opt.get("archived"):
                continue
            value = opt.get("value")
            if value is None:
                continue
            values.append(str(value))
        out[str(name)] = values
    return out


def _get_results(url: str, headers: dict[str, str]) -> list[dict]:
    """GET one HubSpot collection, raising `HubSpotAPIError` on any failure.

    The message keeps the literal ``HTTP {status}`` shape the rest of the repo
    uses, because `_with_backoff` decides what is transient by looking for
    ``429`` and `` 5`` **in the string**.
    """
    try:
        with httpx.Client(timeout=20.0) as http:
            resp = http.get(url, headers=headers, params={"archived": "false"})
            resp.raise_for_status()
            return resp.json().get("results", []) or []
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else 0
        body_txt = e.response.text[:300] if e.response is not None else ""
        raise HubSpotAPIError(
            f"HubSpot property fetch failed: HTTP {status}: {body_txt}"
        ) from e
    except httpx.HTTPError as e:
        raise HubSpotAPIError(f"HubSpot transport error: {e}") from e
