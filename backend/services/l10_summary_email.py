"""Post-meeting L10 summary email — the recording, read back to the room.

What this does, in the order it decides things:

1. Take every L10 session that ENDED in the recent past and hasn't been mailed.
2. Keep only meetings whose TITLE carries "L10" (product scope — see
   ``rubik_core.eos.l10_recording.is_l10_title``).
3. Find that session's RECORDING. A recorded meeting takes the transcript path
   (steps 4-6). A meeting with no usable recording, once it can no longer get
   one, takes the DIGEST-ONLY path below instead.
4. Work out who the transcript itself puts IN the meeting — not who was
   invited (see ``present_participants``; the transcript ACL is a superset that
   includes no-shows).
5. Filter those people through the L10 meeting's OWN security. This is the
   whole ballgame for the Exec team: ``can_access_meeting`` is the same
   function the Exec L10's screens gate on, so a non-exec who sat in the Exec
   L10 gets no email, exactly as they get no access to its notes in Cubie. It
   also honours the roster-is-the-ACL rule for calendar-imported meetings and
   department membership for hand-scheduled ones — one implementation, no
   second opinion about who may read a team's meeting.
6. Summarize the recording TOGETHER WITH the session's own entries (notes,
   ratings, check-ins, issues solved, to-dos created — see
   ``l10_session_digest``), grounded strictly in both, and mail it.

**The digest-only recap (a meeting that was never recorded).** Plenty of teams
simply don't turn Gemini / Fathom on — for them "no recording, no email" meant
Cubie said nothing about a meeting whose whole record it was already holding.
So when a session's recording WINDOW has closed (``find_recording`` can no
longer claim a transcript for it — see :func:`recording_window_closed`) and the
team did enter something, the recap is written from the session's own EOS
entries alone: closing notes, per-person ratings, check-ins, issues solved,
to-dos created. It is clearly labelled as such, and it says only what somebody
typed into Cubie — there is no transcript in it, invented or otherwise.

Waiting for the window to close is the whole trick: sending earlier would beat
a transcript that was still coming and burn the once-only send on the thinner
email. Observed publish delays run to 27 h, which is why the wait is the full
recording window rather than a guess.

Its audience is chosen NARROWLY, by ``_digest_audience`` rather than by the
department fence the recorded path can lean on — read that function's docstring
before widening it. The short version: an Exec meeting whose ``is_exec_meeting``
flag was never set (one titled "Team L10" rather than "ET L10", say) must not be
able to mail its contents to a department, so the digest only ever reaches
people with evidence of being invited to or present at that exact meeting.

**Why a sweep and not a hook on Conclude.** A Gemini / Fathom transcript
appears minutes to hours after the room empties, and ``transcript_sync`` only
ingests it on its next 15-minute pass. Sending at Conclude time would find no
recording essentially every time, i.e. would send nothing. So the sweep runs
periodically over a lookback window and mails a session the first pass on
which its recording exists. ``eos_l10_sessions.summary_email_sent_at`` is what
makes that exactly-once; a failure deliberately leaves it NULL so the next pass
retries, until the session falls out of the window.

Nothing here raises: every session is judged independently and its outcome
recorded, so one broken meeting can't stop the rest of the company's mail.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from rubik_core.brand import BRAND_HEX, PRODUCT_NAME
from rubik_core.company.domain import workspace_domain
from rubik_core.company.operations import get_operations_config
from rubik_core.company.profile import (
    company_name_for_documents,
    get_company_profile,
)
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.database.eos import L10Meeting, L10Session, Person
from rubik_core.eos.l10_recording import (
    DEFAULT_BOX_MINUTES,
    MIN_TRANSCRIPT_CHARS,
    RECORDING_LAG,
    Recording,
    find_recording,
    is_l10_title,
    naive_utc,
    present_participants,
    read_transcript_text,
)
from rubik_core.tenancy.workspace_time import workspace_time_zone

logger = logging.getLogger(__name__)

# How far back a finished session may be and still get a summary. Wide enough
# to cover a Friday-afternoon L10 whose transcript only lands on Saturday, and
# to survive a scheduler outage over a weekend; short enough that a session
# nobody could mail stops being reconsidered (and stops being re-queried) after
# a few days rather than forever.
DEFAULT_LOOKBACK = timedelta(hours=72)

# Transcript budget for the prompt. The digest of the session's own entries is
# small; the recording is what costs, so it is capped rather than the pair.
TRANSCRIPT_PROMPT_BYTES = 120_000


# ── Outcomes ────────────────────────────────────────────────────────────────

# Terminal, no retry: the answer will not change on a later pass.
STATUS_ALREADY_SENT = "already_sent"
STATUS_NOT_L10 = "not_l10"
STATUS_SENT = "sent"
# Sent from the session's own EOS entries, with no recording behind it.
STATUS_SENT_DIGEST_ONLY = "sent_digest_only"
# Ended before the workspace go-live floor, or no valid floor is configured.
STATUS_BEFORE_START = "before_start"
# Retryable: the transcript may still arrive, or the send may work next time.
STATUS_NOT_RECORDED = "not_recorded"
STATUS_NO_RECIPIENTS = "no_recipients"
STATUS_FAILED = "failed"
# Unrecorded AND the team entered nothing — there is no meeting to describe, so
# neither path can say anything. Not an error, and nothing to retry into.
STATUS_NO_MATERIAL = "no_material"
# Another session of the SAME meeting already got this occurrence's recap.
STATUS_ALREADY_COVERED = "already_covered"

# Ops states.
STATUS_WOULD_SEND = "would_send"  # dry run
STATUS_DISABLED = "disabled"  # autosend kill switch is off

# How close two sessions of one meeting must be for the later one to count as
# the same occurrence. An L10 is weekly, so 48 h cannot reach the next real
# occurrence (168 h away) — while an abandoned session row routinely sits a day
# or so before the real one: dev holds a "Team L10" pair ending 09-08 19:21 and
# 09-09 18:33, 23 h apart, for one Wednesday meeting.
#
# Only the DIGEST path needs this. The recorded path is already single-shot per
# occurrence because a recording belongs to exactly one session
# (``_claimed_doc_ids``); a digest is built from rows, which both sessions can
# have, so nothing stops two of them describing one meeting.
DIGEST_DEDUPE_WINDOW = timedelta(hours=48)


@dataclass
class SessionOutcome:
    session_id: UUID
    meeting_name: str
    is_exec_meeting: bool
    status: str
    actual_end: datetime | None = None
    doc_id: str | None = None
    recording_title: str | None = None
    recipients: list[str] = field(default_factory=list)
    # People on the recording who were dropped, and why — this is the answer to
    # "why didn't I get the recap?", so it is reported rather than swallowed.
    excluded: dict[str, str] = field(default_factory=dict)
    message_id: str | None = None
    error: str | None = None
    # True when this recap was written from the session's EOS entries alone,
    # with no recording behind it. Reported so "why is this one thinner?" has
    # an answer in the ops log rather than only in the email's own footer.
    digest_only: bool = False

    def describe(self) -> str:
        who = ", ".join(self.recipients) if self.recipients else "-"
        return (
            f"{self.status:17} {self.meeting_name[:34]:34} "
            f"ended={self.actual_end:%Y-%m-%d %H:%M} "
            f"recording={'yes' if self.doc_id else 'no ':3} to={who}"
        )


@dataclass
class SweepReport:
    outcomes: list[SessionOutcome]
    dry_run: bool
    now: datetime
    enabled: bool = True

    def by_status(self, status: str) -> list[SessionOutcome]:
        return [o for o in self.outcomes if o.status == status]

    def summary(self) -> str:
        if not self.enabled:
            return "L10 summary email is disabled (CUBIE_L10_SUMMARY_EMAIL_ENABLED)"
        counts: dict[str, int] = {}
        for o in self.outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing"
        return f"{len(self.outcomes)} finished L10 session(s): {parts}"


# ── Recipient resolution ────────────────────────────────────────────────────


def _company_domain() -> str:
    return workspace_domain()


def _company_domains() -> frozenset[str]:
    from rubik_core.company.domain import workspace_domains

    return workspace_domains()


def start_floor() -> datetime | None:
    """Earliest meeting end eligible for mail, as naive UTC.

    A missing or unreadable workspace floor returns ``None``. The caller treats
    that as a send block, including in a dry-run report. A bare date is local
    midnight in the active workspace's IANA zone.
    """
    try:
        raw = get_operations_config().l10_start_after
        if not raw:
            return None
        zone = workspace_time_zone()
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 — unknown policy must never widen the floor
        logger.exception("[l10-summary] workspace go-live floor unavailable")
        return None


def recording_window_closed(session, meeting, *, now: datetime) -> bool:
    """True once no transcript can still be claimed for this session.

    The digest-only recap must never beat a recording that is merely late: the
    send happens exactly once, so an early digest email would permanently
    replace the fuller one. This mirrors :func:`find_recording`'s own upper
    bound — ``actual_start + box + RECORDING_LAG`` — so the two can never
    disagree about whether a transcript was still possible.

    Anchored on ``actual_start`` for the same reason ``find_recording`` is: an
    auto-closed session's ``actual_end`` drifts for as long as nobody concludes
    the meeting (dev has held sessions open for 676 h), and an end-anchored
    wait would postpone the digest for as long as the row was left open.
    """
    start = naive_utc(getattr(session, "actual_start", None))
    if start is None:
        # Never started, so no recording was ever possible for it — nothing to
        # wait for. (A session with no start also has no digest window, so the
        # digest itself will come back empty and stop this anyway.)
        return True
    box = timedelta(
        minutes=getattr(meeting, "duration_minutes", None) or DEFAULT_BOX_MINUTES
    )
    return now > start + box + RECORDING_LAG


def _meeting_expected_emails(db, meeting) -> set[str]:
    """The people this meeting is *for*: its calendar invitees, or its team.

    This is the hard attendance gate that stops :func:`find_recording` from
    claiming another meeting's transcript on a title-and-time coincidence, so
    it matters most where a mismatch would hurt most. In priority order:

    * a calendar-imported meeting → its invite list;
    * the Exec L10 → the Exec team. It usually has neither a roster nor a
      department, which would otherwise leave the ONE meeting whose contents
      must never reach the wrong people with no attendance check at all;
    * a department meeting → that department's members.

    Empty only for an unscoped, hand-scheduled, non-exec meeting — which is
    company-wide anyway, so title + time window carry the match alone.
    """
    from backend.routers.eos._authz import meeting_roster
    from rubik_core.database.models import User

    roster = set(meeting_roster(meeting))
    if roster:
        return roster
    if bool(getattr(meeting, "is_exec_meeting", False)):
        return {
            (u.email or "").lower()
            for u in db.query(User).filter(User.is_platform_support.is_(False)).all()
            if u.email and u.is_exec
        }
    dept = getattr(meeting, "department_name", None)
    if not dept:
        return set()
    return {
        (u.email or "").lower()
        for u in db.query(User).filter(User.is_platform_support.is_(False)).all()
        if (u.department or "").strip().lower() == dept.strip().lower() and u.email
    }


def _display_names(db, emails: set[str]) -> dict[str, str]:
    """email → best display name, for matching people against a transcript.

    Prefers the Workspace/app name (which is what Meet and Fathom label
    speakers with) and falls back to the EOS ``Person`` first/last name.
    """
    from rubik_core.database.models import User

    lowered = {e.lower() for e in emails if e}
    if not lowered:
        return {}
    out: dict[str, str] = {}
    for u in db.query(User).filter(User.is_platform_support.is_(False)).all():
        key = (u.email or "").lower()
        if key in lowered and (u.name or "").strip():
            out[key] = u.name.strip()
    for p in db.query(Person).all():
        key = (p.email or "").lower()
        if key in lowered and key not in out:
            full = f"{p.first_name or ''} {p.last_name or ''}".strip()
            if full:
                out[key] = full
    # Everyone stays a candidate even with no name on file — their email may
    # still appear in the transcript, and they may own the recording.
    for e in lowered:
        out.setdefault(e, "")
    return out


def resolve_recipients(
    db, meeting, recording: Recording, transcript_text: str
) -> tuple[list[str], dict[str, str]]:
    """Who gets this recap, and why everyone else didn't.

    Four gates, all of which must pass, in cheapest-first order:

    1. **Company domain.** Confidential meeting content never leaves
       the workspace's verified domain. A transcript's ACL can contain external
       addresses (they are scraped out of the text), and an external guest on
       an L10 is not an audience for its recap.
    2. **Present in the recording.** See
       :func:`~rubik_core.eos.l10_recording.present_participants` — invitees
       who never joined are dropped here.
    3. **A real Cubie account.** No ``users`` row ⇒ no profile ⇒ no way to
       evaluate the meeting's ACL, so the answer is no. Fail-closed on purpose.
    4. **The L10 meeting's own access rule** (``can_access_meeting``). The Exec
       fence, the calendar-roster rule and department membership all live in
       that one function; this feature gets no opinion of its own about who may
       read a team's meeting.
    """
    from backend.routers.eos._authz import can_access_meeting
    from rubik_core.database.repository import SQLUserRegistry

    domains = _company_domains()
    candidates = _display_names(db, set(recording.participant_emails))
    excluded: dict[str, str] = {}

    internal: dict[str, str] = {}
    for email, name in candidates.items():
        if email.rsplit("@", 1)[-1] not in domains:
            excluded[email] = "outside the company domain"
            continue
        internal[email] = name

    present = present_participants(
        transcript_text, candidates=internal, owner_email=recording.owner_email
    )
    for email in internal:
        if email not in present:
            excluded[email] = "on the invite/ACL but not present in the recording"

    # Reuse the caller's session: a fresh SessionLocal() per attendee would open
    # and close one Cloud SQL connection each, and dev's instance has a small
    # connection cap (see docs/ENGINEERING_NOTES.md, "Connection-pool budget").
    registry = SQLUserRegistry(db)
    recipients: list[str] = []
    for email in sorted(present):
        profile = registry.get_user(email)
        if profile is None:
            excluded[email] = f"no {PRODUCT_NAME} account"
            continue
        if profile.is_platform_support:
            excluded[email] = "platform support is not a meeting recipient"
            continue
        if not can_access_meeting(profile, meeting, db):
            # The Exec-team case lands here: present in the room, but not on
            # the Exec team, so not entitled to the Exec L10's content.
            excluded[email] = "no access to this L10 (meeting ACL)"
            continue
        recipients.append(email)
    return recipients, excluded


def _digest_audience(db, meeting, session) -> tuple[set[str], str]:
    """Candidate recipients for a digest-only recap, and where they came from.

    Deliberately NARROWER than ``_meeting_expected_emails``, which the
    recording matcher uses. Each source here is positive evidence that a
    specific person belongs to THIS meeting:

    1. ``attendance`` — people with an attendance row marked ``attended``,
       written by the Check-in form or the Conclude survey. Evidence they were
       in the room.
    2. ``calendar roster`` — the invite list of a calendar-imported meeting,
       which is also literally its ACL (see ``_authz.meeting_roster``).
    3. ``exec team`` — an Exec-flagged meeting with neither of the above. Safe
       by construction: the Exec team is exactly the fence on that meeting.

    **There is deliberately no department fallback**, and that omission is the
    point. ``_meeting_expected_emails`` ends with "everyone in the meeting's
    department", which is right for REJECTING a mis-matched transcript but
    wrong for choosing who to mail: it reaches people who were neither invited
    nor present. An Exec meeting that is not correctly flagged
    ``is_exec_meeting`` — one named, say, "Team L10" rather than "ET L10", which
    ``_infer_import_exec_flag`` does not recognise — would otherwise have its
    contents mailed to a whole department under the weaker department fence.
    Confidential Exec material must not depend on somebody having set a flag
    correctly, so the digest never reaches anyone with no evidence of being
    invited to or present at this specific meeting.

    The cost is that a hand-scheduled meeting with no roster and no attendance
    rows gets no digest. That is the correct answer: we have no evidence of who
    was in it.
    """
    from backend.routers.eos._authz import meeting_roster
    from rubik_core.database.eos import L10Attendance
    from rubik_core.database.models import User

    attended_ids = [
        a.person_id
        for a in db.query(L10Attendance)
        .filter(
            L10Attendance.session_id == session.id,
            L10Attendance.is_deleted == False,  # noqa: E712
        )
        .all()
        if a.attended and a.person_id
    ]
    if attended_ids:
        attended = {
            (p.email or "").lower()
            for p in db.query(Person).filter(Person.id.in_(attended_ids)).all()
            if (p.email or "").strip()
        }
        if attended:
            return attended, "attendance"

    roster = set(meeting_roster(meeting))
    if roster:
        return roster, "calendar roster"

    if bool(getattr(meeting, "is_exec_meeting", False)):
        return {
            (u.email or "").lower()
            for u in db.query(User).filter(User.is_platform_support.is_(False)).all()
            if u.email and u.is_exec
        }, "exec team"

    return set(), "no evidence of who attended"


def resolve_digest_recipients(
    db, meeting, session, digest
) -> tuple[list[str], dict[str, str]]:
    """Who gets a recap that has no recording behind it.

    The recorded path asks "who does the transcript show was in the room?".
    With no transcript there is nothing to ask that of, so the audience comes
    from the session's own record of who took part — see :func:`_digest_audience`
    for the sources and, importantly, for the department fallback that is
    deliberately absent.

    Every candidate is then put through the two gates that are NOT relaxed: the
    company domain, and the meeting's own ACL (``can_access_meeting``) — so the
    Exec L10's digest stays on the Exec team just as its recording-backed recap
    does. Belt and braces: the audience is already narrow, and the ACL is the
    same one that hides the meeting's notes in the app.
    """
    from backend.routers.eos._authz import can_access_meeting
    from rubik_core.database.repository import SQLUserRegistry

    candidates, source = _digest_audience(db, meeting, session)

    domains = _company_domains()
    registry = SQLUserRegistry(db)
    recipients: list[str] = []
    excluded: dict[str, str] = {}
    for email in sorted(e for e in candidates if e):
        if email.rsplit("@", 1)[-1] not in domains:
            excluded[email] = "outside the company domain"
            continue
        profile = registry.get_user(email)
        if profile is None:
            excluded[email] = f"no {PRODUCT_NAME} account"
            continue
        if profile.is_platform_support:
            excluded[email] = "platform support is not a meeting recipient"
            continue
        if not can_access_meeting(profile, meeting, db):
            excluded[email] = "no access to this L10 (meeting ACL)"
            continue
        recipients.append(email)
    logger.info(
        "[l10-summary] digest-only audience for %r resolved from %s: %d candidate(s), "
        "%d recipient(s)",
        getattr(meeting, "name", "?"),
        source,
        len(candidates),
        len(recipients),
    )
    return recipients, excluded


# ── The summary itself ──────────────────────────────────────────────────────

_PROMPT = """You are writing the post-meeting recap of an EOS Level 10 (L10) \
team meeting, emailed to the people who were in the room.

You have TWO sources and must use both:
  * the RECORDING TRANSCRIPT — what was actually said;
  * the MEETING RECORD — what the team entered in the {product} EOS tool for this \
same meeting (closing notes, per-person ratings and comments, check-ins, \
issues solved, to-dos created).

Where they overlap, the MEETING RECORD is authoritative for names, owners, \
numbers and statuses; the transcript is authoritative for what was discussed \
and decided. Never invent an issue, a name, an owner, a date or a number that \
is not in one of the two sources, and never guess at something that was only \
alluded to.

Write it for people who were there: no preamble, no "in this meeting we", no \
restating the agenda. Use this exact plain-text structure, omitting any section \
that would be empty:

## Summary
Two to four sentences on what actually happened and what it means.

## Decisions and issues solved
- One line each, naming who owns any follow-through.

## New to-dos
- One line each: the to-do, then the owner in parentheses.

## Carrying into next week
- Only things explicitly left open, parked, or promised for next time.

Do not add any other section, heading, or closing line.

=== RECORDING TRANSCRIPT ===
{transcript}
=== END TRANSCRIPT ===

=== MEETING RECORD ({product} EOS entries for this session) ===
{data_block}
=== END MEETING RECORD ===
"""


_DIGEST_ONLY_PROMPT = """You are writing the post-meeting recap of an EOS Level \
10 (L10) team meeting, emailed to the people who were in the room.

This meeting was NOT recorded. You have exactly ONE source: the MEETING RECORD \
— what the team entered in the {product} EOS tool during this meeting (closing \
notes, per-person ratings and comments, check-ins, issues solved, to-dos \
created). There is no transcript.

That makes your job narrow and you must keep it narrow. Report what the record \
contains and nothing else. Never infer what was discussed, never reconstruct a \
conversation, never explain WHY something was decided unless the record says \
so, and never invent an issue, a name, an owner, a date or a number. If the \
record is thin, the recap is short — a short accurate recap is the goal, not a \
padded one. Do not comment on the absence of a recording; the email says that \
itself.

Write it for people who were there: no preamble, no "in this meeting we", no \
restating the agenda. Use this exact plain-text structure, omitting any section \
that would be empty:

## Summary
One to three sentences on what the record shows the meeting covered.

## Decisions and issues solved
- One line each, naming who owns any follow-through.

## New to-dos
- One line each: the to-do, then the owner in parentheses.

Do not add any other section, heading, or closing line.

=== MEETING RECORD ({product} EOS entries for this session) ===
{data_block}
=== END MEETING RECORD ===
"""


@attribute_to("l10_summary_email")
def generate_digest_summary(*, data_block: str) -> str:
    """The recap body for an unrecorded meeting, grounded in its EOS entries.

    Shares the ``l10_summary_email`` cost category with the recorded path: it
    is the same feature reaching the same people, and splitting it would just
    fragment one line item on the AI Cost tab.

    Raises on LLM failure, exactly like :func:`generate_summary` — the caller
    records ``failed`` and leaves ``summary_email_sent_at`` NULL to retry.
    """
    from rubik_core.ai_providers.runtime import generate_text

    text = generate_text(
        "l10_summary_email",
        _DIGEST_ONLY_PROMPT.format(product=PRODUCT_NAME, data_block=data_block),
        default_model=settings.VERTEX_LLM_MODEL_PRO,
    ).text.strip()
    if not text:
        raise RuntimeError("empty digest-only summary from the model")
    return text


@attribute_to("l10_summary_email")
def generate_summary(*, transcript_text: str, data_block: str) -> str:
    """The recap body, grounded in the recording + the session's entries.

    Raises on LLM failure — the caller records the session as ``failed`` and
    leaves ``summary_email_sent_at`` NULL so the next sweep retries.
    """
    from rubik_core.ai_providers.runtime import generate_text

    prompt = _PROMPT.format(
        product=PRODUCT_NAME,
        transcript=transcript_text[:TRANSCRIPT_PROMPT_BYTES],
        data_block=data_block,
    )
    text = generate_text(
        "l10_summary_email", prompt, default_model=settings.VERTEX_LLM_MODEL_PRO
    ).text.strip()
    if not text:
        raise RuntimeError("empty summary from the model")
    return text


# ── Rendering ───────────────────────────────────────────────────────────────


def _provenance_text(recording_title: str | None) -> str:
    """The email's footer — what this recap was written from, and who got it.

    ``recording_title`` is ``None`` for a digest-only recap, and the wording
    then has to change on BOTH counts: there is no recording to name, and the
    "only people the recording shows were in the room" promise is one this
    path cannot make (see :func:`resolve_digest_recipients`). Saying it anyway
    would be the email quietly lying about its own provenance.
    """
    if recording_title is None:
        return (
            f"Written by {PRODUCT_NAME} from the entries in this L10 — closing notes, "
            "ratings, check-ins, issues solved and to-dos. This meeting was not "
            "recorded, so nothing here comes from a transcript and anything "
            "discussed but never logged is missing. Sent to the meeting's team. "
            "Check anything before you act on it."
        )
    return (
        f"Written by {PRODUCT_NAME} from this meeting's recording "
        f"({recording_title}) and the entries in its L10. "
        "Sent only to the people the recording shows were in the room. "
        "Check anything before you act on it."
    )


def _recap_kicker(company_name: str | None) -> str:
    """The line above the meeting name: whose recap this is.

    The company's name (``company_name_for_documents``: the company profile's
    name, else the product's), never "None" or a bare " · L10 recap".
    """
    company = (company_name or "").strip() or company_name_for_documents()
    return f"{company} · L10 recap"


def render_html(
    *,
    meeting_name: str,
    when: str,
    summary: str,
    recording_title: str | None,
    company_name: str | None = None,
) -> str:
    """The summary as an email-safe HTML fragment.

    A deliberately tiny renderer over the fixed structure the prompt asks for
    (``## `` headings, ``- `` bullets, paragraphs) rather than a markdown
    dependency — and every piece of model or database text goes through
    :func:`html.escape`, because meeting names, to-do text and issue titles are
    all user-authored and an email client will happily render injected markup.
    The company's name comes from the profile an admin typed, so it is escaped
    too.
    """
    brand = BRAND_HEX
    out: list[str] = [
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,sans-serif;'
        'max-width:640px;color:#1f1a1b;line-height:1.5">',
        '<div style="font-size:12px;font-weight:600;letter-spacing:.05em;'
        'text-transform:uppercase;color:#6d6467;margin-bottom:4px">'
        f"{html.escape(_recap_kicker(company_name))}</div>",
        f'<h2 style="margin:0 0 2px;font-size:19px;color:{brand}">'
        f"{html.escape(meeting_name)}</h2>",
        f'<div style="color:#6d6467;font-size:13px;margin-bottom:18px">'
        f"{html.escape(when)}</div>",
    ]

    bullets_open = False

    def close_bullets() -> None:
        nonlocal bullets_open
        if bullets_open:
            out.append("</ul>")
            bullets_open = False

    for raw in (summary or "").splitlines():
        line = raw.strip()
        if not line:
            close_bullets()
            continue
        if line.startswith("#"):
            close_bullets()
            heading = html.escape(line.lstrip("#").strip())
            out.append(
                '<div style="font-size:12px;font-weight:600;letter-spacing:.05em;'
                "text-transform:uppercase;color:#6d6467;margin:18px 0 6px"
                f'">{heading}</div>'
            )
            continue
        if line.startswith(("- ", "* ", "• ")):
            if not bullets_open:
                out.append('<ul style="margin:0 0 4px;padding-left:20px">')
                bullets_open = True
            out.append(
                f'<li style="margin-bottom:4px">{html.escape(line[2:].strip())}</li>'
            )
            continue
        close_bullets()
        out.append(f'<p style="margin:0 0 10px">{html.escape(line)}</p>')
    close_bullets()

    out.append(
        '<p style="color:#6d6467;font-size:12px;margin-top:22px;'
        'border-top:1px solid #e7e2e3;padding-top:12px">'
        f"{html.escape(_provenance_text(recording_title))}</p>"
    )
    out.append("</div>")
    return "".join(out)


def render_text(
    *,
    meeting_name: str,
    when: str,
    summary: str,
    recording_title: str | None,
    company_name: str | None = None,
) -> str:
    return (
        f"{_recap_kicker(company_name)}\n{meeting_name}\n{when}\n\n{summary}\n\n"
        f"— {_provenance_text(recording_title)}\n"
    )


def _sender_mailbox(
    db, meeting, recording: Recording | None, recipients: list[str] | None = None
) -> str:
    """The real Workspace mailbox the shared service account sends AS.

    Domain-wide delegation can only impersonate a mailbox that exists, so this
    walks from most to least meaningful: an explicitly configured sender, then
    the meeting's own facilitator (a recap "from" whoever runs the L10 reads
    right), then the person whose recording it is.

    A digest-only meeting may have neither a facilitator nor a recording owner.
    In that case, the first recipient already selected by this report's ACL
    sends to themselves. Every chosen sender must be in this workspace.
    """
    from rubik_core.tools.email_sender import _workspace_address_allowed

    configured = get_operations_config().l10_sender_mailbox
    if configured:
        selected = configured
    else:
        selected = ""
        manager_id = getattr(meeting, "manager_id", None)
        if manager_id is not None:
            person = db.query(Person).filter(Person.id == manager_id).first()
            selected = (getattr(person, "email", "") or "").strip().lower()
        if not selected and recording is not None:
            selected = recording.owner_email or ""
        if not selected and recipients:
            selected = recipients[0]
    if not selected or not _workspace_address_allowed(selected):
        raise ValueError("No valid sender mailbox in this workspace")
    return selected


# ── The sweep ───────────────────────────────────────────────────────────────


def _claimed_doc_ids(db) -> set[str]:
    """Recordings already mailed for some session.

    A recording belongs to exactly one meeting occurrence, so one that has
    already been used is not a candidate again. Belt-and-braces next to the
    tight match window: two sessions of the same meeting (a restart, or an
    abandoned row plus the real one) must never both mail the same recording to
    the same people.
    """
    rows = (
        db.query(L10Session.summary_email_doc_id)
        .filter(L10Session.summary_email_doc_id.isnot(None))
        .all()
    )
    return {r[0] for r in rows if r[0]}


def _finished_sessions(db, *, now: datetime, lookback: timedelta) -> list:
    return (
        db.query(L10Session, L10Meeting)
        .join(L10Meeting, L10Meeting.id == L10Session.meeting_id)
        .filter(
            L10Session.is_deleted == False,  # noqa: E712
            L10Meeting.is_deleted == False,  # noqa: E712
            L10Session.actual_end.isnot(None),
            L10Session.actual_end >= now - lookback,
        )
        .order_by(L10Session.actual_end.desc())
        .all()
    )


def sweep_l10_summary_emails(
    db,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    lookback: timedelta | None = None,
    session_ids: list[UUID] | None = None,
) -> SweepReport:
    """Mail the recap for every recently-finished L10. Idempotent.

    A recorded meeting is summarized from its transcript plus its EOS entries;
    an unrecorded one whose recording window has closed gets the digest-only
    recap built from those entries alone (see the module docstring).

    ``dry_run`` resolves recordings and recipients and reports them, but spends
    no LLM tokens and sends nothing — the safe way to see who *would* be
    mailed, including for the Exec L10, without any content leaving the system.

    ⚠️ The lookback must outlast the recording window, or the digest-only path
    can never fire: a session leaves the sweep before it becomes eligible. The
    default 72 h comfortably clears the ~38 h window; a caller narrowing it
    below that gets recorded recaps only.
    """
    now = now or datetime.utcnow()
    lookback = lookback or timedelta(
        hours=settings.CUBIE_L10_SUMMARY_LOOKBACK_HOURS or 72
    )

    if not settings.CUBIE_L10_SUMMARY_EMAIL_ENABLED:
        return SweepReport(outcomes=[], dry_run=dry_run, now=now, enabled=False)

    rows = _finished_sessions(db, now=now, lookback=lookback)
    if session_ids is not None:
        wanted = set(session_ids)
        rows = [(s, m) for s, m in rows if s.id in wanted]

    claimed = _claimed_doc_ids(db)
    # occurrence key -> actual_end of the session that already carried this
    # occurrence's recap in THIS sweep. See _already_covered / _occurrence_key.
    covered: dict = {}
    outcomes: list[SessionOutcome] = []
    for session, meeting in rows:
        outcome = _process_session(
            db,
            session,
            meeting,
            dry_run=dry_run,
            claimed=claimed,
            now=now,
            covered=covered,
        )
        outcomes.append(outcome)
        # Claim it in-memory too, so two sessions in the SAME sweep can't both
        # take one recording (a dry run writes nothing, so it must track this
        # itself or its report would double-count).
        if outcome.doc_id and outcome.status in (STATUS_SENT, STATUS_WOULD_SEND):
            claimed.add(outcome.doc_id)
        # A recorded recap covers the occurrence just as a digest does, so both
        # close the door on a sibling session mailing the same meeting again.
        if outcome.status in (STATUS_SENT, STATUS_SENT_DIGEST_ONLY, STATUS_WOULD_SEND):
            end = naive_utc(getattr(session, "actual_end", None))
            if end is not None:
                covered.setdefault(_occurrence_key(meeting), end)
        if outcome.status in (
            STATUS_SENT,
            STATUS_SENT_DIGEST_ONLY,
            STATUS_FAILED,
            STATUS_NO_RECIPIENTS,
        ):
            logger.info("[l10-summary] %s", outcome.describe())
    return SweepReport(outcomes=outcomes, dry_run=dry_run, now=now)


def _process_session(
    db,
    session,
    meeting,
    *,
    dry_run: bool,
    claimed: set[str] | None = None,
    now: datetime | None = None,
    covered: dict | None = None,
) -> SessionOutcome:
    """One session's decision. Never raises."""
    outcome = SessionOutcome(
        session_id=session.id,
        meeting_name=meeting.name or "(unnamed)",
        is_exec_meeting=bool(meeting.is_exec_meeting),
        status=STATUS_NOT_L10,
        actual_end=naive_utc(session.actual_end),
    )

    # Scope: only meetings titled "L10".
    if not is_l10_title(meeting.name):
        return outcome

    # Go-live floor. Checked before everything else so a backlog of older
    # meetings costs nothing to skip and is never revisited.
    floor = start_floor()
    if floor is None:
        outcome.status = STATUS_BEFORE_START
        outcome.error = "Workspace L10 go-live date is not configured or available"
        return outcome
    if outcome.actual_end is None or outcome.actual_end < floor:
        outcome.status = STATUS_BEFORE_START
        return outcome

    if session.summary_email_sent_at is not None:
        outcome.status = STATUS_ALREADY_SENT
        outcome.doc_id = session.summary_email_doc_id
        outcome.recipients = list(session.summary_email_recipients or [])
        return outcome

    try:
        expected = _meeting_expected_emails(db, meeting)
        recording = find_recording(
            db,
            meeting_name=meeting.name or "",
            session_start=session.actual_start,
            box_minutes=getattr(meeting, "duration_minutes", None),
            expected_emails=expected,
            exclude_doc_ids=claimed,
        )
        if recording is None:
            # Not recorded (yet). Either it is still coming — retried on the
            # next pass — or the window has closed and this meeting is never
            # getting one, in which case the digest carries the recap.
            return _process_digest_only(
                db, session, meeting, outcome, dry_run=dry_run, now=now, covered=covered
            )

        outcome.doc_id = recording.doc_id
        outcome.recording_title = recording.title

        transcript_text = read_transcript_text(
            recording.source_path, max_bytes=TRANSCRIPT_PROMPT_BYTES
        )
        if len(transcript_text.strip()) < MIN_TRANSCRIPT_CHARS:
            # Unreadable, or published EMPTY by Google (a "recording" can be a
            # 3-byte BOM and nothing else). Either way
            # there is nothing to summarize FROM THE RECORDING, so this counts
            # as unrecorded and falls through to the digest rather than getting
            # a recap invented from nothing.
            outcome.error = (
                f"recording has no usable text ({len(transcript_text.strip())} "
                f"chars, need {MIN_TRANSCRIPT_CHARS})"
            )
            # Drop the doc_id: we are not mailing this recording, so it must
            # not be stamped on the session or claimed against another one.
            outcome.doc_id = None
            return _process_digest_only(
                db, session, meeting, outcome, dry_run=dry_run, now=now, covered=covered
            )

        recipients, excluded = resolve_recipients(
            db, meeting, recording, transcript_text
        )
        outcome.recipients = recipients
        outcome.excluded = excluded
        if not recipients:
            outcome.status = STATUS_NO_RECIPIENTS
            return outcome

        if dry_run:
            outcome.status = STATUS_WOULD_SEND
            return outcome

        if not settings.CUBIE_L10_SUMMARY_EMAIL_AUTOSEND:
            # Checked BEFORE generating: a dark rollout must not re-spend the
            # summary's tokens on every pass for mail that never leaves.
            outcome.status = STATUS_DISABLED
            return outcome

        sender_mailbox = _sender_mailbox(db, meeting, recording, recipients)

        from backend.services.l10_session_digest import build_session_digest
        from rubik_core.tools.email_sender import EmailMessageSpec, send_email

        digest = build_session_digest(db, session)
        summary = generate_summary(
            transcript_text=transcript_text, data_block=digest.data_block
        )

        when = _when_label(session)
        subject = f"{meeting.name} — recap for {when}"
        company = company_name_for_documents(get_company_profile(db))
        result = send_email(
            EmailMessageSpec(
                to=recipients,
                subject=subject,
                text_body=render_text(
                    meeting_name=meeting.name or "L10",
                    when=when,
                    summary=summary,
                    recording_title=recording.title,
                    company_name=company,
                ),
                html_body=render_html(
                    meeting_name=meeting.name or "L10",
                    when=when,
                    summary=summary,
                    recording_title=recording.title,
                    company_name=company,
                ),
            ),
            impersonate=sender_mailbox,
            autosend=settings.CUBIE_L10_SUMMARY_EMAIL_AUTOSEND,
        )
        outcome.message_id = result.message_id
        if result.status != "sent":
            outcome.status = STATUS_FAILED
            outcome.error = result.error or result.status
            return outcome

        # Stamp ONLY on a real send — see the module note on idempotency.
        session.summary_email_sent_at = datetime.utcnow()
        session.summary_email_doc_id = recording.doc_id
        session.summary_email_recipients = recipients
        db.commit()
        outcome.status = STATUS_SENT
        return outcome
    except Exception as exc:  # noqa: BLE001 — one bad meeting must not stop the rest
        db.rollback()
        logger.exception(
            "[l10-summary] session %s (%s) failed", session.id, outcome.meeting_name
        )
        outcome.status = STATUS_FAILED
        outcome.error = str(exc)
        return outcome


def _occurrence_key(meeting) -> tuple:
    """Identity of the MEETING a session belongs to, for de-duplication.

    Not the primary key: dev holds the same real meeting as two rows — two
    "Team L10" (Systems) and two "Sales L10 Meeting" (Sales), each pair a
    re-import of one recurring slot. Keying only on ``meeting_id`` would let
    both rows mail the same occurrence.

    The key is deliberately NAME + DEPARTMENT + the exec flag rather than name
    alone, because a name can genuinely be shared across teams: the Exec team
    may well run a "Team L10" of its own. Two same-named meetings that differ
    in team are different meetings and must both be able to send — suppressing
    one would silence a real team, which is worse than a duplicate email.
    Department is compared by RAG slug, never raw (docs/ENGINEERING_NOTES.md).
    """
    from rubik_core.rag.config import slugify_department

    name = " ".join((getattr(meeting, "name", "") or "").lower().split())
    dept = slugify_department(getattr(meeting, "department_name", None) or "") or ""
    return (name, dept, bool(getattr(meeting, "is_exec_meeting", False)))


def _already_covered(db, session, meeting, *, covered: dict | None) -> bool:
    """Has this meeting occurrence already had its recap mailed?

    Two places to look, because a duplicate can straddle either boundary:

    * ``covered`` — occurrences mailed EARLIER IN THIS SWEEP. Sessions are
      processed newest-end-first, so the real occurrence is judged before the
      abandoned row beside it and wins the send; a dry run tracks this too or
      its report would double-count.
    * the database — a sibling session already stamped
      ``summary_email_sent_at`` on an earlier sweep, whether it went out as a
      recorded recap or a digest. A recorded recap covers the occurrence just
      as well, so a digest must not follow it.

    Both look across meeting ROWS via :func:`_occurrence_key`, not just within
    one, so a re-imported duplicate of the same meeting cannot mail it twice.
    """
    end = naive_utc(getattr(session, "actual_end", None))
    if end is None:
        return False
    key = _occurrence_key(meeting)

    previous = (covered or {}).get(key)
    if previous is not None and abs(previous - end) <= DIGEST_DEDUPE_WINDOW:
        return True

    siblings = (
        db.query(L10Session, L10Meeting)
        .join(L10Meeting, L10Meeting.id == L10Session.meeting_id)
        .filter(
            L10Session.id != session.id,
            L10Session.is_deleted == False,  # noqa: E712
            L10Meeting.is_deleted == False,  # noqa: E712
            L10Session.summary_email_sent_at.isnot(None),
            L10Session.actual_end.isnot(None),
            L10Session.actual_end >= end - DIGEST_DEDUPE_WINDOW,
            L10Session.actual_end <= end + DIGEST_DEDUPE_WINDOW,
        )
        .all()
    )
    # The ±48 h candidate set is a handful of rows, so the key comparison is
    # done in Python rather than as a three-column SQL predicate that would
    # have to re-implement slugify_department in the database.
    return any(_occurrence_key(m) == key for _s, m in siblings)


def _process_digest_only(
    db,
    session,
    meeting,
    outcome: SessionOutcome,
    *,
    dry_run: bool,
    covered: dict | None = None,
    now: datetime | None,
) -> SessionOutcome:
    """Recap an unrecorded meeting from its own EOS entries. Never raises.

    Called when the recorded path found nothing usable. Everything here is
    conditional on the recording window being CLOSED — until then the honest
    answer is still ``not_recorded``, because a transcript may yet arrive and
    the send happens only once.
    """
    outcome.status = STATUS_NOT_RECORDED
    if not settings.CUBIE_L10_DIGEST_ONLY_ENABLED:
        return outcome
    if not recording_window_closed(session, meeting, now=now or datetime.utcnow()):
        # Still inside the window a recording could land in. Wait.
        return outcome

    if _already_covered(db, session, meeting, covered=covered):
        # A sibling session of this meeting already carried this occurrence's
        # recap — typically the real session, with this one the abandoned row
        # beside it. One meeting, one email.
        outcome.status = STATUS_ALREADY_COVERED
        return outcome

    from backend.services.l10_session_digest import build_session_digest

    digest = build_session_digest(db, session)
    if not digest.has_material:
        # No recording AND nothing logged: there is genuinely no meeting to
        # describe. Sending an empty-handed recap would be worse than silence.
        outcome.status = STATUS_NO_MATERIAL
        return outcome

    outcome.digest_only = True
    recipients, excluded = resolve_digest_recipients(db, meeting, session, digest)
    outcome.recipients = recipients
    outcome.excluded = excluded
    if not recipients:
        outcome.status = STATUS_NO_RECIPIENTS
        return outcome

    if dry_run:
        outcome.status = STATUS_WOULD_SEND
        return outcome

    if not settings.CUBIE_L10_SUMMARY_EMAIL_AUTOSEND:
        # Same dark-rollout rule as the recorded path: checked BEFORE
        # generating, so a switched-off feature costs no tokens per pass.
        outcome.status = STATUS_DISABLED
        return outcome

    sender_mailbox = _sender_mailbox(db, meeting, None, recipients)

    from rubik_core.tools.email_sender import EmailMessageSpec, send_email

    summary = generate_digest_summary(data_block=digest.data_block)
    when = _when_label(session)
    subject = f"{meeting.name} — recap for {when}"
    company = company_name_for_documents(get_company_profile(db))
    result = send_email(
        EmailMessageSpec(
            to=recipients,
            subject=subject,
            text_body=render_text(
                meeting_name=meeting.name or "L10",
                when=when,
                summary=summary,
                recording_title=None,
                company_name=company,
            ),
            html_body=render_html(
                meeting_name=meeting.name or "L10",
                when=when,
                summary=summary,
                recording_title=None,
                company_name=company,
            ),
        ),
        impersonate=sender_mailbox,
        autosend=settings.CUBIE_L10_SUMMARY_EMAIL_AUTOSEND,
    )
    outcome.message_id = result.message_id
    if result.status != "sent":
        outcome.status = STATUS_FAILED
        outcome.error = result.error or result.status
        return outcome

    # Stamp exactly as the recorded path does, minus the doc_id there is no
    # recording for. This is what makes the send once-only.
    session.summary_email_sent_at = datetime.utcnow()
    session.summary_email_recipients = recipients
    db.commit()
    outcome.status = STATUS_SENT_DIGEST_ONLY
    return outcome


def _when_label(session) -> str:
    """ "Wednesday, August 20" — the day the meeting HAPPENED, locally.

    Rendered in the organization's timezone, not UTC: an L10 that runs at 5pm
    Pacific is already "tomorrow" in UTC, and a recap headed with the wrong day
    is the kind of small wrongness that makes people distrust the whole email.
    """
    from datetime import timezone as _tz

    start = naive_utc(session.actual_start)
    if start is None:
        return "this week"
    try:
        from rubik_core.priorities.temporal import resolve_tz

        tz, _ = resolve_tz(None)
        local = start.replace(tzinfo=_tz.utc).astimezone(tz)
    except Exception:  # noqa: BLE001 — a tz lookup must never lose the email
        local = start
    # No %-d / %#d: the no-pad day directive is platform-specific.
    return f"{local:%A, %B} {local.day}"
