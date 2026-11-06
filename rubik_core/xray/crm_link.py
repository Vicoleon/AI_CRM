"""Point a HubSpot company at its Customer X-Ray page in Cubie (UC-45).

Sales lives in HubSpot, not in Cubie. Research that only exists behind a Cubie
URL nobody has is research nobody reads — so every Fast Research and Deep
Research run stamps the company record with a link back to its brief. A rep
opening the account in the CRM sees that Cubie has studied this customer and
gets there in one click.

**The link is stable, and that is the design.** It is
``{FRONTEND_URL}/customers/{hubspot_company_id}/xray``, which always renders the
newest ready snapshot for that company. Nothing about it encodes a version, so:

* it can never rot into a pointer at some stale March brief,
* re-running research changes nothing about the value, which makes the write
  idempotent and lets us skip the PATCH entirely when HubSpot already agrees,
* and — the load-bearing consequence — the value is fully known *before* a Deep
  run starts. That is what lets the write happen inside the request a person
  triggered, on the ``cubie`` Service, instead of at the end of the run
  inside the ``cubie-xray-deep`` Cloud Run Job. The write client refuses Cloud
  Run Jobs in code, even if the workspace has a write token. A versioned link
  would have moved this write into a background job to buy nothing a rep wants.

Everything here is best-effort: ``publish_research_link`` never raises and never
fails an X-Ray run. A brief the user can read is worth more than a CRM field,
and a HubSpot outage must not turn a good run into a failed one.

The property itself (``cubie_research_link``) is created by hand in HubSpot
Settings → Properties, not by this code — see ``docs/uc45_research_link.md``.
Until it exists HubSpot rejects the call — on the read or on the write, and we
handle both — and we log one clear line and skip. That makes rollout
order-independent: create the property whenever, and the next run starts
populating it with no deploy.
"""

from __future__ import annotations

import logging

from rubik_core.config import settings
from rubik_core.retrieval.hubspot_write import (
    RESEARCH_LINK_PROPERTY,
    HubSpotWriteClient,
    HubSpotWriteError,
)

logger = logging.getLogger(__name__)

#: Path on the SPA that renders a company's latest ready X-Ray
#: (``frontend/src/App.tsx`` → ``customers/:companyId/xray``).
_XRAY_PATH = "/customers/{company_id}/xray"


def enabled() -> bool:
    """Kill switch for the CRM writeback alone, independent of X-Ray itself.

    Turning this off stops stamping HubSpot without disabling research. The
    write token being unset is the other, harder gate — in an environment
    without it every write is refused in ``hubspot_write``, so this module is
    inert there whatever this flag says.
    """
    return bool(getattr(settings, "XRAY_CRM_LINK_ENABLED", True))


def research_link_url(company_id: str) -> str | None:
    """The Cubie X-Ray URL for a company, or ``None`` if we can't build a real one.

    Refuses anything that isn't a public http(s) origin. ``FRONTEND_URL``
    defaults to ``http://localhost:5173``, and a process that simply doesn't
    carry the real value would otherwise write dead localhost links into the
    CRM and report success — precisely the failure the ``cubie-worker`` job hit
    with emailed links before the platform deploy set its public origin. A missing
    link is recoverable; a CRM full of dead ones is not.
    """
    base = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return None
    host = base.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
        return None
    if not str(company_id).isdigit():
        return None
    return base + _XRAY_PATH.format(company_id=company_id)


def _property_missing(exc: Exception) -> bool:
    """True when HubSpot rejected the call because the property isn't defined.

    Matched on the response body, which both ``get_object`` and ``_request``
    carry. HubSpot phrases this several ways, so we look for the property name
    alongside a 400 rather than for one exact sentence.
    """
    text = str(exc)
    return "HTTP 400" in text and RESEARCH_LINK_PROPERTY in text


def _log_failure(stage: str, company_id: str, exc: Exception) -> None:
    """One actionable line when the property simply hasn't been created yet.

    Checked on BOTH the read and the write on purpose. Whether HubSpot rejects
    an unknown property name on the GET (400) or lets the read through and
    rejects it on the PATCH is an endpoint detail we should not have to be right
    about — the operator-facing message is the same either way, and getting it
    wrong would bury the one thing that needs doing under a generic error.
    """
    if _property_missing(exc):
        logger.warning(
            "xray crm link: HubSpot company property %r does not exist on the "
            "portal — create it (Settings → Properties → Company, single-line "
            "text; see docs/uc45_research_link.md) and the next run will "
            "populate it. No deploy needed.",
            RESEARCH_LINK_PROPERTY,
        )
    else:
        logger.warning(
            "xray crm link: could not %s company %s: %s", stage, company_id, exc
        )


def publish_research_link(company_id: str) -> str | None:
    """Stamp the company's X-Ray link onto HubSpot. Returns the URL, or ``None``.

    ``None`` means "nothing was written", for any reason — disabled, no usable
    ``FRONTEND_URL``, property not created yet, HubSpot down, write token unset.
    Callers treat that as normal; it is never an error worth surfacing to the
    person who asked for research.

    Reads before it writes, and skips the PATCH when HubSpot already holds the
    exact URL. This fires on *every* run, and the value only ever changes for a
    company's first-ever brief — without the read this would spend a write per
    run against the 190-per-10s budget to store a string that was already there.
    """
    if not enabled():
        return None

    url = research_link_url(company_id)
    if url is None:
        logger.debug(
            "xray crm link: no usable URL for company %s (FRONTEND_URL=%r)",
            company_id,
            getattr(settings, "FRONTEND_URL", ""),
        )
        return None

    try:
        client = HubSpotWriteClient()
        current = client.get_object(
            "companies", str(company_id), [RESEARCH_LINK_PROPERTY]
        )
    except HubSpotWriteError as exc:
        _log_failure("read", company_id, exc)
        return None
    except Exception:  # noqa: BLE001 - the CRM stamp never fails a research run
        logger.exception("xray crm link: unexpected read failure for %s", company_id)
        return None

    existing = (current.get("properties") or {}).get(RESEARCH_LINK_PROPERTY) or ""
    if existing.strip() == url:
        logger.debug("xray crm link: company %s already linked", company_id)
        return url

    try:
        client.update_company_research_link(str(company_id), url)
    except Exception as exc:  # noqa: BLE001 - see above
        _log_failure("write", company_id, exc)
        return None

    logger.info("xray crm link: company %s → %s", company_id, url)
    return url
