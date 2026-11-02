"""Deliver AI Priorities + manager digest to Slack (as the Cubie bot).

Separate from the overnight *generation* run (02:00 UTC) — a distinct Cloud
Scheduler job fires this at the team's local morning so DMs land when people start
their day, not when the recs are computed.

Priority DMs: each recipient gets the **exact same AI Priorities Cubie
shows them** for today — the judged HomeCard bundle (``HomeCard.metadata_json``,
written by the priorities orchestrator), read through the same accessor the web
panel uses (``home._load_today_priorities``), so the DM's titles, order and set
match the app 1:1. That parity is the whole point: the DM is a faithful teaser
that pulls people into Cubie. Each priority carries its source deep-link where
one exists, plus an "Open Cubie" button. Fresh daily snapshot (the job runs
once/day). Neither DM reads the mechanical ``agent_recommendations`` table — that
path produced duplicated "Contact priority: <company>" cards that never matched
the app, and the Overnight Sales Agent that wrote it is now retired (the manager
digest below reads the same judged bundle).

Recipients = users in an **opted-in department**
(``departments.slack_priorities_enabled``) OR on the **Exec team** (``User.is_exec``,
any department). The per-dept opt-in is the rollout control.

Two priority DMs exist, one per report kind, and ``report`` selects which:

* ``report="daily"`` (weekday morning job) — today's briefing. Look-ahead items
  the read path merges into the web pane are excluded here; they belong to the
  DM below.
* ``report="next_week"`` (Friday afternoon job, after ``priorities-next-week``
  generation) — ONLY the next-week look-ahead, read straight off the
  ``next_week_report`` card via ``home.load_next_week_priorities``, under a
  header naming the target week.

That split is load-bearing. Both jobs used to call this module with no notion of
which report they were delivering, so the Friday afternoon DM rendered the
*merged* bundle: the morning's daily items first, look-ahead items appended, then
sliced to ``top_n``. Daily generation caps at 5 and ``top_n`` is 5, so a normal
user's second DM was a verbatim re-send of the first with the look-ahead
collapsed into "+N more in Cubie": an afternoon DM that repeats every morning
item in order. Only a user with an empty daily
card (e.g. on PTO) ever saw a clean look-ahead. Reading the look-ahead card
directly also skips ``_merge_next_week``'s signal-id dedup, which had been
dropping exactly the Monday/Tuesday items Friday's 72h daily lookahead covers —
the most useful half of the report.

Manager digest: for each opted-in department, the **EOS head of department**
— the manager of that department's L10 meeting (``eos_l10_meetings.manager_id`` →
``eos_people.email``) — is DM'd a digest of their team's **active AI Priorities for
today** (aggregated from each rep's judged HomeCard bundle), high-urgency first.

User-facing name is ``rubik_core.brand.PRODUCT_NAME``; every DM reads it rather
than repeating a literal. Degrades gracefully: no workspace bot token → logs and
returns, never raises into the scheduler.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.services.directory_status import deactivated_priorities_emails
from backend.services.slack_service import SlackAPIError, SlackBotClient
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.eos.l10 import L10Meeting
from rubik_core.database.eos.people import Person
from rubik_core.database.models import Department, User
from rubik_core.priorities.temporal import REPORT_KIND_DAILY, REPORT_KIND_NEXT_WEEK

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 5

# The report kinds a delivery run can carry, re-exported from the pipeline's
# canonical pair so the scheduler body, the generation job and the DM all speak
# one vocabulary.
DELIVERY_REPORTS = (REPORT_KIND_DAILY, REPORT_KIND_NEXT_WEEK)

# Slack mrkdwn links only render for these URL schemes; anything else is shown as
# plain text (no link) so a weird value can't break the block.
_LINK_SCHEMES = ("http://", "https://", "slack://", "mailto:")
_SEVERITY_DOT = {"urgent": "🔴", "info": "🟡", "ok": "🟢"}
_URL_RE = re.compile(r"(?:https?://|slack://)\S+")

# Slack hard-caps a section's mrkdwn text at 3000 chars; one block over the limit
# makes chat.postMessage reject the ENTIRE message with `invalid_blocks` (the
# user then silently gets nothing — the text fallback isn't posted either). Cap
# below 3000 with headroom for the suffix.
_SECTION_TEXT_LIMIT = 2900


def _truncate_mrkdwn(text: str, limit: int = _SECTION_TEXT_LIMIT) -> str:
    """Keep a section's mrkdwn under Slack's 3000-char cap. Snaps to a line
    boundary when that costs little, and never leaves a half-open ``<url|label>``
    link (a dangling ``<`` is itself invalid Block Kit)."""
    if len(text) <= limit:
        return text
    suffix = f"\n…  _(more in {PRODUCT_NAME})_"
    cut = text[: limit - len(suffix)]
    nl = cut.rfind("\n")
    if nl >= len(cut) * 0.6:  # snap to a clean line break when one is close to the end
        cut = cut[:nl]
    # If we cut mid-link, drop the open bracket so no dangling `<` is left behind.
    if cut.count("<") > cut.count(">"):
        cut = cut[: cut.rfind("<")].rstrip()
    return cut.rstrip() + suffix


def _link_label(url: str) -> str:
    """A short, human label for a link target so the DM shows e.g. 'Gmail'
    instead of a raw URL."""
    u = url.lower()
    if "mail.google.com" in u:
        return "Gmail"
    if "calendar.google" in u or "google.com/calendar" in u:
        return "Calendar"
    if u.startswith("slack://") or "slack.com" in u:
        return "Slack"
    if "hubspot.com" in u:
        return "HubSpot"
    return "More info"


def _render_body(body: str, item_link: str | None) -> str:
    """Render a priority's body as Slack mrkdwn: convert GitHub-markdown bold
    (`**x**` → Slack `*x*`), drop the redundant 'Sources:' metadata line, and
    replace every raw URL in place with a tidy labeled link (e.g. `<url|Gmail>`)
    so the DM never shows a full URL. ``item_link`` (the PriorityItem's deep link,
    not in the body) is appended as a 🔗 link when present."""

    def _label_url(m: re.Match) -> str:
        u = m.group(0).rstrip(".,;)]")
        return f"<{u}|{_link_label(u)}>"

    lines: list[str] = []
    for raw in (body or "").splitlines():
        line = raw.rstrip().replace("**", "*")  # GFM bold → Slack bold
        bare = line.lstrip("-*• \t").strip()
        # drop the 'Sources:' metadata line (tolerant of _italic_/*bold* markers)
        if (
            bare.replace("_", "")
            .replace("*", "")
            .strip()
            .lower()
            .startswith("sources:")
        ):
            continue
        line = _URL_RE.sub(_label_url, line)  # raw URLs → labeled links, in place
        if line.strip():
            lines.append(line)
    text = "\n".join(lines).strip()
    if item_link and item_link.startswith(_LINK_SCHEMES) and item_link not in text:
        link_md = f"🔗 <{item_link}|{_link_label(item_link)}>"
        text = f"{text}\n{link_md}" if text else link_md
    return text


def _enabled_departments(db: Session) -> set[str]:
    """Department names opted into Slack delivery (`slack_priorities_enabled=true`).

    Per-department opt-in is the rollout control (turn departments on as they
    go live). Architecture scales to all departments by flipping each one's
    flag — no code change."""
    rows = (
        db.query(Department.name)
        .filter(Department.slack_priorities_enabled.is_(True))
        .all()
    )
    return {r[0] for r in rows}


def _department_members(db: Session, dept: str) -> list[str]:
    """Emails of the people in ``dept``, matched by slug, never a raw compare."""
    from rubik_core.rag.config import slugify_department

    want = slugify_department(dept)
    if not want:
        return []
    return [
        u.email
        for u in db.query(User)
        .filter(User.is_platform_support.is_(False))
        .order_by(User.email)
        .all()
        if u.email and slugify_department(u.department or "") == want
    ]


# ─── Priority DMs ─────────────────────────────────────────────────────


@dataclass
class SlackDeliveryResult:
    users_delivered: int = 0
    recs_delivered: int = 0
    users_skipped_no_slack: int = 0
    # Which report this run delivered ("daily" | "next_week"). Carried on the
    # result so the ops alert can name the right job and recovery recipe — a
    # missing next-week card is not a missing morning briefing.
    report: str = REPORT_KIND_DAILY
    # Recipients whose bundle lookup returned NOTHING for their local today.
    # Named, not just counted: a silent skip here is how a person goes without
    # a briefing (an exec generation gap, a timezone date-keying bug) and nobody
    # notices — the ops alert below names them so it can never be silent.
    # This stays the UNION of the two sub-buckets below (missing card + present
    # but empty) for back-compat; the alert classifies via the sub-buckets.
    users_skipped_empty: list[str] = field(default_factory=list)
    # Sub-buckets that split "skipped empty" by ROOT CAUSE so the ops alert stops
    # conflating them:
    #   * users_missing_card — NO HomeCard exists for their local today. A real
    #     generation gap / killed batch / timezone date-key mismatch → alertable.
    #   * users_empty_no_signals — a card EXISTS but is legitimately empty
    #     (generation_status=no_signals/filtered_empty). A genuinely quiet day,
    #     NOT a failure — it must NOT trip the "no bundle / timezone bug" alert.
    #   * users_silenced_by_filter — a card exists with items, but the read
    #     path hid every one AND at least one hiding row already existed when
    #     the card was built (or nothing could account for the drop). Generation
    #     should have honoured that row and didn't, so it repeats daily until
    #     the row is reopened → alertable. ``silenced_rows`` names the rows.
    #   * users_cleared_since_briefing — a card with items, all of them hidden
    #     by rows written AFTER the card was built: the person (or Cubie on
    #     their behalf: a meeting prep run, a reply drafted, an EOS item done)
    #     handled everything today. Success, not a fault → FYI only. This is
    #     what a second same-day run sees (the Friday afternoon next-week job
    #     re-reads a morning card the person has already cleared; without this
    #     bucket the alert blamed stale memory rows).
    #   * users_no_slack_account — no card AND the address is not a workspace
    #     member, so no DM is possible for it at all. Roster hygiene, not an
    #     outage → reported for context, never alarmed on.
    users_missing_card: list[str] = field(default_factory=list)
    users_empty_no_signals: list[str] = field(default_factory=list)
    users_silenced_by_filter: list[str] = field(default_factory=list)
    # email → the hiding rows that predate the card (home.ReadFilterDrop).
    # Absent for a silenced user when nothing on record accounts for the drop
    # (or the card is the look-ahead, whose drops are not itemised).
    silenced_rows: dict[str, list] = field(default_factory=dict)
    users_cleared_since_briefing: list[str] = field(default_factory=list)
    # Recipients a scheduled DAILY run skipped because they already had a DM
    # on their local today. Only a second daily run produces these: a Friday
    # afternoon job posting no body (→ report=daily) instead of
    # {"report":"next_week"} sends everyone the morning briefing twice and the
    # look-ahead never goes out. Alertable, so a misconfigured job shows up the
    # first time it fires.
    users_already_delivered: list[str] = field(default_factory=list)
    # Recipients with no card whose address isn't in the Slack workspace at all.
    # Split out of users_missing_card because diagnosing them as a generation gap is
    # wrong and unactionable: a stale/typo'd roster row (a misspelt address beside the
    # person's real one) can never receive a DM no matter how many times the batch
    # reruns, yet it fired the "generation gap — check the batch logs and HomeCard
    # dates" alert every single morning for weeks. The fix is to remove the row (or add
    # the person to Slack), so this is surfaced as context and must NEVER trip the alarm
    # on its own — the third de-noising of this alert, after the offboarded-employee and
    # filtered-empty cases.
    users_no_slack_account: list[str] = field(default_factory=list)
    # Recipients who are on PTO today: they get a friendly "enjoy your time off"
    # note (or nothing), and are deliberately kept OUT of users_skipped_empty so
    # a day off never trips the empty-bundle ops alarm.
    users_on_pto: list[str] = field(default_factory=list)
    # Recipients dropped because they are DEACTIVATED / offboarded (Slack
    # `deleted` / Google `suspended`). Distinct from PTO: no note is sent, they
    # are simply excluded, and they never count toward any empty-bundle alarm —
    # an ex-employee is expected to have nothing.
    users_deactivated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    delivered: bool = True  # False when no bot token is available
    # True when NO recipient had a HomeCard for today — a strong signal that the
    # generation batch did not run/complete before delivery fired. Delivery is
    # skipped in that case so a generation outage never masquerades as "everyone
    # had a quiet day" (see #6 — generation→delivery ordering is not enforced by
    # the schedule, only by ~1.5h of slack).
    generation_missing: bool = False


# Urgency (judged bundle) → the severity buckets _priority_blocks renders with a
# coloured dot. Mirrors the app's HIGH / MEDIUM / LOW badges.
_URGENCY_TO_SEVERITY = {"high": "urgent", "medium": "info", "low": "ok"}


@dataclass
class _BundlePriority:
    """Adapts one judged-bundle priority (the ``home._load_today_priorities``
    shape) to the attribute interface ``_priority_blocks`` reads. Rendering the
    DM off the same bundle the web panel uses is what makes the two match."""

    title: str
    severity: str
    body: str
    link: str | None
    # The app shows a HIGH/LOW badge, not a percentage; surfacing no confidence
    # chip here also drops the misleading "100%" the old recs path showed.
    confidence: float | None = None
    next_action: str | None = None
    personal_gmail: bool = False
    private_gmail_context: bool = False


def _bundle_priority_to_item(p: dict) -> _BundlePriority:
    """Map one bundle priority dict → the DM item shape. Body = the app's
    ``why_it_matters`` followed by a ``*Next:*`` line (its ``suggested_action``),
    so the DM reads like the card in Cubie."""
    why = (p.get("why_it_matters") or "").strip()
    action = (p.get("suggested_action") or "").strip()
    body = "\n".join(
        part for part in (why, f"*Next:* {action}" if action else "") if part
    )
    attrs = p.get("attrs") if isinstance(p.get("attrs"), dict) else {}
    personal_gmail = attrs.get("personal_gmail") is True
    private_gmail_context = attrs.get("private_gmail_context") is True
    mailbox = attrs.get("gmail_mailbox")
    if personal_gmail and isinstance(mailbox, str) and mailbox:
        body = f"{body}\n*Mailbox:* {mailbox}"
    links = [
        link
        for link in (p.get("source_links") or [])
        if isinstance(link, str) and link.startswith(_LINK_SCHEMES)
    ]
    return _BundlePriority(
        title=(p.get("title") or "Priority").strip(),
        severity=_URGENCY_TO_SEVERITY.get((p.get("urgency") or "").lower(), "info"),
        body=body,
        link=links[0] if links else None,
        personal_gmail=personal_gmail,
        private_gmail_context=private_gmail_context,
    )


def _load_bundle_priority_items(
    user_email: str, *, report: str = REPORT_KIND_DAILY, for_manager: bool = False
) -> list[_BundlePriority]:
    """The judged AI-Priorities items to DM ``user_email`` for ``report``.

    ``report="daily"`` reads today's bundle — the SAME set the Cubie web panel
    renders. ``home._load_today_priorities`` keys on the user's LOCAL today and
    applies the UC-25 dismiss/snooze/resolve memory, so the DM honours what the
    user already handled. Look-ahead items that accessor merges in for the web
    pane are dropped here: they are the Friday DM's payload, and in the morning
    briefing they only pad (or duplicate) it.

    ``report="next_week"`` reads the look-ahead card on its own via
    ``home.load_next_week_priorities`` — never the merged daily bundle, which is
    what made the Friday afternoon DM a re-send of that morning's.

    Returns [] when no bundle exists (→ no DM sent; never a mechanical
    fallback). Lazy imports avoid a circular import at module load and keep the
    bundle order the app shows."""
    if report == REPORT_KIND_NEXT_WEEK:
        from backend.routers.home import load_next_week_priorities
        from rubik_core.priorities.privacy import is_private_gmail_bundle

        bundle = load_next_week_priorities(user_email)
        if not bundle:
            return []
        if for_manager and is_private_gmail_bundle(bundle):
            return []
        return [_bundle_priority_to_item(p) for p in (bundle.get("priorities") or [])]

    from backend.routers.home import _load_today_priorities
    from rubik_core.priorities.privacy import is_private_gmail_bundle

    bundle = _load_today_priorities(user_email)
    if not bundle:
        return []
    if for_manager and is_private_gmail_bundle(bundle):
        return []
    return [
        _bundle_priority_to_item(p)
        for p in (bundle.get("priorities") or [])
        if (p.get("origin") or "priority") != REPORT_KIND_NEXT_WEEK
    ]


def _next_week_window_for(user_email: str) -> tuple[str | None, str | None]:
    """``(week_start, week_end)`` of the user's visible look-ahead card.

    Feeds the DM header so the message names the week it covers. Best-effort:
    any failure yields ``(None, None)`` and the header falls back to
    window-free phrasing rather than costing the user their DM."""
    try:
        from backend.routers.home import load_next_week_priorities

        bundle = load_next_week_priorities(user_email) or {}
        return bundle.get("week_start"), bundle.get("week_end")
    except Exception:  # noqa: BLE001 — the header window is best-effort
        logger.warning(
            "[slack-deliver] next-week window lookup failed for %s",
            user_email,
            exc_info=True,
        )
        return None, None


def _bundle_pto_for(user_email: str) -> dict | None:
    """Today's PTO banner payload for ``user_email`` when they're off, else None.

    Read separately from :func:`_load_bundle_priority_items` (which returns only
    the priority items) so a user who is out today gets a friendly note instead
    of being counted as an empty-bundle failure. Best-effort: any error → None
    (treated as not-on-PTO), so a lookup hiccup can never break delivery."""
    try:
        from backend.routers.home import _load_today_priorities

        bundle = _load_today_priorities(user_email) or {}
        pto = bundle.get("pto")
        return pto if isinstance(pto, dict) and pto.get("on_pto") else None
    except Exception:  # noqa: BLE001 — PTO is best-effort
        logger.warning(
            "[slack-deliver] PTO lookup failed for %s", user_email, exc_info=True
        )
        return None


def _bundle_hygiene_for(user_email: str) -> dict | None:
    """Today's unanswered-invite note for ``user_email``, else None.

    Same best-effort contract as :func:`_bundle_pto_for` — any error yields None
    so a lookup hiccup can never cost the user their priorities DM.
    """
    try:
        from backend.routers.home import _load_today_priorities

        bundle = _load_today_priorities(user_email) or {}
        hygiene = bundle.get("calendar_hygiene")
        return hygiene if isinstance(hygiene, dict) else None
    except Exception:  # noqa: BLE001 — the note is best-effort
        logger.warning(
            "[slack-deliver] calendar-hygiene lookup failed for %s",
            user_email,
            exc_info=True,
        )
        return None


def _bundle_generated_at_for(user_email: str) -> str | None:
    """When today's bundle for ``user_email`` was COMPILED, as an ISO instant.

    Same best-effort contract as :func:`_bundle_hygiene_for` — any error yields
    None and the DM simply ships without a stamp.

    This is the pipeline's completion time (``PriorityBundle.generated_at``, set
    in the orchestrator), NOT the age of the underlying email/HubSpot/calendar
    data. Per-channel freshness is computed but never persisted, so the copy
    below deliberately says "compiled" and never "your email as of".
    """
    try:
        from backend.routers.home import _load_today_priorities

        bundle = _load_today_priorities(user_email) or {}
        stamp = bundle.get("generated_at")
        return stamp if isinstance(stamp, str) and stamp.strip() else None
    except Exception:  # noqa: BLE001 — the stamp is best-effort
        logger.warning(
            "[slack-deliver] generated_at lookup failed for %s",
            user_email,
            exc_info=True,
        )
        return None


def _compiled_at_line(generated_at: str | None, email: str | None = None) -> str | None:
    """The DM's freshness stamp, in the reader's own timezone, or None.

    Renders as: Compiled 6:04 AM EDT — open Cubie to refresh.

    A reader wants to know how old the list is before acting on it. Rendered
    in the RECIPIENT's timezone (one team's users can span several
    time zones), which is why this reuses :func:`resolve_user_now` — passing the
    bundle's instant as ``now_utc`` converts it with the same timezone
    resolution every other user-facing time in the pipeline uses.

    Returns None for a missing or unparseable stamp: no line is better than a
    wrong one.
    """
    if not generated_at:
        return None
    import datetime as _dt

    try:
        stamp = _dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    try:
        from rubik_core.priorities.temporal import resolve_user_now

        local = resolve_user_now(email, now_utc=stamp).now_local
    except Exception:  # noqa: BLE001 — never cost the user their DM over a stamp
        local = stamp.astimezone(_dt.timezone.utc)
    clock = local.strftime("%I:%M %p").lstrip("0")
    zone = local.strftime("%Z") or "UTC"
    return f"Compiled {clock} {zone} — open {PRODUCT_NAME} to refresh."


def _pto_dm_text(pto: dict) -> str:
    """A short, friendly Slack note for a user who is on PTO today.

    No work list — just an acknowledgement that Cubie knows they're off and
    will hold their priorities until they're back."""
    import datetime as _dt

    back = pto.get("return_date")
    back_phrase = ""
    if back:
        try:
            d = _dt.date.fromisoformat(back)
            back_phrase = (
                f" I'll have your priorities ready when you're back "
                f"{d.strftime('%A, %b')} {d.day}."
            )
        except ValueError:
            back_phrase = ""
    return (
        "You're on PTO today 🌴 — enjoy the time off. I won't send your daily "
        f"priorities while you're out.{back_phrase}"
    )


def _time_of_day_greeting(email: str | None = None) -> str:
    """Time-of-day salutation for the user's LOCAL hour at send time.

    "Good morning" / "Good afternoon" / "Good evening".

    The batch that sends these DMs can fire at any hour (a delayed or manually
    re-run job may land in the afternoon), so a hardcoded "Good morning" reads
    wrong. Best-effort: any timezone-lookup failure falls back to "Hello".
    """
    try:
        from rubik_core.priorities.temporal import resolve_user_now

        hour = resolve_user_now(email).now_local.hour
    except Exception:  # noqa: BLE001 — greeting must never break delivery
        return "Hello"
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def _next_week_header(week_start: str | None, week_end: str | None) -> str:
    """Header for the Friday look-ahead DM — it must never say "for today".

    Naming the target week is what keeps this DM distinguishable from the daily
    briefing sent the same morning; with the shared "your top priorities for
    today" header, recipients read the second DM as a duplicate even when it
    did carry look-ahead items. Falls back to window-free phrasing when the
    card's window is missing/unparseable."""
    import datetime as _dt

    try:
        start = _dt.date.fromisoformat(week_start or "")
        end = _dt.date.fromisoformat(week_end or "")
    except (TypeError, ValueError):
        return "The week ahead — your next-week look-ahead"
    return (
        f"The week ahead — {start.strftime('%a %b')} {start.day}"
        f" to {end.strftime('%a %b')} {end.day}"
    )


def _hygiene_line(hygiene: dict | None) -> str | None:
    """One-line "you haven't answered these invites" note, or None.

    Invites the user never RSVP'd to are deliberately excluded from the priority
    list — an unanswered invite is not a commitment, and prepping every one of
    them buries the meetings they did accept. This is how they still hear the
    meetings exist, in one line rather than a card each.
    """
    if not isinstance(hygiene, dict):
        return None
    count = hygiene.get("unanswered_count") or 0
    if count < 1:
        return None
    named = [
        (inv.get("title") or "").strip()
        for inv in (hygiene.get("invites") or [])
        if isinstance(inv, dict) and (inv.get("title") or "").strip()
    ]
    titles = ", ".join(named)
    if count > len(named):
        extra = count - len(named)
        titles = f"{titles} +{extra} more" if titles else f"{extra} more"
    subject = "invite" if count == 1 else "invites"
    tail = f": {titles}" if titles else ""
    return (
        f"🗓️ {count} upcoming {subject} you haven't answered{tail} — "
        "not in the list above. Accept or decline to keep this clean."
    )


def _priority_blocks(
    items: list,
    top_n: int,
    web_url: str,
    greeting: str = "Good morning",
    hygiene: dict | None = None,
    header: str | None = None,
    compiled_at: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Render home-feed PriorityItems as a Slack DM.

    One section per priority — severity dot + title + per-item action deep link
    (when the PriorityItem carries one) + body — then a "+N more" line, the
    unanswered-invite note (``hygiene``, see :func:`_hygiene_line`), the
    "compiled at" stamp (``compiled_at``, see :func:`_compiled_at_line`) and an
    "Open Cubie" button to the web app. ``web_url`` is FRONTEND_URL. ``greeting``
    is the time-of-day salutation (see :func:`_time_of_day_greeting`).

    ``header`` overrides the daily "your top priorities for today" line — the
    next-week run passes :func:`_next_week_header` so its DM announces the week
    it covers instead of presenting look-ahead items as today's work.
    """
    shown = items[:top_n]
    home = (web_url or "").rstrip("/")
    header = header or f"{greeting} — your top priorities for today"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}}
    ]
    fallback_lines = [f"{header}:"]
    for p in shown:
        dot = _SEVERITY_DOT.get(getattr(p, "severity", None), "•")
        title = (getattr(p, "title", None) or "Priority").strip()
        pct = (
            f"  `{round(p.confidence * 100)}%`"
            if getattr(p, "confidence", None)
            else ""
        )
        text = f"{dot} *{title}*{pct}"
        prose = getattr(p, "next_action", None) or getattr(p, "body", None) or ""
        rendered = _render_body(prose, getattr(p, "link", None))
        if rendered:
            text += f"\n{rendered}"
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _truncate_mrkdwn(text)},
            }
        )
        fallback_lines.append(f"{dot} {title}")

    extra = len(items) - len(shown)
    if extra > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"+{extra} more in {PRODUCT_NAME}"}
                ],
            }
        )
        fallback_lines.append(f"+{extra} more in {PRODUCT_NAME}")
    hygiene_line = _hygiene_line(hygiene)
    if hygiene_line:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": _truncate_mrkdwn(hygiene_line)}
                ],
            }
        )
        fallback_lines.append(hygiene_line)
    compiled_line = _truncate_mrkdwn(compiled_at) if compiled_at else None
    if compiled_line:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": compiled_line}]}
        )
        fallback_lines.append(compiled_line)
    if home.startswith("https://"):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": f"Open {PRODUCT_NAME}"},
                        "url": f"{home}/",
                    }
                ],
            }
        )
    # A genuinely useful plain-text fallback: it's both the notification preview
    # AND the message body if we ever have to degrade to text-only on invalid_blocks.
    fallback = "\n".join(fallback_lines)
    return fallback, blocks


def _today_card_present(email: str) -> bool:
    """True when ``email`` has a judged HomeCard for today.

    Distinguishes "generation ran and this rep genuinely had nothing" (a card
    exists but is empty) from "generation never produced a card for this rep"
    (no card at all). ``_load_today_priorities`` returns the bundle dict or
    ``None``; ``None`` means no card exists for today.
    """
    from backend.routers.home import _load_today_priorities

    try:
        return _load_today_priorities(email) is not None
    except Exception:  # a lookup hiccup must not be read as "generation ran"
        return False


def _today_bundle_status(email: str) -> tuple[bool, str | None, int]:
    """``(card_present, generation_status, stored_count)`` for ``email``'s local today.

    Used to CLASSIFY an empty delivery into three distinct causes:

    * ``card_present=False`` — no HomeCard exists at all (a real generation gap
      → alertable).
    * ``card_present=True, stored_count == 0`` — the card was written empty: a
      legitimate quiet day (``generation_status`` is ``no_signals`` /
      ``filtered_empty`` / ``on_pto``). NOT a failure.
    * ``card_present=True, stored_count > 0`` — the card HAS items but none
      survived the read-path UC-25 filter. Generation succeeded and delivery
      still had nothing to send, which is a real (and daily-recurring) fault.

    ``stored_count`` is the PRE-filter count (``generated_count``, projected
    straight from ``metadata_json``) while ``bundle["priorities"]`` is the
    POST-filter list — comparing the two is what separates the last two cases.
    Deliberately not inferred from ``generation_status`` strings: the status is
    written by generation and says nothing about what the read path later
    removed. A lookup hiccup is reported as "missing" so we err toward
    surfacing a possible gap rather than hiding it.
    """
    from backend.routers.home import _load_today_priorities

    try:
        bundle = _load_today_priorities(email)
    except Exception:
        return False, None, 0
    if bundle is None or not isinstance(bundle, dict):
        return False, None, 0
    status = bundle.get("generation_status")
    try:
        stored = int(bundle.get("generated_count") or 0)
    except (TypeError, ValueError):
        stored = 0
    return True, (str(status) if status else None), stored


def _rows_predating_card(email: str) -> list | None:
    """The read-filter drops on ``email``'s card that were ALREADY there when
    the card was built (``home.ReadFilterDrop``), for a card that emptied.

    * ``[]`` — every item was hidden by a row (or a deal defer) written AFTER
      the card was built: the person handled their list today. Not a fault.
    * a non-empty list — those rows existed when generation ran, so generation
      should have left the items out and didn't. Repeats daily → alert.
    * ``None`` — nothing accounts for the drop (no card, nothing hidden, or the
      lookup failed). Reported as unexplained, never as handled: "we couldn't
      tell" must not quietly become "they cleared it".

    A drop with no timestamp, or a card that never recorded when it was built,
    counts as predating for the same reason.
    """
    from backend.routers.home import today_read_filter_drops

    try:
        explained = today_read_filter_drops(email)
    except Exception:  # noqa: BLE001 — the alert must still go out
        logger.warning(
            "[slack-deliver] could not explain the read-filter drop for %s",
            email,
            exc_info=True,
        )
        return None
    if not explained or not explained[1]:
        return None
    built_at, drops = explained
    return [
        d
        for d in drops
        if built_at is None or d.changed_at is None or d.changed_at < built_at
    ]


def _already_delivered_today(emails: list[str]) -> set[str]:
    """Which of ``emails`` already got a priorities DM on their local today.

    Read from the ``delivered_slack`` rows :func:`_record_delivery` writes once
    Slack has accepted a DM. Lower-cased. Fails OPEN to an empty set: a
    duplicate DM is an annoyance, while a briefing withheld because this check
    broke is the silent failure the ops alert exists to prevent. Inert when
    Insights is switched off (``INSIGHTS_ENABLED``), since no rows are written.
    """
    wanted = sorted({e.strip().lower() for e in emails if e and e.strip()})
    if not wanted:
        return set()
    try:
        from datetime import datetime, timedelta, timezone

        from rubik_core.database.db import SessionLocal
        from rubik_core.database.insights import PriorityEvent
        from rubik_core.priorities.temporal import resolve_user_now

        # Wide enough to hold any timezone's "today"; the local-date compare
        # below does the real work.
        since = datetime.utcnow() - timedelta(hours=36)
        with SessionLocal() as session:
            rows = (
                session.query(PriorityEvent.user_email, PriorityEvent.at)
                .filter(
                    PriorityEvent.kind == "delivered_slack",
                    PriorityEvent.at >= since,
                    PriorityEvent.user_email.in_(wanted),
                )
                .all()
            )
        sent: set[str] = set()
        now_by_user: dict[str, Any] = {}
        for user_email, at in rows:
            key = (user_email or "").lower()
            if not key or at is None or key in sent:
                continue
            if key not in now_by_user:
                now_by_user[key] = resolve_user_now(key).now_local
            now_local = now_by_user[key]
            at_utc = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            if at_utc.astimezone(now_local.tzinfo).date() == now_local.date():
                sent.add(key)
        return sent
    except Exception:  # noqa: BLE001 — fail open: deliver rather than withhold
        logger.warning(
            "[slack-deliver] already-delivered check failed; delivering to all",
            exc_info=True,
        )
        return set()


def _next_week_card_present(email: str) -> bool:
    """True when ``email`` has a currently-visible next-week look-ahead card.

    The next-week counterpart of :func:`_today_card_present`, used by the
    generation→delivery guard: the Friday DM must abort loudly when the
    ``priorities-next-week`` job hasn't landed yet, not fall through to the
    daily card (which is exactly how it came to re-send the morning briefing).
    """
    from backend.routers.home import load_next_week_priorities

    try:
        return load_next_week_priorities(email) is not None
    except Exception:  # a lookup hiccup must not be read as "generation ran"
        return False


def _not_workspace_members(bot: SlackBotClient, emails: list[str]) -> set[str]:
    """Which of ``emails`` are NOT members of the Slack workspace.

    Used only to explain a delivery skip that already happened — an address with
    no Slack identity cannot be DM'd at all, so calling it a generation gap sends
    ops to the batch logs for something no rerun can fix.

    Deliberately conservative, because the underlying ``users.list`` sweep is
    best-effort and may come back partial or empty: an empty sweep yields an
    empty set ("unknown"), so the caller keeps its unqualified wording rather
    than mislabelling real people as phantoms. Never used to drop a recipient.
    """
    if not emails:
        return set()
    try:
        members = bot.member_emails()
    except Exception:  # noqa: BLE001 — classification must never break delivery
        logger.warning("[slack-deliver] workspace member sweep failed", exc_info=True)
        return set()
    if not members:
        # No roster came back — absence proves nothing. Stay silent.
        return set()
    return {e for e in emails if e.strip().lower() not in members}


def _next_week_bundle_status(email: str) -> tuple[bool, str | None, int]:
    """``(card_present, generation_status, stored_count)`` for the look-ahead card.

    Same three-way classification as :func:`_today_bundle_status` — a genuine
    generation gap, a legitimately quiet week, or a card whose items were all
    removed by the read-path UC-25 filter — read off the ``next_week_report``
    card instead of today's briefing.
    """
    from backend.routers.home import load_next_week_priorities

    try:
        bundle = load_next_week_priorities(email)
    except Exception:
        return False, None, 0
    if bundle is None or not isinstance(bundle, dict):
        return False, None, 0
    status = bundle.get("generation_status")
    try:
        stored = int(bundle.get("generated_count") or 0)
    except (TypeError, ValueError):
        stored = 0
    return True, (str(status) if status else None), stored


def _department_card_coverage(db: Session, dept: str) -> tuple[int, int]:
    """Return ``(members_with_card_today, total_members)`` for a department."""
    members = _department_members(db, dept)
    present = sum(1 for e in members if _today_card_present(e))
    return present, len(members)


def _record_delivery(email: str, profile: Any) -> None:
    """Note in the Insights card history that ``email``'s DM went out.

    One row per person per delivered DM, actor ``system``, written only after
    Slack accepted the message. Its own short session (never the caller's
    ``db``), and best effort: a telemetry failure must not stop the next
    person's DM.
    """
    try:
        from rubik_core.insights.priority_events import record_priority_event
        from rubik_core.rag.config import slugify_department

        record_priority_event(
            user_email=email,
            kind="delivered_slack",
            actor="system",
            department_slug=slugify_department(
                getattr(profile, "department", None) or ""
            )
            or None,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[slack-deliver] delivery not recorded for %s", email, exc_info=True
        )


def deliver_slack_priorities(
    db: Session,
    *,
    top_n: int = DEFAULT_TOP_N,
    emails: list[str] | None = None,
    report: str = REPORT_KIND_DAILY,
) -> SlackDeliveryResult:
    """DM each recipient the AI Priorities Cubie shows them — the same judged
    bundle the web panel renders (matching titles, order and set) — with
    per-item source links and an "Open Cubie" button.

    Recipients = users in an opted-in department (`slack_priorities_enabled`) OR on
    the Exec team (`User.is_exec`). A fresh snapshot per run — not deduped across
    runs. No-ops without a bot token.

    ``report`` picks which report to deliver. ``"daily"`` (default, the weekday
    morning job) sends today's briefing, excluding the look-ahead items the read
    path merges into the web pane. ``"next_week"`` (the Friday job that follows
    ``priorities-next-week`` generation) sends ONLY the look-ahead, under a header
    naming the target week; today-only extras — the PTO note and the
    unanswered-invite line — are skipped there, since neither says anything about
    next week. An unrecognised value falls back to ``"daily"`` rather than
    delivering nothing.

    ``emails`` overrides recipient selection with an explicit list — a targeted
    re-send (e.g. recovering users a prior run failed to deliver to). It bypasses
    the dept/exec gate since the caller named the recipients; the per-user guards
    (profile exists, has priorities, in Slack) still apply.
    """
    if report not in DELIVERY_REPORTS:
        logger.warning(
            "[slack-deliver] unknown report=%r — delivering the daily briefing",
            report,
        )
        report = REPORT_KIND_DAILY
    is_next_week = report == REPORT_KIND_NEXT_WEEK
    result = SlackDeliveryResult(report=report)

    bot = SlackBotClient()
    if not bot.is_available:
        logger.warning(
            "[slack-deliver] no workspace bot token — skipping. Install the %s "
            "Slack app to the workspace first. No DMs sent.",
            PRODUCT_NAME,
        )
        result.delivered = False
        return result

    from rubik_core.priorities.batch import (
        is_eligible_priorities_email,
        is_pipeline_fixture_email,
        is_slack_priorities_recipient,
        slack_opted_department_slugs,
    )

    if emails:
        # Targeted re-send: deliver to exactly these users, bypassing the
        # dept/exec gate. Still drop seed / admin-impersonation fixtures — there
        # is no real mailbox behind boss@example.com or
        # impersonated_user@sales.local, so a DM to one is only ever noise, never
        # an intended re-send.
        requested = list(dict.fromkeys(e.strip() for e in emails if e and e.strip()))
        recipients = [e for e in requested if not is_pipeline_fixture_email(e)]
        dropped = [e for e in requested if is_pipeline_fixture_email(e)]
        if dropped:
            logger.info(
                f"[slack-deliver] dropped {len(dropped)} fixture address(es) from "
                f"targeted run (not real mailboxes): {dropped}"
            )
        enabled: set[str] = set()
        logger.info(
            f"[slack-deliver] targeted run for {len(recipients)} user(s): {recipients}"
        )
    else:
        # Recipients = opted-in departments OR anyone on the Exec team (User.is_exec),
        # regardless of department. Generation already covers them
        # (``priorities_roster`` exempts exactly these people from its
        # never-signed-in filter); this gates delivery. Departments match by
        # slug on both sides, so an opt-in for "Sales Ops" reaches "sales-ops".
        enabled = _enabled_departments(db)
        opted = slack_opted_department_slugs(db)
        # Same eligibility guard generation applies (fixture blocklist + Workspace
        # domain) so the delivery roster is a strict SUBSET of the generated
        # population. Without it, seed/impersonation fixtures (boss@example.com,
        # impersonated_user@sales.local, …) with is_exec or a real department tag
        # were enumerated here, never had a card, and surfaced in the "Got nothing"
        # ops alert every morning.
        from rubik_core.company.domain import workspace_domains

        domains = workspace_domains()
        recipients = [
            u.email
            for u in db.query(User)
            .filter(User.is_platform_support.is_(False))
            .order_by(User.email)
            .all()
            if u.email
            and is_eligible_priorities_email(u.email, domains)
            and is_slack_priorities_recipient(u, opted)
        ]
    # Offboarded-user gate: drop DEACTIVATED employees (Slack `deleted` / Google
    # `suspended`) before anything else. An ex-employee has no live feed, so
    # keeping them here only produced an empty bundle that surfaced in the "no
    # bundle for their local today" ops alert every morning. Distinct from
    # PTO — no note, just dropped. Reuses the delivery `bot` so the workspace
    # users.list sweep is fetched once. Fully fail-open: a detection outage
    # leaves the roster untouched.
    try:
        deactivated = deactivated_priorities_emails(recipients, bot=bot)
    except Exception:  # noqa: BLE001 — detection must never break delivery
        logger.warning("[slack-deliver] deactivation lookup failed", exc_info=True)
        deactivated = set()
    if deactivated:
        result.users_deactivated = sorted(
            e for e in recipients if e.strip().lower() in deactivated
        )
        recipients = [e for e in recipients if e.strip().lower() not in deactivated]
        logger.info(
            "[slack-deliver] excluded %d deactivated user(s): %s",
            len(result.users_deactivated),
            result.users_deactivated,
        )

    if not recipients:
        logger.info(
            "[slack-deliver] no opted-in departments or exec users; nothing to deliver."
        )
        return result

    # Generation→delivery guard (#6): the batch that writes the HomeCards and
    # this delivery job are independent Cloud Scheduler jobs; nothing enforces
    # that generation finished first. If NOT ONE recipient has a card for this
    # report, generation almost certainly failed or has not run — deliver nothing
    # and raise a loud, alertable error rather than silently sending zero DMs
    # (which is indistinguishable from "everyone had a quiet day"). Skipped for a
    # targeted re-send, where the caller has deliberately named the recipients.
    card_present = _next_week_card_present if is_next_week else _today_card_present
    if not emails and not any(card_present(e) for e in recipients):
        logger.error(
            "[slack-deliver] ABORTING: 0 of %d recipients have a %s HomeCard — "
            "the priorities generation batch did not run/complete before "
            "delivery fired. No DMs sent. Check the cubie-priorities-batch job.",
            len(recipients),
            report,
        )
        result.generation_missing = True
        return result

    # Read the judged HomeCard bundle — the SAME data the Cubie web panel renders
    # — so the DM's titles, order and set match the app exactly. Lazy import
    # avoids a circular import at module load.
    from rubik_core.auth.registry import UserRegistry

    # One briefing per person per day. A scheduled daily run skips anyone who
    # already got a DM on their local today, so a second daily job (or a
    # bodyless Friday look-ahead job) cannot re-send the morning briefing. A
    # targeted run (``emails``) is a deliberate re-send and is never skipped,
    # nor is the look-ahead, which is its own report.
    already = (
        set() if (emails or is_next_week) else _already_delivered_today(recipients)
    )

    web_url = settings.FRONTEND_URL or ""
    registry = UserRegistry()
    for email in recipients:
        if email.strip().lower() in already:
            result.users_already_delivered.append(email)
            continue
        profile = registry.get_user(email)
        if profile is None:
            result.errors.append(f"{email}: no user profile")
            continue
        try:
            items = _load_bundle_priority_items(email, report=report)
        except Exception as e:  # one user's failure must not break the batch
            logger.warning(f"[slack-deliver] priorities build failed for {email}: {e}")
            result.errors.append(f"{email}: priorities build failed")
            continue
        if not items:
            # Nothing to send. If the user is on PTO today, greet them instead of
            # counting them as an empty-bundle failure (which alerts ops). Only
            # for the daily run: being off THIS Friday says nothing about next
            # week, so a look-ahead run must not answer with a PTO note.
            pto_meta = None if is_next_week else _bundle_pto_for(email)
            if pto_meta and pto_meta.get("on_pto"):
                try:
                    bot.send_dm(email, text=_pto_dm_text(pto_meta))
                except SlackAPIError as e:
                    logger.info(
                        f"[slack-deliver] PTO note not sent to {email}: {e.error}"
                    )
                result.users_on_pto.append(email)
                continue
            # No PTO, no items → classify WHY so the ops alert distinguishes a
            # genuine generation gap (no card at all → alert) from a legitimate
            # quiet day (a card exists but is empty → do NOT cry "timezone bug").
            # users_skipped_empty stays the union for back-compat.
            present, status, stored = (
                _next_week_bundle_status(email)
                if is_next_week
                else _today_bundle_status(email)
            )
            result.users_skipped_empty.append(email)
            if not present:
                result.users_missing_card.append(email)
            elif stored > 0:
                # The card HAS items but the read path hid every one. WHEN the
                # hiding rows were written decides what that means: after the
                # card was built, the person handled their list today (FYI);
                # before it, generation and the read filter disagree about a
                # row, which repeats every morning until it is reopened (a
                # silent empty feed) — that one alerts.
                stale = (
                    None if is_next_week else _rows_predating_card(email)
                )  # the look-ahead card is not explained; it alerts as before
                if stale == []:
                    result.users_cleared_since_briefing.append(email)
                    continue
                logger.warning(
                    "[slack-deliver] %s: card has %d item(s) (status=%s) but 0 "
                    "survived the read filter, and %s — nothing to deliver.",
                    email,
                    stored,
                    status,
                    f"{len(stale)} hiding row(s) predate the card"
                    if stale
                    else "the drop could not be attributed",
                )
                result.users_silenced_by_filter.append(email)
                if stale is not None:
                    result.silenced_rows[email] = stale
            else:
                result.users_empty_no_signals.append(email)
            continue  # nothing to send → don't send an empty DM; alerted below

        if is_next_week:
            week_start, week_end = _next_week_window_for(email)
            header = _next_week_header(week_start, week_end)
        else:
            header = None  # → the daily "your top priorities for today" line
        fallback, blocks = _priority_blocks(
            items,
            top_n,
            web_url,
            greeting=_time_of_day_greeting(email),
            # Reported alongside real priorities only. A user whose whole plate
            # is unanswered invites gets no DM at all (unchanged) — turning
            # "please RSVP" into its own daily message would just be a new nag.
            # Skipped on the look-ahead run: the note is about TODAY's unanswered
            # invites, so it would only repeat the morning DM's tail.
            hygiene=None if is_next_week else _bundle_hygiene_for(email),
            header=header,
            # How stale the list is, in the reader's own timezone. Shown on the
            # look-ahead run too — a Friday DM about next week is exactly where
            # "when was this built?" matters most.
            compiled_at=_compiled_at_line(_bundle_generated_at_for(email), email),
        )
        try:
            sent = bot.send_dm(email, text=fallback, blocks=blocks)
        except SlackAPIError as e:
            if e.error == "invalid_blocks":
                # Block Kit rejected the rich message (truncation should prevent
                # this, but never drop the user silently): degrade to text-only so
                # they still get their priorities. The reason is logged in _api.
                logger.warning(
                    f"[slack-deliver] invalid_blocks for {email}; retrying text-only"
                )
                try:
                    sent = bot.send_dm(email, text=fallback)
                except SlackAPIError as e2:
                    logger.warning(
                        f"[slack-deliver] text-only retry failed for {email}: {e2.error}"
                    )
                    result.errors.append(f"{email}: {e2.error}")
                    continue
            else:
                logger.warning(f"[slack-deliver] send failed for {email}: {e.error}")
                result.errors.append(f"{email}: {e.error}")
                continue
        if sent is None:
            result.users_skipped_no_slack += 1
            continue
        result.users_delivered += 1
        result.recs_delivered += min(len(items), top_n)
        _record_delivery(email, profile)

    logger.info(
        f"[slack-deliver] report={report} depts={sorted(enabled)}+exec "
        f"users_delivered={result.users_delivered} items={result.recs_delivered} "
        f"skipped_no_slack={result.users_skipped_no_slack} "
        f"missing_card={result.users_missing_card} "
        f"no_slack_account={result.users_no_slack_account} "
        f"empty_no_signals={result.users_empty_no_signals} "
        f"silenced={result.users_silenced_by_filter} "
        f"cleared_since_briefing={result.users_cleared_since_briefing} "
        f"already_delivered={len(result.users_already_delivered)} "
        f"on_pto={result.users_on_pto} deactivated={result.users_deactivated} "
        f"errors={result.errors}"
    )
    # Roster hygiene, resolved in ONE batched sweep rather than per user: a
    # missing-card address that isn't a workspace member can't be DM'd at all, so
    # blaming generation for it is wrong and no rerun will ever fix it. Move those
    # out of the alertable bucket (they stay in the users_skipped_empty union) so
    # a stale row can't keep paging ops as if it were an outage.
    phantoms = _not_workspace_members(bot, result.users_missing_card)
    if phantoms:
        result.users_no_slack_account = sorted(phantoms)
        result.users_missing_card = [
            e for e in result.users_missing_card if e not in phantoms
        ]
        logger.warning(
            "[slack-deliver] %d roster address(es) are not Slack workspace members "
            "and have no card — no DM is possible for them: %s. Remove the row in "
            "Admin → Users (DELETE /api/users/<email>) if it isn't a person, or "
            "add them to the Slack workspace.",
            len(result.users_no_slack_account),
            result.users_no_slack_account,
        )
    _alert_ops_delivery_failures(db, bot, result)
    return result


def _ops_recipients(db: Session) -> list[str]:
    """Who gets the delivery-failure alert: the same ops audience the Ingestion
    Council reports to (configured recipients, else the configured department,
    else the workspace's admins). Lazy import
    keeps council code off this module's import path."""
    from backend.agents.ingestion_council import _recipient_emails

    return _recipient_emails(db, None)


def _alert_ops_delivery_failures(
    db: Session, bot: SlackBotClient, result: SlackDeliveryResult
) -> None:
    """DM the ops team when a delivery run hit a REAL problem.

    ``result.report`` decides the wording: a missing ``next_week_report`` card
    points at the Friday ``priorities-next-week`` job, not at the morning batch,
    so the alert must not send ops chasing the wrong one.

    Fires only for an actionable failure:

    * ``users_missing_card`` — NO HomeCard for this report (a generation
      gap, a killed batch, or a timezone date-key mismatch).
    * ``users_silenced_by_filter`` — a card WITH items where the read path hid
      every one and at least one hiding row predates the card (or nothing
      accounts for the drop). Without it, generation reports ``ok``, so this
      looks identical to a quiet day while a user's feed sits empty for days.
      The alert names the rows, so nobody has to query for them.
    * ``users_already_delivered`` — a scheduled daily run found people who
      already had today's DM: a second daily job is firing, and without this
      the duplicates go unnoticed because nothing says so.
    * a send error.

    A card emptied by rows written AFTER it was built (``users_cleared_since_briefing``)
    is the person having handled their list, so it is context only, never the
    stale-row alarm.

    It deliberately does NOT fire for a recipient whose card simply had nothing
    in it (``users_empty_no_signals``) — that's a legitimate quiet day, and
    treating it as a "no bundle / timezone bug" is exactly the false alarm an
    offboarded employee once caused. Nor for
    ``users_no_slack_account``: an address that isn't a workspace member can never
    be DM'd, so it is roster hygiene (reported as context, with the fix) rather
    than an incident to page on every morning.
    Deactivated users are dropped upstream and never reach here. Text-only on
    purpose: an alert about a delivery failure must not itself fail on Block Kit
    validation. Never raises into the delivery loop.
    """
    missing = result.users_missing_card
    silenced = result.users_silenced_by_filter
    repeated = result.users_already_delivered
    if not (missing or silenced or result.errors or repeated):
        return
    try:
        recipients = _ops_recipients(db)
        if not recipients:
            logger.warning(
                "[slack-deliver] delivery failures but no ops recipients (no "
                "admin, no configured recipient or department match)"
            )
            return
        is_next_week = result.report == REPORT_KIND_NEXT_WEEK
        label = "next-week look-ahead" if is_next_week else "morning priorities"
        lines = [
            f"⚠️ *{PRODUCT_NAME} {label} — delivery problems*",
            f"Delivered: {result.users_delivered} · "
            f"No card written: {len(missing)} · "
            f"Silenced by filter: {len(silenced)} · "
            f"Send errors: {len(result.errors)}"
            + (f" · Already sent today: {len(repeated)}" if repeated else ""),
        ]
        if silenced:
            shown_s = ", ".join(sorted(silenced)[:15])
            more_s = f" (+{len(silenced) - 15} more)" if len(silenced) > 15 else ""
            lines.append(
                f"*Card had items but ALL were hidden on read (nothing sent):* "
                f"{shown_s}{more_s}"
            )
            lines.append(
                "Cause: the rows below already existed when the card was built, "
                "so generation should have left those items out and didn't. "
                "Generation and the read filter disagree about them, and it "
                "repeats every morning until the row is reopened (the person's "
                "Settings → AI Personalization → Handled priorities, or "
                "priority_memory) or the disagreement is fixed:"
            )
            lines.extend(_silenced_row_lines(result))
        if repeated:
            lines.append(
                f"*A second daily run fired today, nothing re-sent:* "
                f"{len(repeated)} people already had today's briefing and were "
                "skipped. On a Friday afternoon this is the next-week "
                "Slack delivery scheduler job posting no body, which "
                'asks for the daily report: give it {"report":"next_week"}. '
                "Until then the look-ahead never reaches Slack."
            )
        if missing:
            shown = ", ".join(sorted(missing)[:15])
            more = f" (+{len(missing) - 15} more)" if len(missing) > 15 else ""
            if is_next_week:
                lines.append(
                    f"*No visible next_week_report card (generation gap):* "
                    f"{shown}{more}"
                )
                lines.append(
                    "Likely cause: the Friday priorities-next-week job didn't "
                    "write their card, or the card's week window has already "
                    "passed (it is hidden once the user's local date reaches the "
                    "target Monday). Check that job's logs and the card's "
                    "week_start/week_end."
                )
            else:
                lines.append(
                    f"*No HomeCard for their local today (generation gap):* "
                    f"{shown}{more}"
                )
                lines.append(
                    "Likely cause: the priorities batch didn't write their card, "
                    "or it's keyed to the wrong local date (user timezone unset → "
                    "org default). Check the cubie-priorities-batch job logs and "
                    "HomeCard dates."
                )
        if result.errors:
            lines.append("*Send errors:* " + "; ".join(result.errors[:10]))
        # Context-only (no alarm): a card existed but was empty, or users were
        # dropped as offboarded. Shown so ops can see the full picture without
        # these being blamed as failures.
        if result.users_empty_no_signals:
            quiet = result.users_empty_no_signals
            shown_q = ", ".join(sorted(quiet)[:10])
            more_q = f" (+{len(quiet) - 10} more)" if len(quiet) > 10 else ""
            period = "week" if is_next_week else "day"
            lines.append(
                f"_FYI — quiet {period}, card present but empty (not a failure): "
                f"{shown_q}{more_q}_"
            )
        if result.users_cleared_since_briefing:
            cleared = result.users_cleared_since_briefing
            shown_c = ", ".join(sorted(cleared)[:10])
            more_c = f" (+{len(cleared) - 10} more)" if len(cleared) > 10 else ""
            lines.append(
                "_FYI — handled everything since the briefing was built, nothing "
                f"left to send (not a failure): {shown_c}{more_c}_"
            )
        if result.users_no_slack_account:
            phantoms = result.users_no_slack_account
            shown_p = ", ".join(sorted(phantoms)[:10])
            more_p = f" (+{len(phantoms) - 10} more)" if len(phantoms) > 10 else ""
            lines.append(
                f"_FYI — roster hygiene, NOT a generation gap: {shown_p}{more_p} — "
                "no card, and not a Slack workspace member either, so no DM is "
                "possible and rerunning the batch will not help. Delete the row in "
                "Admin → Users if the address isn't a person, or add them to the "
                "Slack workspace._"
            )
        if result.users_deactivated:
            lines.append(
                f"_FYI — {len(result.users_deactivated)} deactivated user(s) "
                "skipped (offboarded): "
                f"{', '.join(sorted(result.users_deactivated)[:10])}_"
            )
        # A repeated run needs a job fix, not a regenerate + re-send.
        if missing or silenced or result.errors:
            lines.append(_recover_line(is_next_week))
        text = "\n".join(lines)
        for email in recipients:
            try:
                bot.send_dm(email, text=text)
            except SlackAPIError as e:
                logger.warning(
                    f"[slack-deliver] ops alert to {email} failed: {e.error}"
                )
    except Exception:  # noqa: BLE001 — alerting must never break delivery
        logger.exception("[slack-deliver] ops alert failed")


def _recover_line(is_next_week: bool) -> str:
    """The ops alert's recovery recipe for this report."""
    if is_next_week:
        return (
            "Recover: regenerate via POST /api/jobs/priorities/run-next-week "
            '{"target_email": "<user>"} then re-send via POST '
            "/api/agents/priorities/deliver-slack "
            '{"report": "next_week", "emails": [...]}.'
        )
    return (
        "Recover: regenerate via POST /api/jobs/priorities/run-all "
        '{"target_email": "<user>"} then re-send via POST '
        '/api/agents/priorities/deliver-slack {"emails": [...]}.'
    )


_MAX_SILENCED_ROW_LINES = 10


def _silenced_row_lines(result: SlackDeliveryResult) -> list[str]:
    """One alert line per hiding row that predates the card, per silenced user.

    Ids and timestamps only (``home.ReadFilterDrop``): a person's priority
    titles do not belong in an ops DM. A user with no rows on record is named
    as unexplained, so the alert never implies a cause it did not find.
    """
    lines: list[str] = []
    for email in sorted(result.users_silenced_by_filter):
        rows = result.silenced_rows.get(email)
        if not rows and result.report == REPORT_KIND_NEXT_WEEK:
            lines.append(
                f"• {email}: the look-ahead card's drops are not itemised. Check "
                "priority_memory and deal_defers against its priorities."
            )
            continue
        if not rows:
            lines.append(
                f"• {email}: nothing on record accounts for it (no hiding row or "
                "deal defer found). Check priority_memory, deal_defers and the "
                "card's audit_trail."
            )
            continue
        for d in rows:
            what = (
                "deferred deal"
                if d.cause == "deal_deferred"
                else f"{d.cause} {d.entity_kind}"
            )
            kind = f" ({d.priority_kind})" if d.priority_kind else ""
            when = (
                d.changed_at.strftime("%Y-%m-%d %H:%M UTC")
                if d.changed_at
                else "no recorded time"
            )
            who = f" by {d.changed_by}" if d.changed_by else ""
            lines.append(f"• {email}: {what} `{d.entity_id}`{kind}, {when}{who}")
    if len(lines) > _MAX_SILENCED_ROW_LINES:
        extra = len(lines) - _MAX_SILENCED_ROW_LINES
        lines = [*lines[:_MAX_SILENCED_ROW_LINES], f"• (+{extra} more)"]
    return lines


# ─── Manager digest + escalations ─────────────────────────────────────


@dataclass
class ManagerDigestResult:
    digests_sent: int = 0
    items_escalated: int = 0
    errors: list[str] = field(default_factory=list)
    delivered: bool = True  # False when no bot token is available
    # True when NO team member in any opted-in department had a HomeCard for
    # today — generation almost certainly did not run. The digest is skipped so
    # a partial/empty team view is never presented to a manager as complete.
    generation_missing: bool = False


def _department_head_email(db: Session, dept: str) -> str | None:
    """The EOS head of a department = the manager of its L10 meeting
    (`eos_l10_meetings.manager_id` → `eos_people.email`), matched on
    `L10Meeting.department_name` (FK to departments.name). Returns None when the
    dept has no L10 meeting or no manager assigned — the same identity EOS uses.
    Departments match by slug, as everywhere else."""
    from rubik_core.rag.config import slugify_department

    want = slugify_department(dept)
    if not want:
        return None
    rows = (
        db.query(Person.email, L10Meeting.department_name)
        .join(L10Meeting, L10Meeting.manager_id == Person.id)
        .order_by(L10Meeting.created_at.desc())
        .all()
    )
    for email, department_name in rows:
        if slugify_department(department_name or "") == want:
            return email
    return None


def _team_priorities_for_department(
    db: Session, dept: str
) -> list[tuple[str, _BundlePriority]]:
    """Every team member's ACTIVE AI Priorities for today, as (member, priority).

    Reads the SAME judged HomeCard bundle the app + the rep's own morning DM show
    (via ``home._load_today_priorities``), which already applies the UC-25
    dismiss/snooze/resolve memory — so "active" == "still surfaced to the rep
    today". This is what replaced the deprecated ``agent_recommendations`` feed:
    the manager digest is now backed by AI Priorities, leaving the Overnight
    Sales Agent with no remaining consumer.
    """
    members = _department_members(db, dept)
    out: list[tuple[str, _BundlePriority]] = []
    for email in members:
        try:
            items = _load_bundle_priority_items(email, for_manager=True)
        except Exception as e:  # one rep's bundle failing must not break the digest
            logger.warning(f"[slack-manager-digest] bundle failed for {email}: {e}")
            continue
        local = email.split("@")[0]
        # The model may have used personal mail without citing it. Keep the
        # entire mixed-context run in the owner's own DM, out of team digests.
        out.extend(
            (local, it)
            for it in items
            if not it.personal_gmail and not it.private_gmail_context
        )
    return out


def _build_digest_blocks(
    dept: str,
    team_items: list[tuple[str, _BundlePriority]],
    max_items: int,
    web_url: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Digest DM to the department head: a count + the team's active AI Priorities
    for today, high-urgency first with a coloured dot and the rep attributed."""
    high = sum(1 for _, p in team_items if p.severity == "urgent")
    header = f"{dept.title()} — your team's priorities today"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{len(team_items)} active across the team · {high} high",
                }
            ],
        },
    ]
    fallback = [f"{header}: {len(team_items)} active, {high} high"]
    # High-urgency first so the manager sees what matters at a glance.
    ordered = sorted(team_items, key=lambda t: t[1].severity != "urgent")
    for member, p in ordered[:max_items]:
        dot = _SEVERITY_DOT.get(p.severity, "•")
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _truncate_mrkdwn(f"{dot} *{p.title}*  ·  @{member}"),
                },
            }
        )
        fallback.append(f"{dot} {p.title} (@{member})")
    extra = len(team_items) - min(len(team_items), max_items)
    if extra > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"+{extra} more"}],
            }
        )
    home = (web_url or "").rstrip("/")
    if home.startswith("https://"):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": f"Open {PRODUCT_NAME}"},
                        "url": f"{home}/",
                    }
                ],
            }
        )
    return "\n".join(fallback), blocks


def deliver_manager_digest(db: Session, *, max_items: int = 12) -> ManagerDigestResult:
    """For each opted-in department, DM the EOS head of department a digest of the
    team's ACTIVE AI Priorities for today (high-urgency first).

    Backed by the judged HomeCard bundle (``home._load_today_priorities``) — the
    SAME data the app + the rep's own morning DM use — NOT the deprecated
    ``agent_recommendations`` table (the Overnight Sales Agent is retired). Gated
    by the same per-dept opt-in as priority delivery (``slack_priorities_enabled``);
    recipient = the L10 meeting's manager (the EOS head). No-ops without a bot
    token; a dept with no active priorities or no EOS head is skipped
    (the latter recorded as an error), never fatal.
    """
    result = ManagerDigestResult()
    bot = SlackBotClient()
    if not bot.is_available:
        logger.warning("[slack-manager-digest] no workspace bot token — skipping.")
        result.delivered = False
        return result

    enabled = _enabled_departments(db)
    if not enabled:
        logger.info("[slack-manager-digest] no departments opted in; nothing to do.")
        return result

    web_url = settings.FRONTEND_URL or ""

    # Generation→delivery guard (#6): if not a single team member across any
    # opted-in department has a HomeCard for today, generation did not run —
    # skip every digest so a manager is never shown an empty/partial team view
    # as if it were complete.
    coverage = {dept: _department_card_coverage(db, dept) for dept in sorted(enabled)}
    if not any(present for present, _total in coverage.values()):
        logger.error(
            "[slack-manager-digest] ABORTING: 0 team members across %s have a "
            "HomeCard for today — priorities generation did not run/complete. "
            "No digests sent.",
            sorted(enabled),
        )
        result.generation_missing = True
        return result

    for dept in sorted(enabled):
        present, total = coverage[dept]
        team_items = _team_priorities_for_department(db, dept)
        if not team_items:
            continue
        head_email = _department_head_email(db, dept)
        if not head_email:
            result.errors.append(
                f"{dept}: no EOS head (set the L10 meeting's manager) — skipped"
            )
            continue

        text, blocks = _build_digest_blocks(dept, team_items, max_items, web_url)
        # Partial-coverage banner: some reps have no card today, so this team
        # view is incomplete. Surface that instead of implying it is the whole
        # picture. (Insert after the header block.)
        if present < total:
            note = (
                f"⚠️ {total - present} of {total} reps have no priorities "
                "generated today — this view may be incomplete."
            )
            blocks.insert(
                1,
                {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]},
            )
            text = f"{note}\n{text}"
        try:
            sent = bot.send_dm(head_email, text=text, blocks=blocks)
        except SlackAPIError as e:
            if e.error == "invalid_blocks":
                try:
                    sent = bot.send_dm(head_email, text=text)  # degrade to text-only
                except SlackAPIError as e2:
                    result.errors.append(f"{dept} head {head_email}: {e2.error}")
                    continue
            else:
                logger.warning(
                    f"[slack-manager-digest] send failed for {dept} head {head_email}: {e.error}"
                )
                result.errors.append(f"{dept} head {head_email}: {e.error}")
                continue
        if sent is None:
            result.errors.append(f"{dept} head {head_email}: not in Slack workspace")
            continue
        result.digests_sent += 1

    logger.info(
        f"[slack-manager-digest] depts={sorted(enabled)} "
        f"digests_sent={result.digests_sent} errors={result.errors}"
    )
    return result
