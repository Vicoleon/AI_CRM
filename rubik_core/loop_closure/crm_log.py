""" "Log it in HubSpot" — the AE's own sent email, logged as a HubSpot email.

Decision: Loose Ends never writes a HubSpot NOTE and never writes model text.
Call notes come from Fathom, which writes better ones (that is why CRM Sync has
no note path either). What an AE forgot to log
is the EMAIL, so the button logs exactly that: each email the AE sent in the
thread, with its full body, as a HubSpot email engagement on the contact,
company and deal the morning scan matched — the same record HubSpot's own Gmail
extension writes, and the one that moves the contact's "last contacted" date.

Two steps, both in a request the AE triggered (jobs never write HubSpot,
README §2):

* :func:`loggable_emails` reads the thread from the AE's OWN mailbox
  (``format="full"``) and keeps the messages the AE sent to the client since
  the item opened, each with its body decoded from the MIME tree and a
  duplicate probe (:meth:`HubSpotWriteClient.logged_email_candidates`, matched
  one to one across the thread) saying whether HubSpot already has it. The
  page previews exactly these (:func:`log_preview`) with a box per email,
  ticked when the email went to the item's own contact (``to_contact``), so
  a forward to someone else is logged only if the AE ticks it.
* :func:`log_emails` RE-READS the thread server-side — content sent by the
  browser is never trusted — refuses any message id that is not one of those
  emails, probes again, and writes each remaining one with
  ``create_email``. Attachments are never logged. A failure after some
  emails were written raises :class:`PartialLogError` carrying their ids,
  which the router stores on the item (``detail[CRM_LOGGED_DETAIL_KEY]``) so a quick
  retry does not depend on HubSpot's search having caught up.

Nothing here logs a subject, a body or an address: failures name the error
type only (``safe_error``).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr
from typing import Any

from rubik_core.brand import PRODUCT_NAME
from rubik_core.identifiers import CRM_LOGGED_DETAIL_KEY
from rubik_core.logsafe import safe_error
from rubik_core.retrieval.hubspot_write import (
    MODE_ALREADY_EXISTS,
    HubSpotPipelineRefused,
    pipeline_writable,
)

logger = logging.getLogger(__name__)

#: HubSpot caps a text property value at 65,536 characters; this stays under
#: it. The text is cut here (the preview says "truncated"); an HTML body over
#: it is left out rather than cut mid-tag, and the email is logged as its
#: plain text (the preview says "plain text only").
MAX_BODY_CHARS = 65_000
#: How much of each email the preview shows before the click.
EXCERPT_CHARS = 600
#: At most this many emails per click (the router enforces it on the request).
MAX_EMAILS_PER_LOG = 20
#: A message sent this long before ``opened_at`` still counts: ``opened_at``
#: is the scan's copy of Gmail's timestamp, rounded to the second.
_OPENED_SLACK = timedelta(minutes=1)
#: Never-sent or thrown-away mail. A DRAFT is From the AE too.
_SKIPPED_LABELS = frozenset({"DRAFT", "TRASH", "SPAM"})
# ``item.detail[CRM_LOGGED_DETAIL_KEY]`` holds ``{gmail message id: HubSpot
# email id}`` for emails already logged while the item stayed open (a failure
# partway, or an earlier batch of a long thread). Ids only. The key is a stored
# value, so it lives in ``rubik_core.identifiers``.

_NO_CONTACT = (
    f"{PRODUCT_NAME} does not know which HubSpot contact this email went to, so "
    "it cannot log it. Add the contact in HubSpot first; the next morning's "
    "check will pick it up."
)
_GMAIL_UNREADABLE = (
    f"{PRODUCT_NAME} could not read that email thread from your mailbox, so "
    "nothing was logged. Try again in a minute."
)
_HUBSPOT_UNREADABLE = (
    f"{PRODUCT_NAME} could not check HubSpot for these emails just now, so "
    "nothing was logged. Try again in a minute."
)


class LogError(RuntimeError):
    """Gmail or HubSpot could not be READ. ``str(exc)`` is safe to show the AE.

    Distinct from ``ValueError`` (the item or the request is not loggable) and
    from ``HubSpotWriteError`` (the write itself failed)."""


class PartialLogError(RuntimeError):
    """A write failed after earlier emails of the same click were created.

    ``created`` is ``[{"message_id", "hubspot_id"}]`` for the emails already in
    HubSpot; ``cause`` is the write error (for ``friendly_write_error``). The
    caller stores ``created`` on the item before answering, because HubSpot's
    search takes a while to show a new email and a quick retry's probe would
    otherwise miss it and log it twice. ``str(exc)`` names counts only.
    """

    def __init__(self, created: list[dict[str, str]], cause: Exception):
        super().__init__(f"{len(created)} email(s) logged before a write failed")
        self.created = created
        self.cause = cause


@dataclass
class LoggableEmail:
    """One email the AE sent in the thread, ready to be logged verbatim."""

    message_id: str
    subject: str
    #: Aware UTC (Gmail's ``internalDate``).
    sent_at: datetime
    from_email: str
    from_name: str
    to: list[str]
    cc: list[str]
    #: The full plain-text body (from the HTML when there is no text part),
    #: capped at ``MAX_BODY_CHARS``.
    text: str
    html: str | None
    already_logged: bool = False
    #: The HubSpot email engagement already holding it, when there is one.
    hubspot_id: str | None = None
    #: True when the text was cut at ``MAX_BODY_CHARS``.
    truncated: bool = False
    #: True when the HTML was over ``MAX_BODY_CHARS`` and is left out: the
    #: email is logged as its plain text.
    plain_text_only: bool = False
    #: True when To or Cc holds the item's own contact (``counterparty_email``)
    #: — the page ticks only these by default.
    to_contact: bool = False


# ── MIME ─────────────────────────────────────────────────────────────────────


def _headers(part: dict) -> dict[str, str]:
    return {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in (part.get("headers") or [])
    }


def _charset(part: dict) -> str:
    m = re.search(
        r"charset\s*=\s*\"?([\w.:-]+)", _headers(part).get("content-type", ""), re.I
    )
    return m.group(1) if m else "utf-8"


def _decode(part: dict) -> str:
    """A part's ``body.data`` (base64url, padding optional) as text."""
    data = (part.get("body") or {}).get("data") or ""
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return ""
    try:
        return raw.decode(_charset(part), "replace")
    except LookupError:  # a charset Python does not know
        return raw.decode("utf-8", "replace")


def _is_attachment(part: dict) -> bool:
    """A file riding in the message, never a body — even a ``.txt`` one."""
    if part.get("filename"):
        return True
    if (part.get("body") or {}).get("attachmentId"):
        return True
    disposition = _headers(part).get("content-disposition", "")
    return disposition.strip().lower().startswith("attachment")


def _walk(part: dict, found: dict[str, str]) -> None:
    if _is_attachment(part):
        return
    mime = (part.get("mimeType") or "").lower()
    if mime in ("text/plain", "text/html") and mime not in found:
        text = _decode(part)
        if text.strip():
            found[mime] = text
    for child in part.get("parts") or []:
        _walk(child, found)


_STRIP_BLOCKS = re.compile(r"(?is)<(style|script|head)\b[^>]*>.*?</\1\s*>")


def html_to_text(markup: str) -> str:
    """HTML → plain text, for an email that has only an HTML part."""
    from rubik_core.tools.gmail import _html_to_text

    return _html_to_text(_STRIP_BLOCKS.sub("", markup or ""))


def message_bodies(payload: dict) -> tuple[str, str | None]:
    """``(text, html)`` of a ``format="full"`` message payload.

    Walks the MIME tree depth-first and takes the first ``text/plain`` and the
    first ``text/html`` part that are not attachments. The text prefers the
    plain part; an HTML-only email gets its text derived from the HTML.
    """
    found: dict[str, str] = {}
    _walk(payload or {}, found)
    html = found.get("text/html")
    text = found.get("text/plain")
    if text is None:
        text = html_to_text(html) if html else ""
    return text.replace("\r\n", "\n"), html


# ── reading the thread ───────────────────────────────────────────────────────


def _sent_at(message: dict, headers: dict[str, str]) -> datetime | None:
    raw = message.get("internalDate")
    if raw:
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(headers.get("date", ""))
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _addresses(value: str) -> list[str]:
    return list(
        dict.fromkeys(
            a.strip().lower() for _, a in getaddresses([value or ""]) if a and "@" in a
        )
    )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _require_loggable(item: Any, owner_email: str) -> str:
    """The owner, folded — or ``ValueError`` with a sentence for the AE."""
    owner = (owner_email or "").strip().lower()
    if (item.owner_email or "").strip().lower() != owner:
        raise ValueError("This loose end belongs to someone else.")
    if item.kind != "unlogged" or item.source != "gmail":
        raise ValueError(
            "Only an email that is not in HubSpot can be logged from here. For "
            "anything else, open the record in HubSpot."
        )
    if item.status != "open":
        raise ValueError(
            "This loose end is already closed, so there is nothing to log."
        )
    if not item.thread_id:
        raise ValueError(
            f"{PRODUCT_NAME} does not know which email thread this is, so it cannot "
            "log it. Open it in Gmail or HubSpot instead."
        )
    if not (item.hubspot_contact_id or "").strip():
        raise ValueError(_NO_CONTACT)
    return owner


def _counterparty(item: Any) -> str:
    return (getattr(item, "counterparty_email", None) or "").strip().lower()


def _is_client_recipient(address: str, item: Any, internal: tuple[str, ...]) -> bool:
    """The item's contact, or someone who could be another client contact.

    The counterparty always counts (even on a personal or an internal-looking
    address: the scan matched it to a HubSpot contact). Anyone else counts
    only on a business domain outside the company and not a robot: a forward to a
    personal mailbox (``@gmail.com``, the AE's own included) never makes an
    email loggable. A forward to a business address still can, and is then
    shown UNticked (``to_contact`` False) for the AE to decide.
    """
    from rubik_core.loop_closure.constants import PUBLIC_EMAIL_DOMAINS
    from rubik_core.loop_closure.detect import domain_of, is_automated, is_internal

    counterparty = _counterparty(item)
    if counterparty and address == counterparty:
        return True
    if is_internal(address, internal) or is_automated(address):
        return False
    return domain_of(address) not in PUBLIC_EMAIL_DOMAINS


def emails_in_thread(
    thread: dict, item: Any, *, owner_email: str
) -> list[LoggableEmail]:
    """The emails in a ``format="full"`` thread that this item may log.

    Kept: From the owner, labelled SENT and not DRAFT / TRASH / SPAM, sent at
    or after ``item.opened_at`` (less a minute), and addressed (To or Cc) to
    the item's counterparty or to another possible client contact — a
    business address outside this workspace's verified domains, not a robot,
    not a personal mailbox (:func:`_is_client_recipient`). ``to_contact``
    says which went to the counterparty itself. Oldest first. Pure: no I/O,
    no probe.
    """
    from rubik_core.loop_closure.constants import internal_domains

    owner = (owner_email or "").strip().lower()
    internal = internal_domains()
    floor = (
        _aware(item.opened_at) - _OPENED_SLACK if item.opened_at is not None else None
    )
    out: list[LoggableEmail] = []
    for message in (thread or {}).get("messages") or []:
        labels = set(message.get("labelIds") or ())
        if "SENT" not in labels or labels & _SKIPPED_LABELS:
            continue
        payload = message.get("payload") or {}
        headers = _headers(payload)
        from_name, from_addr = parseaddr(headers.get("from", ""))
        if (from_addr or "").strip().lower() != owner:
            continue
        sent_at = _sent_at(message, headers)
        if sent_at is None or (floor is not None and sent_at < floor):
            continue
        to = _addresses(headers.get("to", ""))
        cc = _addresses(headers.get("cc", ""))
        if not any(_is_client_recipient(a, item, internal) for a in (*to, *cc)):
            continue
        message_id = str(message.get("id") or "")
        if not message_id:
            continue
        text, html = message_bodies(payload)
        # Never slice markup (a cut mid-tag breaks the record): an HTML body
        # too long for HubSpot is left out and the text carries the email.
        html_too_long = bool(html) and len(html or "") > MAX_BODY_CHARS
        counterparty = _counterparty(item)
        out.append(
            LoggableEmail(
                message_id=message_id,
                subject=(headers.get("subject") or "").strip(),
                sent_at=sent_at,
                from_email=owner,
                from_name=(from_name or "").strip().strip('"'),
                to=to,
                cc=cc,
                text=text[:MAX_BODY_CHARS],
                html=None if html_too_long or not html else html,
                truncated=len(text) > MAX_BODY_CHARS,
                plain_text_only=html_too_long,
                to_contact=bool(counterparty) and counterparty in (*to, *cc),
            )
        )
    out.sort(key=lambda e: e.sent_at)
    return out


def _read_thread(item: Any, owner: str, gmail: Any) -> dict:
    try:
        if gmail is None:
            from rubik_core.tools.gmail import GmailClient

            gmail = GmailClient(owner)
        return gmail.get_thread(item.thread_id) or {}
    except Exception as exc:  # noqa: BLE001 — a sentence, never a traceback
        logger.warning("[loop-closure] log: thread read failed: %s", safe_error(exc))
        raise LogError(_GMAIL_UNREADABLE) from None


def _write_client(client: Any) -> Any:
    if client is not None:
        return client
    from rubik_core.retrieval.hubspot_write import HubSpotWriteClient

    return HubSpotWriteClient()


def logged_by_cubie(item: Any) -> dict[str, str]:
    """``{gmail message id: HubSpot email id}`` Cubie already wrote for this
    still-open item (``detail[CRM_LOGGED_DETAIL_KEY]``): a click that failed partway,
    or an earlier batch of a long thread."""
    detail = getattr(item, "detail", None)
    stored = detail.get(CRM_LOGGED_DETAIL_KEY) if isinstance(detail, dict) else None
    if not isinstance(stored, dict):
        return {}
    return {str(m): str(h) for m, h in stored.items() if m and h}


def _assign(
    emails: list[LoggableEmail], candidates: dict[str, list[tuple[str, int]]]
) -> None:
    """Match sent emails to HubSpot emails one to one, closest first.

    Two "Re: Proposal" sent three minutes apart both fall in each other's
    window; probing each alone would let the one HubSpot email stand for
    both, and the second would never be logged. Every (sent, HubSpot) pair
    is ranked by time apart and taken greedily, each side at most once.
    """
    by_id = {e.message_id: e for e in emails}
    claimed = {e.hubspot_id for e in emails if e.already_logged and e.hubspot_id}
    pairs = sorted(
        (distance, index, message_id, hubspot_id)
        for index, (message_id, hits) in enumerate(candidates.items())
        for hubspot_id, distance in hits
    )
    for _distance, _index, message_id, hubspot_id in pairs:
        email = by_id[message_id]
        if email.already_logged or hubspot_id in claimed:
            continue
        email.already_logged = True
        email.hubspot_id = hubspot_id
        claimed.add(hubspot_id)


def loggable_emails(
    item: Any,
    *,
    owner_email: str,
    gmail: Any = None,
    read_client: Any = None,
) -> list[LoggableEmail]:
    """The emails "Log it in HubSpot" would log for this item, probed.

    ``ValueError`` (a sentence) when the item cannot be logged at all: not the
    caller's, not an open unlogged Gmail item, no thread, or no HubSpot contact
    to attach to. :class:`LogError` when the mailbox or HubSpot cannot be read.
    ``read_client`` is anything with ``logged_email_candidates`` (a
    ``HubSpotWriteClient``, whose probe uses the read token and backs off on a
    429).

    An email Cubie already wrote for this item (:func:`logged_by_cubie`) is
    already logged without asking HubSpot, whose search can lag a fresh write
    by a minute. The rest are matched one to one (:func:`_assign`).
    """
    owner = _require_loggable(item, owner_email)
    thread = _read_thread(item, owner, gmail)
    emails = emails_in_thread(thread, item, owner_email=owner)
    if not emails:
        return emails
    recorded = logged_by_cubie(item)
    for email in emails:
        if email.message_id in recorded:
            email.already_logged = True
            email.hubspot_id = recorded[email.message_id]
    todo = [e for e in emails if not e.already_logged]
    if not todo:
        return emails
    client = _write_client(read_client)
    contact_ids = [str(item.hubspot_contact_id).strip()]
    candidates: dict[str, list[tuple[str, int]]] = {}
    try:
        for email in todo:
            candidates[email.message_id] = list(
                client.logged_email_candidates(
                    contact_ids=contact_ids,
                    subject=email.subject,
                    sent_at=email.sent_at,
                )
                or []
            )
    except Exception as exc:  # noqa: BLE001 — "could not check" is never "not there"
        logger.warning("[loop-closure] log: HubSpot probe failed: %s", safe_error(exc))
        raise LogError(_HUBSPOT_UNREADABLE) from None
    _assign(emails, candidates)
    return emails


# ── the preview ──────────────────────────────────────────────────────────────


def hubspot_url_for(item: Any) -> str | None:
    """Where the email will show: the contact, else the company, else the deal."""
    from rubik_core.sales.rhythm_checks import record_url

    return (
        record_url("contact", item.hubspot_contact_id)
        or record_url("company", item.hubspot_company_id)
        or record_url("deal", item.hubspot_deal_id)
    )


def _naive_iso(dt: datetime) -> str:
    return (
        _aware(dt)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def deal_label(item: Any, *, read_client: Any = None) -> str | None:
    """The deal's name when the email will also be attached to it.

    Read live: only a deal in one of the company's pipelines gets the
    association (the write gate refuses the rest), so the preview names it only
    then. Any failure is None — the deal is a bonus, never a reason to refuse
    the preview.
    """
    deal_id = (item.hubspot_deal_id or "").strip()
    if not deal_id:
        return None
    try:
        live = _write_client(read_client).get_object(
            "deals", deal_id, ["dealname", "pipeline"]
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("[loop-closure] log preview: deal read failed: %s", safe_error(exc))
        return None
    props = (live or {}).get("properties") or {}
    if not pipeline_writable(props.get("pipeline")):
        return None
    return (props.get("dealname") or "").strip() or "the deal"


def log_preview(
    item: Any,
    *,
    owner_email: str,
    gmail: Any = None,
    read_client: Any = None,
) -> dict:
    """The page's preview: each email as it will be logged, and where.

    ``excerpt`` is the first ``EXCERPT_CHARS`` of the text; ``chars`` is how
    much text will be logged; ``sent_at`` is naive UTC (``parseInstant`` on
    the page); ``to_contact`` is whether the page ticks it by default. Raises
    like :func:`loggable_emails`.
    """
    client = _write_client(read_client)
    emails = loggable_emails(
        item, owner_email=owner_email, gmail=gmail, read_client=client
    )
    detail = item.detail if isinstance(getattr(item, "detail", None), dict) else {}
    return {
        "emails": [
            {
                "message_id": e.message_id,
                "subject": e.subject,
                "to": list(e.to),
                "cc": list(e.cc),
                "sent_at": _naive_iso(e.sent_at),
                "excerpt": e.text[:EXCERPT_CHARS],
                "chars": len(e.text),
                "truncated": e.truncated,
                "plain_text_only": e.plain_text_only,
                "to_contact": e.to_contact,
                "already_logged": e.already_logged,
            }
            for e in emails
        ],
        "contact": (detail.get("crm_contact_name") or item.counterparty_label or None),
        "company": item.company_label or None,
        "deal": deal_label(item, read_client=client) if emails else None,
        "hubspot_url": hubspot_url_for(item),
    }


# ── the write ────────────────────────────────────────────────────────────────


def _name_parts(name: str) -> dict[str, str]:
    parts = (name or "").split()
    if not parts:
        return {}
    if len(parts) == 1:
        return {"firstName": parts[0]}
    return {"firstName": parts[0], "lastName": " ".join(parts[1:])}


def email_properties(email: LoggableEmail) -> dict[str, str]:
    """The ``EMAIL_WRITABLE`` properties for one sent email — its own words,
    verbatim. ``hubspot_owner_id`` is stamped by ``create_email``."""
    headers = {
        "from": {"email": email.from_email, **_name_parts(email.from_name)},
        "to": [{"email": a} for a in email.to],
        "cc": [{"email": a} for a in email.cc],
    }
    props = {
        "hs_timestamp": str(int(_aware(email.sent_at).timestamp() * 1000)),
        "hs_email_direction": "EMAIL",
        "hs_email_status": "SENT",
        "hs_email_subject": email.subject,
        "hs_email_text": email.text,
        "hs_email_headers": json.dumps(headers),
    }
    if email.html:
        props["hs_email_html"] = email.html
    return props


def log_emails(
    item: Any,
    *,
    owner_email: str,
    message_ids: list[str],
    gmail: Any = None,
    write_client: Any = None,
) -> dict:
    """Log the requested emails; return what happened to each.

    ``{"logged": [{"message_id", "hubspot_id", "mode"}], "hubspot_url": …}``
    with ``mode`` ``created`` or ``already_exists``.

    The thread is RE-READ here and every requested id must be one of
    :func:`loggable_emails` (``ValueError`` otherwise), so the browser decides
    WHICH of the AE's sent emails go to HubSpot and never WHAT they say. Each
    is probed (the probe in ``loggable_emails``) before it is written. A deal
    outside the company's pipelines, or one deleted since the scan, is dropped after
    its first refusal and the email is logged on the contact and company
    alone. Any other failure raises: the original error when nothing was
    created in this call, :class:`PartialLogError` (carrying what WAS
    created) when something was. The caller keeps the item open and stores
    those ids (``detail[CRM_LOGGED_DETAIL_KEY]``), so a retry skips them even before
    HubSpot's search shows them.
    """
    wanted = [str(m).strip() for m in message_ids or [] if str(m or "").strip()]
    if not wanted:
        raise ValueError("Pick at least one email to log.")
    client = _write_client(write_client)
    emails = loggable_emails(
        item, owner_email=owner_email, gmail=gmail, read_client=client
    )
    by_id = {e.message_id: e for e in emails}
    unknown = [m for m in wanted if m not in by_id]
    if unknown:
        raise ValueError(
            f"That email is not one {PRODUCT_NAME} can log from this loose end. "
            "Refresh the page and try again."
        )
    owner = (owner_email or "").strip().lower()
    contact_ids = [str(item.hubspot_contact_id).strip()]
    company_id = item.hubspot_company_id or None
    deal_id = item.hubspot_deal_id or None
    logged: list[dict[str, str]] = []
    for message_id in dict.fromkeys(wanted):
        email = by_id[message_id]
        if email.already_logged:
            logged.append(
                {
                    "message_id": message_id,
                    "hubspot_id": email.hubspot_id or "",
                    "mode": MODE_ALREADY_EXISTS,
                }
            )
            continue
        props = email_properties(email)
        try:
            try:
                created = client.create_email(
                    props,
                    owner_email=owner,
                    contact_ids=contact_ids,
                    company_id=company_id,
                    deal_id=deal_id,
                )
            except HubSpotPipelineRefused:
                if not deal_id:
                    raise
                # The deal is in a pipeline Cubie may not write to (the client
                # pipeline the portal also hosts), or it was deleted since the
                # scan (HubSpotDealGone). The email still belongs on the
                # contact and the company: log it there, once, and leave the
                # deal out for the rest of this click.
                deal_id = None
                created = client.create_email(
                    props,
                    owner_email=owner,
                    contact_ids=contact_ids,
                    company_id=company_id,
                    deal_id=None,
                )
        except Exception as exc:
            logger.warning(
                "[loop-closure] log: HubSpot email write failed after %d of %d: %s",
                len(logged),
                len(wanted),
                type(exc).__name__,
            )
            written = [
                {"message_id": r["message_id"], "hubspot_id": r["hubspot_id"]}
                for r in logged
                if r["mode"] != MODE_ALREADY_EXISTS and r["hubspot_id"]
            ]
            if written:
                raise PartialLogError(written, exc) from exc
            raise
        logged.append(
            {
                "message_id": message_id,
                "hubspot_id": str((created or {}).get("id") or ""),
                "mode": str((created or {}).get("_mode") or "created"),
            }
        )
    return {"logged": logged, "hubspot_url": hubspot_url_for(item) or ""}
