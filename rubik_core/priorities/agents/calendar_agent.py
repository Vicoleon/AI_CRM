"""Calendar channel agent — UC-22 pure extractor.

Pulls upcoming events for a user and emits one ``ChannelSignal`` per
event with entity-tagged metadata (``entity_kind=EVENT``). External
attendee emails are also added as ``person`` references via the
``related_entities`` attr so the decision-maker can join them with
Gmail threads and HubSpot deals from the same domain.

No judgment, no urgency, no "flag". The decision-maker (UC-24) is
where importance happens.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from rubik_core.priorities.agents._people import person_label, resolve_display_names
from rubik_core.priorities.agents.base import BaseChannelAgent
from rubik_core.priorities.models import ChannelKind, ChannelSignal, EntityKind
from rubik_core.priorities.temporal import format_when_local

logger = logging.getLogger(__name__)


class CalendarChannelAgent(BaseChannelAgent):
    channel = ChannelKind.CALENDAR

    def fetch_signals(self) -> list[dict]:
        from rubik_core.priorities.agents._google_auth import require_live_client
        from rubik_core.tools.calendar import GoogleCalendarClient

        client = GoogleCalendarClient(user_email=self.user_email)
        # Deactivated / DWD-broken account → service=None → surface as a fetch
        # failure instead of a silent empty calendar (see _google_auth).
        require_live_client(client, channel="calendar", user_email=self.user_email)
        now = datetime.now(timezone.utc)
        now_z = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Ask Google for start times in the reader's tz so the raw ISO already
        # carries the right offset; we still format deterministically below.
        reader_tz = self.temporal.tz_name if self.temporal is not None else None
        # A long lookahead (the Friday next-week run spans ~10 days) can blow
        # past 20 upcoming events before it even reaches next Monday, so widen
        # the fetch window when the horizon is far out.
        lookahead = self.temporal.lookahead_hours if self.temporal else 0
        max_results = 40 if lookahead > 96 else 20
        return (
            client.get_upcoming_events(
                max_results=max_results, time_min=now_z, time_zone=reader_tz
            )
            or []
        )

    def parse_signals(self, raw_items: list) -> list[ChannelSignal]:
        from rubik_core.tools.calendar import (
            RSVP_DECLINED,
            is_non_meeting_event,
            is_placeholder_hold,
            self_response_status,
        )

        me = self.user_email.strip().lower()
        user_domain = me.split("@", 1)[1] if "@" in me else ""
        signals: list[ChannelSignal] = []

        # One DB round-trip for every attendee on the calendar, so each event
        # below can name its counterparties without N queries.
        name_by_email = resolve_display_names(
            (a.get("email") or "")
            for ev in raw_items
            for a in (ev.get("attendees") or [])
        )

        for ev in raw_items:
            # Skip entries that are never a meeting to prepare for. Focus
            # blocks / OOO / birthdays (Google `eventType`) and all-day
            # reminders that no one else is on (holidays, personal reminders)
            # were producing junk "Prepare for Focus time…" / "Prepare for
            # <all-day reminder>" priority cards.
            if is_non_meeting_event(ev):
                continue
            # Opaque self-holds ("Busy"/"Blocked"/"Focus"/…) are genuine busy
            # time but never a meeting to prep — skip so they don't produce a
            # junk "Prepare for your BLOCKED time…" card.
            if is_placeholder_hold(ev):
                continue
            # A meeting the reader DECLINED is a decision already made — it must
            # never reach the decision-maker at all, in any form. Unanswered
            # invites stay (tagged `rsvp` below) so the orchestrator can turn
            # them into one calendar-hygiene note instead of N prep cards.
            rsvp = self_response_status(ev, self.user_email)
            if rsvp == RSVP_DECLINED:
                continue

            start_info = ev.get("start", {}) or {}
            end_info = ev.get("end", {}) or {}
            start = start_info.get("dateTime") or start_info.get("date", "")
            end = end_info.get("dateTime") or end_info.get("date", "")
            all_day = "date" in start_info and "dateTime" not in start_info

            attendees = ev.get("attendees", []) or []
            attendee_emails = [
                (a.get("email") or "").lower() for a in attendees if a.get("email")
            ]
            # An all-day event with no other attendees is a reminder/holiday,
            # not a meeting — you don't "block 30 min before" it. All-day
            # events WITH attendees (e.g. an offsite) are kept.
            if all_day and not attendee_emails:
                continue
            external_attendees = [
                a
                for a in attendee_emails
                if "@" in a and a.split("@")[-1] != user_domain
            ]
            external_domains = sorted(
                {a.split("@")[-1] for a in external_attendees if "@" in a}
            )

            # WHO THE MEETING IS WITH — everyone on the invite except the reader
            # and any room/resource. Internal colleagues are included: for an
            # internal 1:1 they are the ONLY counterparty, and omitting them is
            # what forced the model to guess a name out of the title (and name
            # the reader back at themselves — "your 1:1 with Alex", sent to
            # Alex). Names come from our users table, so the briefing says
            # "Blair Ellison", not "bellison@acme.example".
            counterparties = [
                person_label(
                    (a.get("email") or "").lower(),
                    name_by_email,
                    google_display_name=a.get("displayName") or a.get("display_name"),
                )
                for a in attendees
                if (a.get("email") or "").lower()
                and (a.get("email") or "").lower() != me
                and not a.get("resource")
            ]

            event_id = ev.get("id") or ev.get("summary") or ""
            description = ev.get("description") or ""
            attachments = ev.get("attachments", []) or []
            conf_data = ev.get("conferenceData", {}) or {}
            conference_url = None
            for ep in conf_data.get("entryPoints", []) or []:
                if ep.get("entryPointType") == "video":
                    conference_url = ep.get("uri")
                    break

            # Pre-format the human clock time ONCE, in the reader's timezone.
            # No LLM downstream is allowed to derive a time from the raw ISO —
            # that's what drifted "11:00 AM" into "1:00 PM" on the homepage.
            when_local = (
                format_when_local(start, self.temporal, all_day=all_day)
                if self.temporal is not None
                else None
            )

            attrs: dict[str, Any] = {
                "start_iso": start,
                "end_iso": end,
                "when_local": when_local,
                "all_day": all_day,
                # The reader's own RSVP. `needs_action` events are deterministically
                # barred from producing prep cards (see `_drop_unaccepted_prep`) and
                # collected into the calendar-hygiene note instead.
                "rsvp": rsvp,
                "attendee_count": len(attendee_emails),
                "external_attendee_count": len(external_attendees),
                "external_attendees": external_attendees,
                "external_domains": external_domains,
                # Reader EXCLUDED — see the comment where this is built. The
                # decision-maker names the meeting's other party from here and
                # nowhere else.
                "counterparties": counterparties,
                "counterparty_count": len(counterparties),
                "has_description": bool(description.strip()),
                "has_attached_docs": len(attachments) > 0,
                "conference_url": conference_url,
                "location": ev.get("location"),
                "recurring_event_id": ev.get("recurringEventId"),
                # The decision-maker reads this to find related entities;
                # each external attendee becomes a join key.
                "related_entities": [
                    {"entity_kind": EntityKind.PERSON.value, "entity_id": a}
                    for a in external_attendees
                ]
                + [
                    {"entity_kind": EntityKind.COMPANY.value, "entity_id": d}
                    for d in external_domains
                ],
            }

            # Anchor the event on the first external company we see — that
            # makes calendar events joinable with HubSpot deals by company
            # domain, even when no deal email lives in attendees.
            primary_entity_kind = EntityKind.EVENT
            primary_entity_id = str(event_id)
            primary_entity_label = ev.get("summary") or "(untitled)"

            signals.append(
                ChannelSignal(
                    id=str(event_id),
                    channel=ChannelKind.CALENDAR,
                    title=ev.get("summary", "(untitled)")[:160],
                    snippet=description[:300],
                    link=ev.get("htmlLink"),
                    timestamp=start,
                    entity_kind=primary_entity_kind,
                    entity_id=primary_entity_id,
                    entity_label=primary_entity_label,
                    attrs=attrs,
                )
            )
        return signals

    def build_prompt(self, signals: list[ChannelSignal]) -> str:
        event_lines = []
        for s in signals[:15]:
            attrs = s.attrs
            event_lines.append(
                "- id={id}\n"
                "  title: {title}\n"
                "  when: {when}\n"
                "  with: {with_who}\n"
                "  your_rsvp: {rsvp}\n"
                "  externals: {ext_count} ({ext_domains})\n"
                "  description_present: {desc}\n"
                "  attached_docs: {docs}\n"
                "  conference: {conf}".format(
                    id=s.id,
                    title=s.title,
                    # Reader-local wall clock, pre-computed in parse_signals.
                    # Fall back to the raw ISO only when no temporal context
                    # was supplied (never happens in the live pipeline).
                    when=attrs.get("when_local") or attrs.get("start_iso") or "?",
                    with_who=", ".join(attrs.get("counterparties") or [])
                    or "(no one else on the invite)",
                    rsvp=attrs.get("rsvp") or "none",
                    ext_count=attrs.get("external_attendee_count", 0),
                    ext_domains=", ".join(attrs.get("external_domains") or []) or "-",
                    desc=attrs.get("has_description"),
                    docs=attrs.get("has_attached_docs"),
                    conf="yes" if attrs.get("conference_url") else "no",
                )
            )
        event_block = "\n".join(event_lines) if event_lines else "(no events)"

        return (
            # No judgment vocabulary — extractor only.
            "You are the Calendar channel extractor. Read the upcoming "
            "events and return a structured JSON observation. You do NOT "
            "decide which events matter — the decision-maker downstream "
            "does that.\n\n"
            "## WHOSE CALENDAR THIS IS\n"
            f"You are reading {self.user_email}'s own calendar. The `with:` "
            "line on each event already lists the OTHER people on the invite "
            "and never includes the reader. Never describe the reader as the "
            'person they are meeting: a title like "Alex / Blair 1:1" '
            "contains both names, so the name to use is the one on `with:`, "
            "not whichever name the title happens to start with.\n\n"
            "## RSVP\n"
            "`your_rsvp` is the reader's own response to the invite: "
            "`accepted`, `tentative`, `needs_action` (never answered), or "
            "`none` (a solo event, or one the reader organized — the question "
            "doesn't apply). A `needs_action` event is NOT a commitment the "
            "reader has made: do not describe it as something they are "
            "attending. Declined events are already filtered out before you "
            "see them.\n\n"
            "## TIME HANDLING\n"
            "The `when:` line is already the reader's local wall-clock time. "
            "If you state a meeting time in `summary`, copy that string "
            "exactly — never recompute or reformat a time from an "
            "ISO/offset string.\n\n"
            # Summary only. This prompt used to ask for per-event
            # `mentioned_topics` / `prep_keywords`, which nothing downstream
            # ever read (neither is in decision_maker.ATTRS_PREVIEW_KEYS) —
            # output tokens paid for on every run. Every event attr the
            # decision-maker sees is computed in parse_signals.
            "## OUTPUT (raw JSON only)\n"
            "{\n"
            '  "summary": "1 factual sentence describing the user\'s upcoming calendar today/tomorrow."\n'
            "}\n\n"
            f"## EVENTS\n{event_block}\n"
        )
