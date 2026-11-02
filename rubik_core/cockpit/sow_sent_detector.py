"""Sent-mail evidence that an SOW went out — as a SUGGESTION, never a close.

**Before anything else: nothing guarantees that a copy of the SOW passes
through the AE's mailbox at all.** If SOWs are delivered *by* PandaDoc,
the document is sent from PandaDoc's servers and the AE's Sent folder never sees
it — in which case this module finds nothing, forever, and there is no code
change that fixes it. The sales supervisor knows how the company sends SOWs,
and it is a five-minute question.

Read the rest of this module on that footing. The commitment clock is unaffected
either way: commitments still open from the call, still age, still
escalate, and a person can still close one in a click. What is at stake here is
only the *automatic* close-side evidence, which is a convenience on top of a
mechanism that works without it. Nothing in this module may be described as a
working automation until that question is answered.

**Why it suggests instead of closing.** A wrong auto-close HIDES A REAL BREACH,
which is the precise failure the cockpit exists to fix — the client-requested
SOW that sits for three weeks with nobody watching. So the output is a row the cockpit
renders as:

    Looks like the SOW went out on 22 Aug — close this?   [ Yes ]   [ No ]

One click confirms, and the click records ``closed_source="manual"``, because a
human confirming evidence *is* a manual close. ``commitment_sla_configs``'s
sibling constant ``COMMITMENT_CLOSED_SOURCES`` has only ``manual`` and
``superseded`` for exactly this reason: there is no "email" close source in the
schema, so no future code path can auto-close from a heuristic without a
migration, in public, with a reason.

**Mail-content discipline.** This module never reads or stores a message body.
The PandaDoc-link lane puts the term in the *Gmail query*, so the body match
happens on Google's side and the body never reaches Cubie at all. What is
returned is the message id and structured match reasons — never the subject
text, never a snippet, never a recipient address. The one piece of
message-derived text that does appear is an attachment **filename**, marked
where it happens: it is header-level metadata, it is the single thing that lets
a person judge "is that the SOW?", and a suggestion nobody can evaluate is worse
than no suggestion.

**Zero suggestions is not one outcome, it is five.** "Nobody sent an SOW", "this
AE holds nothing a suggestion could close", "the Gmail grant is revoked so we
never looked", "every search lane raised" and "one lane raised so we looked at
half the mailbox" all used to return the same empty list. The first two are
product outcomes; the last three are outages that make the aging numbers look
*better* than reality, because the commitments a working detector would have
offered to close keep aging toward a breach nobody is told about. So the real
entry point is ``scan_sent_mail_detailed`` → :class:`ScanOutcome`, carrying a
``status`` and the per-lane counts; the list-shaped ``scan_sent_mail`` remains
for callers that only want suggestions and RAISES :class:`MailboxUnavailable` /
:class:`AllLanesFailed` rather than handing back a silent empty list.

None of that says the detector works. A ``status="ok"`` with zero suggestions is
still ambiguous in the one way described above — a genuinely quiet week and a
world where PandaDoc sends every SOW look identical from here, and only the
sales supervisor can tell them apart. What the statuses buy is that *Cubie's own*
failures stop
hiding inside that ambiguity.

Nothing here is persisted. ``scan_sent_mail`` is a pure read + score; the caller
renders the suggestions and the confirming click is what writes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.database.commitments import Commitment
from rubik_core.retrieval.hubspot_write import HubSpotWriteClient, _with_backoff
from rubik_core.tenancy.workspace_time import workspace_time_zone
from rubik_core.tools.gmail import GmailClient

logger = logging.getLogger(__name__)


# ── Outcomes: why the suggestion list is empty ───────────────────────────────

#: The mailbox was read and every lane answered. Zero suggestions here is a
#: product outcome — subject to the mailbox question above, which no status
#: can settle.
SCAN_OK = "ok"
#: This AE holds no live SOW commitment, so no mailbox was read. Reading
#: somebody's Sent folder needs a reason that exists BEFORE the read.
SCAN_NO_COMMITMENTS = "no_commitments"
#: ``GmailClient`` handed back a ``None`` service: the DWD grant was revoked, the
#: Workspace key was rotated without a redeploy, or the address is not a
#: Workspace user. We did not look. This is the bucket that must never be summed
#: with "nobody sent anything".
SCAN_MAILBOX_UNAVAILABLE = "mailbox_unavailable"
#: The mailbox opened but EVERY search lane raised. Same consequence as an
#: unavailable mailbox — no evidence was seen — from a different cause.
SCAN_LANES_FAILED = "lanes_failed"
#: Some lanes answered and some raised: the search was silently narrowed, so a
#: suggestion may be missing. Suggestions found are still real; absences are not
#: trustworthy.
SCAN_LANES_DEGRADED = "lanes_degraded"

#: Every value ``ScanOutcome.status`` can take. Callers zero-fill their report
#: buckets from this so a bucket that did not happen still renders as 0 — a dict
#: holding only what happened cannot tell "nothing failed" from "we never ran".
SCAN_STATUSES: tuple[str, ...] = (
    SCAN_OK,
    SCAN_NO_COMMITMENTS,
    SCAN_MAILBOX_UNAVAILABLE,
    SCAN_LANES_FAILED,
    SCAN_LANES_DEGRADED,
)

#: The statuses where a mailbox was actually searched and the empty case can be
#: read as "quiet". ``SCAN_LANES_DEGRADED`` is in here on purpose: a partial
#: search is still a search, and its counters say how much of one.
SCAN_SEARCHED_STATUSES: frozenset[str] = frozenset({SCAN_OK, SCAN_LANES_DEGRADED})


class SowSentScanError(RuntimeError):
    """Base for the failures that used to look like an empty mailbox."""


class MailboxUnavailable(SowSentScanError):
    """Gmail handed back no service for this AE — nothing was searched."""


class AllLanesFailed(SowSentScanError):
    """Every Gmail search lane raised — nothing was searched."""


@dataclass(frozen=True)
class ScanOutcome:
    """One AE's scan: what was found, and whether the finding means anything.

    ``suggestions`` alone is not a result. ``status`` is what a report branches
    on, and ``lanes_searched``/``lanes_failed`` are what make a partial
    degradation visible instead of quietly narrowing the search.
    """

    status: str
    suggestions: list[dict] = field(default_factory=list)
    #: Live SOW commitments this AE holds — the reason the mailbox was read.
    live_commitments: int = 0
    #: Lanes that answered, and lanes that raised. ``lanes_searched`` is 0 for
    #: every status where the mailbox was never opened.
    lanes_searched: int = 0
    lanes_failed: int = 0
    #: Sent messages from this AE that reached the scorer.
    messages_scored: int = 0

    @property
    def searched(self) -> bool:
        """True when a mailbox was actually searched, whole or in part."""
        return self.status in SCAN_SEARCHED_STATUSES

    @property
    def degraded(self) -> bool:
        """True when at least one lane raised — absences are not trustworthy."""
        return bool(self.lanes_failed)


#: Below this, the match is not shown at all. A module constant rather than a
#: setting on purpose: a confidence floor that can be configured to zero is a
#: floor somebody will eventually configure to zero, and the whole point of this
#: module is that a weak match must not reach a person as though it were strong.
#: Raise it here, in a reviewed change, with the "Not right" rate as evidence.
MIN_SUGGESTION_CONFIDENCE = 0.6

# Score weights. Every candidate already arrived through a lane, so the minimum
# achievable score is a bare attachment (0.10) — deliberately far below the
# floor, because AEs email attachments to clients all day and "a file went to
# this domain" is not evidence about an SOW.
_W_CONTACT_MATCH = 0.45  # went to a contact HubSpot associates with the deal
_W_DOMAIN_MATCH = 0.30  # went to the company's domain, but not a known contact
_W_SUBJECT_SOW = 0.25
_W_ATTACHMENT_SOW_FILENAME = 0.20
_W_ATTACHMENT = 0.10
_W_PANDADOC_LINK = 0.15

# Default window when the caller passes no ``since``. Wide enough to catch an
# SOW sent before the commitment was noticed; the per-commitment ``opened_at``
# gate below is what keeps the window honest.
_DEFAULT_LOOKBACK_DAYS = 30

# Per-lane thread cap. Gmail's threads.list is one API call but each thread is
# then hydrated individually (see GmailClient.search_threads), so this is the
# real cost knob for a sweep that runs hourly across every AE.
_MAX_THREADS_PER_LANE = 25

# Terms handed to Gmail's own body search for the PandaDoc lane. Kept as a query
# rather than a body scan so no message body is ever transferred to Cubie.
_PANDADOC_QUERY_TERMS = ("pandadoc", "app.pandadoc.com")

_SOW_SUBJECT_TOKENS = (
    "sow",
    "scope of work",
    "statement of work",
    "engagement letter",
    "proposal",
    "agreement",
)
_SOW_FILENAME_TOKENS = ("sow", "scope", "engagement", "proposal", "agreement")

# Copied, not imported, from rubik_core/priorities/agents/gmail_agent.py:33 —
# the contract's reuse list points at that module as the multi-lane + dedupe
# PATTERN to copy. Importing three privates out of a priorities channel agent
# into the cockpit would couple two features that share nothing but Gmail.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _norm(email: str | None) -> str:
    return (email or "").strip().lower()


def _addresses(header_value: str) -> list[str]:
    """Every bare address in a ``Name <a@b.com>, c@d.com`` header value."""
    return [m.group(0).lower() for m in _EMAIL_RE.finditer(header_value or "")]


def _header(msg: dict, name: str) -> str:
    """One header value off a Gmail message dict, case-insensitively."""
    target = name.lower()
    for header in msg.get("payload", {}).get("headers", []) or []:
        if (header.get("name") or "").lower() == target:
            return header.get("value") or ""
    return ""


def _sent_at(msg: dict) -> datetime | None:
    """``internalDate`` (epoch ms) as a NAIVE UTC datetime.

    Naive UTC because every ``DateTime`` column in this repo is naive and mixing
    an aware value into a comparison against ``Commitment.opened_at`` raises at
    runtime rather than at review time.
    """
    raw = msg.get("internalDate")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).replace(
            tzinfo=None
        )
    except (TypeError, ValueError, OSError):
        return None


def _attachment_filenames(msg: dict) -> list[str]:
    """Filenames on this message's top-level parts.

    Top-level only, matching ``gmail_agent``'s attachment probe — a nested
    multipart hides its filenames one level down, so this under-detects rather
    than over-detects. Under-detection costs recall on a suggestion; a body walk
    to fix it would cost the content boundary this module is built on.
    """
    return [
        str(part.get("filename"))
        for part in (msg.get("payload", {}).get("parts") or [])
        if part.get("filename")
    ]


def _gmail_after(since: datetime) -> str:
    """Gmail's ``after:`` operator, which takes YYYY/MM/DD and no time.

    Local reimplementation of ``backend/services/gmail_service._to_gmail_after``:
    ``rubik_core/`` must not import from ``backend/`` (docs/ENGINEERING_NOTES.md's layering
    rule), and the cockpit's sweep runs from the worker path where nothing under
    ``backend/services/`` is cleanly importable anyway.

    Note the consequence — the effective window is rounded DOWN to midnight UTC
    on the start day, so the query slightly over-fetches. That direction is the
    safe one: an extra candidate is scored and probably discarded, a missing one
    is a breach nobody was offered a close for.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return since.astimezone(timezone.utc).strftime("%Y/%m/%d")


def _message_key(msg: dict) -> str:
    """Stable per-message identity for the cross-lane union dedupe.

    Real Gmail messages always carry a globally-unique ``id``, identical no
    matter which lane surfaced the thread. The composite fallback exists only
    for hydration shapes and fixtures that omit it.
    """
    mid = msg.get("id")
    if mid:
        return str(mid)
    return "|".join(
        (
            str(msg.get("internalDate", "")),
            _header(msg, "From"),
            _header(msg, "Subject"),
        )
    )


# ── HubSpot context, fetched once per AE per run ──────────────────────────────


def _company_domains(
    company_ids: set[str], *, hs: HubSpotWriteClient
) -> dict[str, str]:
    """{company_id: domain}, best-effort.

    One read per distinct company, once per run and passed down — never per
    message. Account-wide and per-record lookups made inside a loop are what
    burn HubSpot's 5 req/s search budget (``rubik_core/crm_sync/service.py``
    records the same rule).
    """
    out: dict[str, str] = {}
    for company_id in sorted(company_ids):
        try:
            record = _with_backoff(
                hs.get_object, "companies", str(company_id), ["domain", "name"]
            )
        except Exception as exc:  # noqa: BLE001 — one company, not the sweep
            logger.warning(
                "[sow-sent] company %s: HubSpot read failed: %s", company_id, exc
            )
            continue
        domain = _norm((record.get("properties") or {}).get("domain"))
        if domain:
            out[str(company_id)] = domain
    return out


def _deal_contact_emails(
    deal_ids: set[str], *, hs: HubSpotWriteClient
) -> dict[str, set[str]]:
    """{deal_id: {contact emails}}, best-effort.

    Uses the ``associations.deal`` search pseudo-property, the same mechanism
    ``rubik_core/crm_sync/hubspot_context.py`` uses with ``associations.company``
    — one call instead of walking the v4 association endpoint, and it needs none
    of the association *type ids*, which are still UNVERIFIED against this
    portal (contract unknown #4). A failure here costs the strongest scoring
    signal and falls back to the company domain, which can only lower a score.
    It can never invent a match.
    """
    out: dict[str, set[str]] = {}
    for deal_id in sorted(deal_ids):
        try:
            contacts = _with_backoff(
                hs.read_client.search_all,
                "contacts",
                [
                    {
                        "propertyName": "associations.deal",
                        "operator": "EQ",
                        "value": str(deal_id),
                    }
                ],
                ["email"],
            )
        except Exception as exc:  # noqa: BLE001 — one deal, not the sweep
            logger.warning(
                "[sow-sent] deal %s: contact lookup failed: %s", deal_id, exc
            )
            continue
        emails = {
            _norm((c.get("properties") or {}).get("email")) for c in (contacts or [])
        }
        emails.discard("")
        if emails:
            out[str(deal_id)] = emails
    return out


# ── Scoring ──────────────────────────────────────────────────────────────────


def _score(
    *,
    commitment: Commitment,
    sent_at: datetime | None,
    external_recipients: list[str],
    subject: str,
    filenames: list[str],
    from_pandadoc_lane: bool,
    contact_emails: set[str],
    company_domain: str,
) -> tuple[float, list[str]]:
    """Score one sent message against one live commitment.

    Returns ``(0.0, [])`` when a hard gate fails. The gates matter more than the
    weights: the one unacceptable outcome is suggesting a close on the WRONG
    commitment, because a person clicking Yes on that suggestion closes a
    commitment whose SOW never went anywhere.
    """
    # Gate 1 — it has to have left the building. An internal-only email is not
    # "the SOW went out to the client", however SOW-shaped it looks.
    if not external_recipients:
        return 0.0, []

    # Gate 2 — it cannot predate the ask. A document sent before the client
    # requested this SOW is a different document.
    if commitment.opened_at is not None and sent_at is not None:
        if sent_at < commitment.opened_at:
            return 0.0, []

    # Gate 3 — it has to tie to THIS commitment. Without a contact or domain
    # tie there is no reason to think this message belongs to this obligation,
    # and a score built on subject wording alone would happily close the wrong
    # deal for a client with two open SOWs.
    reasons: list[str] = []
    score = 0.0
    if contact_emails and any(addr in contact_emails for addr in external_recipients):
        score += _W_CONTACT_MATCH
        # The address itself is deliberately NOT recorded: the deal identifies
        # the suggestion, so naming a client contact adds nothing a person needs.
        reasons.append("contact_match")
    elif company_domain and any(
        addr.rsplit("@", 1)[-1] == company_domain for addr in external_recipients
    ):
        score += _W_DOMAIN_MATCH
        reasons.append(f"domain_match:{company_domain}")
    else:
        return 0.0, []

    lowered = (subject or "").lower()
    if any(token in lowered for token in _SOW_SUBJECT_TOKENS):
        # The reason, not the subject line. A subject can carry client detail
        # and it is not needed to justify the suggestion.
        reasons.append("subject_mentions_sow")
        score += _W_SUBJECT_SOW

    if filenames:
        sow_named = [
            name
            for name in filenames
            if any(token in name.lower() for token in _SOW_FILENAME_TOKENS)
        ]
        if sow_named:
            score += _W_ATTACHMENT_SOW_FILENAME
            # THE ONE piece of message-derived text this module returns. Header
            # metadata, not body content, and it is what makes the suggestion
            # judgeable — "close this?" with nothing to look at is not a
            # question a person can answer.
            reasons.append(f"attachment:{sow_named[0]}")
        else:
            score += _W_ATTACHMENT
            reasons.append("attachment")

    if from_pandadoc_lane:
        score += _W_PANDADOC_LINK
        # Honest label. Gmail matched the term somewhere in the THREAD; Cubie
        # does not read bodies, so it cannot confirm the AE's own message
        # carried the link rather than a PandaDoc notification the client
        # forwarded. Weighted below the attachment lane for that reason, and it
        # is one of the reasons this is a suggestion and not a close.
        reasons.append("pandadoc_in_thread")

    return min(score, 1.0), reasons


def _suggestion_text(sent_at: datetime | None) -> str:
    """The exact sentence the cockpit renders next to [ Yes ] [ No ].

    Dated in the workspace's control-row time zone, not the reader's timezone: the
    cockpit runs on one clock org-wide, because an SLA that means something
    different per AE is not auditable and the sales manager could not reason
    about it. The same rule applies to the date a person is asked to confirm.
    """
    if sent_at is None:
        return "Looks like the SOW went out — close this?"
    local = sent_at.replace(tzinfo=timezone.utc).astimezone(workspace_time_zone())
    # Explicit day/month rather than a %-d format: the dash-flag is glibc/BSD
    # only and silently differs between a Mac laptop and the Cloud Run image.
    return f"Looks like the SOW went out on {local.day} {local:%b} — close this?"


# ── Entry point ──────────────────────────────────────────────────────────────


def scan_sent_mail_detailed(
    db: Session,
    owner_email: str,
    since: datetime | None = None,
    *,
    hs: HubSpotWriteClient | None = None,
) -> ScanOutcome:
    """Close SUGGESTIONS for one AE's live SOW commitments, plus WHY there are none.

    At most one suggestion per commitment — the highest-scoring message — so the
    AE is never shown two competing "close this?" prompts for one obligation.

    The mailbox is not touched unless there is a live SOW commitment a
    suggestion could close. That ordering is deliberate: reading somebody's Sent
    folder needs a reason that exists before the read, not one discovered during
    it.

    Confirming a suggestion is
    ``commitment_engine.close_commitment(db, commitment, source="manual",
    evidence_ref=<message_id>, actor=<the person who clicked>)``. There is no
    close path in this module, by design.

    Returns a :class:`ScanOutcome` rather than a list because an empty list has
    five different meanings (see the module docstring) and three of them are
    outages. Callers that only want the suggestions can use ``scan_sent_mail``,
    which raises on the outage cases instead of returning empty.

    ``hs`` is the injection point for tests. It is the write client because that
    is where the repo's only fetch-one-by-id lives (``get_object``, which reads
    with the READ token) and it exposes ``read_client`` for the search calls —
    no ``HUBSPOT_WRITE_TOKEN`` is required and nothing here writes to HubSpot.
    """
    owner = _norm(owner_email)
    if not owner:
        raise ValueError("scan_sent_mail requires an owner_email")

    commitments = (
        db.query(Commitment)
        .filter(
            Commitment.kind == "sow",
            Commitment.closed_at.is_(None),
            func.lower(Commitment.owner_email) == owner,
        )
        .all()
    )
    if not commitments:
        logger.info("[sow-sent] %s: no live SOW commitments — mailbox not read", owner)
        return ScanOutcome(status=SCAN_NO_COMMITMENTS)

    if since is None:
        since = datetime.utcnow() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)

    hs = hs or HubSpotWriteClient()
    contact_map = _deal_contact_emails(
        {str(c.hubspot_deal_id) for c in commitments if c.hubspot_deal_id}, hs=hs
    )
    domain_map = _company_domains(
        {str(c.hubspot_company_id) for c in commitments if c.hubspot_company_id}, hs=hs
    )

    client = GmailClient(user_email=owner)
    if not client.service:
        # GmailClient logs and returns a None service rather than raising, so an
        # unavailable mailbox has to be checked for. Named status, because "zero
        # suggestions" and "we never looked" must not read the same in a report:
        # the second one leaves live commitments aging toward a breach that a
        # working detector would have offered to close.
        logger.warning(
            "[sow-sent] %s: Gmail UNAVAILABLE (no service) — %d live SOW "
            "commitment(s) went unscanned. This is a delegation/credential "
            "failure, not a quiet mailbox.",
            owner,
            len(commitments),
        )
        return ScanOutcome(
            status=SCAN_MAILBOX_UNAVAILABLE, live_commitments=len(commitments)
        )

    after = _gmail_after(since)
    lanes: list[tuple[str, str, bool]] = [
        ("attachment", f"in:sent has:attachment after:{after}", False),
        (
            "pandadoc",
            f"in:sent ({' OR '.join(_PANDADOC_QUERY_TERMS)}) after:{after}",
            True,
        ),
    ]

    # Union across lanes, deduped by message id, remembering which lane found
    # each message so the PandaDoc weight can be applied honestly.
    candidates: dict[str, tuple[dict, bool]] = {}
    lanes_searched = 0
    failed_lanes: list[str] = []
    for lane_name, query, is_pandadoc_lane in lanes:
        try:
            threads = client.search_threads(
                query=query, max_results=_MAX_THREADS_PER_LANE
            )
        except Exception as exc:  # noqa: BLE001 — one lane, not the scan
            # Counted, not just logged. One lane 429ing narrows the search
            # without changing the shape of the result, so an absence after a
            # partial search must not be reported as a clean "nothing found".
            failed_lanes.append(lane_name)
            logger.warning(
                "[sow-sent] %s: lane %s (%r) failed: %s", owner, lane_name, query, exc
            )
            continue
        lanes_searched += 1
        for thread in threads or []:
            for msg in thread.get("messages") or []:
                # `in:sent` matches the THREAD, so a hydrated thread carries the
                # client's replies too — including, on the attachment lane, an
                # inbound message that is the one holding the attachment.
                # Filter to messages the AE actually sent.
                if owner not in _addresses(_header(msg, "From")):
                    continue
                key = _message_key(msg)
                previous = candidates.get(key)
                if previous is None:
                    candidates[key] = (msg, is_pandadoc_lane)
                elif is_pandadoc_lane:
                    candidates[key] = (previous[0], True)

    if not lanes_searched:
        # The mailbox opened and then answered nothing at all. Same consequence
        # as an unavailable mailbox — no evidence was seen — so it gets its own
        # status rather than an empty list that reads as a quiet week.
        logger.warning(
            "[sow-sent] %s: ALL %d search lane(s) failed (%s) — nothing was "
            "searched; %d live SOW commitment(s) went unscanned",
            owner,
            len(lanes),
            ", ".join(failed_lanes),
            len(commitments),
        )
        return ScanOutcome(
            status=SCAN_LANES_FAILED,
            live_commitments=len(commitments),
            lanes_failed=len(failed_lanes),
        )

    status = SCAN_LANES_DEGRADED if failed_lanes else SCAN_OK
    if failed_lanes:
        logger.warning(
            "[sow-sent] %s: %d of %d search lane(s) failed (%s) — the search was "
            "NARROWED; a missing suggestion here is not evidence of a quiet mailbox",
            owner,
            len(failed_lanes),
            len(lanes),
            ", ".join(failed_lanes),
        )

    if not candidates:
        return ScanOutcome(
            status=status,
            live_commitments=len(commitments),
            lanes_searched=lanes_searched,
            lanes_failed=len(failed_lanes),
        )

    owner_domain = owner.rsplit("@", 1)[-1]
    best: dict[str, dict] = {}

    for msg, from_pandadoc_lane in candidates.values():
        recipients = _addresses(_header(msg, "To")) + _addresses(_header(msg, "Cc"))
        external = [
            addr for addr in recipients if addr.rsplit("@", 1)[-1] != owner_domain
        ]
        sent_at = _sent_at(msg)
        subject = _header(msg, "Subject")
        filenames = _attachment_filenames(msg)

        for commitment in commitments:
            score, reasons = _score(
                commitment=commitment,
                sent_at=sent_at,
                external_recipients=external,
                subject=subject,
                filenames=filenames,
                from_pandadoc_lane=from_pandadoc_lane,
                # ``or ""`` rather than ``str(None)``: a commitment can legitimately
                # have no deal yet, and "None" as a lookup key would collide with
                # a HubSpot id that happened to be that literal string.
                contact_emails=contact_map.get(
                    str(commitment.hubspot_deal_id or ""), set()
                ),
                company_domain=domain_map.get(
                    str(commitment.hubspot_company_id or ""), ""
                ),
            )
            if score < MIN_SUGGESTION_CONFIDENCE:
                continue
            current = best.get(commitment.id)
            if current is not None and current["confidence"] >= score:
                continue
            best[commitment.id] = {
                "commitment_id": commitment.id,
                "owner_email": owner,
                "hubspot_deal_id": commitment.hubspot_deal_id,
                "hubspot_company_id": commitment.hubspot_company_id,
                # The evidence ref a confirming click stores on the commitment.
                # The id, not the message.
                "message_id": str(msg.get("id") or ""),
                "thread_id": str(msg.get("threadId") or ""),
                "sent_at": sent_at,
                "confidence": round(score, 3),
                "match_reasons": reasons,
                "suggestion": _suggestion_text(sent_at),
            }

    suggestions = sorted(best.values(), key=lambda s: s["confidence"], reverse=True)
    logger.info(
        "[sow-sent] %s: %d live SOW commitment(s), %d sent message(s) scored, "
        "%d suggestion(s), status=%s (%d/%d lanes searched)",
        owner,
        len(commitments),
        len(candidates),
        len(suggestions),
        status,
        lanes_searched,
        len(lanes),
    )
    return ScanOutcome(
        status=status,
        suggestions=suggestions,
        live_commitments=len(commitments),
        lanes_searched=lanes_searched,
        lanes_failed=len(failed_lanes),
        messages_scored=len(candidates),
    )


def scan_sent_mail(
    db: Session,
    owner_email: str,
    since: datetime | None = None,
    *,
    hs: HubSpotWriteClient | None = None,
) -> list[dict]:
    """``scan_sent_mail_detailed`` for callers that only want the suggestions.

    Same scan, list-shaped — and it REFUSES to express an outage as an empty
    list: :class:`MailboxUnavailable` and :class:`AllLanesFailed` are raised
    instead. A caller rendering a cockpit row can catch them and say "we could
    not check"; a caller that catches nothing gets a loud failure rather than a
    silent zero, which is the strictly safer default for a signal whose absence
    means "this commitment keeps aging".

    An empty list from this function therefore means one of the two honest
    empties: no live SOW commitment to close, or a searched mailbox with nothing
    matching. A partially degraded search still returns its suggestions — use
    ``scan_sent_mail_detailed`` when the distinction matters.
    """
    outcome = scan_sent_mail_detailed(db, owner_email, since, hs=hs)
    if outcome.status == SCAN_MAILBOX_UNAVAILABLE:
        raise MailboxUnavailable(
            f"Gmail unavailable for {_norm(owner_email)}: "
            f"{outcome.live_commitments} live SOW commitment(s) went unscanned"
        )
    if outcome.status == SCAN_LANES_FAILED:
        raise AllLanesFailed(
            f"every Gmail search lane failed for {_norm(owner_email)}: "
            f"{outcome.live_commitments} live SOW commitment(s) went unscanned"
        )
    return outcome.suggestions
