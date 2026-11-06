"""UC-47 — research the people on a meeting invite. Entry point for the feature.

`research_attendees` is the whole public surface. It runs inside meeting prep's
source-gathering ``asyncio.gather`` (in the background build, see
``meeting_prep/background.py``), so it is written to a wall-clock budget and
to fail soft everywhere: a dead provider, a slow grounded call, a namesake it could
not disambiguate, or a cache error all degrade the attendee panel rather than the
briefing.

Who gets researched, and why the filter is narrow:

* **External attendees only.** Colleagues are already known to the reader,
  their LinkedIn adds nothing to a 1:1 brief, and profiling coworkers is not a
  thing this app should do.
* **Real humans only.** Shared mailboxes and calendar resources are dropped by
  `PersonQuery.is_researchable` — an invented persona for `billing@` is worse
  than a missing card.
* **Capped, and the cap is reported.** `skipped_attendees` names anyone past
  `MAX_ATTENDEES`, because a silently truncated list reads as "these people have
  no public presence".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import anyio

from rubik_core.cost.usage import attribute_to
from rubik_core.people import config, persona, store
from rubik_core.people.schemas import AttendeeIntel
from rubik_core.people.sources.base import PersonQuery, resolve_source

logger = logging.getLogger(__name__)


@dataclass
class AttendeeIntelResult:
    """What the meeting-prep context gets back."""

    intel: list[AttendeeIntel] = field(default_factory=list)
    # Emails deliberately not researched because of `MAX_ATTENDEES`.
    skipped_attendees: list[str] = field(default_factory=list)
    # Human-readable notes for `context["errors"]` (surfaced as partial_errors).
    errors: list[str] = field(default_factory=list)

    def as_dicts(self) -> list[dict]:
        return [i.as_dict() for i in self.intel]


def _company_hint(attendee: dict, domain: str) -> str:
    """Best available company name for disambiguation.

    Falls back to the email domain's second-level label ("globex.com" →
    "Globex"), which is a weak hint but a real one: it is what separates the
    right Pat Lee from the other four thousand.
    """
    for key in ("company", "organization", "company_name"):
        value = str(attendee.get(key) or "").strip()
        if value:
            return value
    if not domain:
        return ""
    label = domain.split(".")[0]
    return label.replace("-", " ").title() if label else ""


def build_queries(
    attendees: list[dict],
    *,
    user_email: str,
    limit: int | None = None,
) -> tuple[list[PersonQuery], list[str]]:
    """External, researchable, deduped attendees → (queries, skipped_emails).

    Pure and side-effect free so the filter can be tested without touching an
    LLM or a database — this is the function that decides who gets profiled at
    all, which makes it the one most worth pinning down in tests.
    """
    user_domain = user_email.split("@")[-1].lower() if "@" in user_email else ""
    # The company's own domain as well as the reader's: a caller with no reader
    # (user_email="") must still never profile a colleague.
    from rubik_core.company.domain import workspace_domains

    internal = set(workspace_domains())
    if user_domain:
        internal.add(user_domain)
    cap = config.MAX_ATTENDEES if limit is None else limit

    queries: list[PersonQuery] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for attendee in attendees or []:
        email = str(attendee.get("email") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        domain = email.split("@")[-1]
        if domain in internal:
            continue  # internal colleague
        if email == user_email.strip().lower():
            continue

        query = PersonQuery(
            email=email,
            name=str(
                attendee.get("display_name") or attendee.get("displayName") or ""
            ).strip(),
            company_hint=_company_hint(attendee, domain),
            title_hint=str(attendee.get("title") or "").strip(),
        )
        if not query.is_researchable():
            continue
        if len(queries) >= cap:
            skipped.append(email)
            continue
        queries.append(query)

    return queries, skipped


@attribute_to("attendee_intel")
def research_one(query: PersonQuery) -> AttendeeIntel:
    """Research one person through the best available source. Never raises.

    Sources are tried in fidelity order and a source that returns nothing usable
    falls through to the next, so an out-of-credit API degrades to grounded
    search instead of blanking the person. Decorated (rather than relying on a
    contextvar at the caller) because this runs in a worker thread — see
    ``cost.usage.attribute_to``.
    """
    plan = resolve_source()
    last_error = ""

    for source in plan.sources:
        try:
            evidence = source.fetch(query)
        except Exception as exc:  # noqa: BLE001 - try the next source
            last_error = f"{source.name}: {exc}"
            logger.info(
                "attendee intel: %s fetch failed for %s: %s",
                source.name,
                query.email,
                exc,
            )
            continue

        if evidence.is_empty():
            continue

        try:
            raw = (
                persona.synthesize_structured(query, evidence)
                if evidence.structured
                else evidence.text
            )
            intel = persona.parse_intel(
                raw,
                query=query,
                provider=source.name,
                citations=evidence.citations,
            )
        except Exception as exc:  # noqa: BLE001 - try the next source
            last_error = f"{source.name}: {exc}"
            logger.info(
                "attendee intel: %s synthesis failed for %s: %s",
                source.name,
                query.email,
                exc,
            )
            continue

        if intel.has_content():
            return intel
        # A clean "found nothing" is an answer, not a failure. Returned WITHOUT an
        # error set, so it caches for the full TTL — somebody with no public
        # professional footprint today almost certainly has none in two weeks, and
        # re-searching every prep would pay repeatedly for the same silence. Only
        # the LAST source's silence counts, though: a higher-fidelity source
        # coming up empty still falls through to grounded search.
        if source is plan.sources[-1]:
            return intel

    return AttendeeIntel(
        email=query.email.lower(),
        name=query.display_name(),
        provider=(plan.names() or [""])[0],
        error=last_error or "no public professional presence found",
    )


async def research_attendees(
    attendees: list[dict],
    *,
    user_email: str,
    use_cache: bool = True,
) -> AttendeeIntelResult:
    """Research every external attendee on an invite, concurrently and bounded.

    Returns in invite order. Cached people cost nothing; the rest are researched
    in parallel under `PER_ATTENDEE_BUDGET_S` each and `TOTAL_BUDGET_S` overall,
    so the step's contribution to a prep's latency has a ceiling regardless of
    how many strangers are on the invite.
    """
    result = AttendeeIntelResult()
    if not config.enabled():
        return result

    queries, skipped = build_queries(attendees, user_email=user_email)
    result.skipped_attendees = skipped
    if skipped:
        result.errors.append(
            f"Attendee research capped at {config.MAX_ATTENDEES} people; skipped "
            + ", ".join(skipped)
        )
    if not queries:
        return result

    cached: dict[str, AttendeeIntel] = (
        store.load_fresh([q.email for q in queries]) if use_cache else {}
    )
    todo = [q for q in queries if q.email not in cached]

    timed_out: list[str] = []

    async def _research(query: PersonQuery) -> tuple[str, AttendeeIntel | None]:
        try:
            intel = await asyncio.wait_for(
                anyio.to_thread.run_sync(research_one, query),
                timeout=config.PER_ATTENDEE_BUDGET_S,
            )
        except (TimeoutError, asyncio.TimeoutError):
            # WARNING, not INFO: a timeout leaves the card out of the brief, and
            # at INFO a week of timed-out lookups can go unnoticed.
            logger.warning(
                "attendee intel: %s timed out after %.0fs",
                query.email,
                config.PER_ATTENDEE_BUDGET_S,
            )
            timed_out.append(query.email)
            return query.email, None
        except Exception as exc:  # noqa: BLE001 - one person never sinks the run
            logger.warning("attendee intel: %s failed: %s", query.email, exc)
            return query.email, None
        return query.email, intel

    fresh: dict[str, AttendeeIntel] = {}
    if todo:
        try:
            done = await asyncio.wait_for(
                asyncio.gather(*(_research(q) for q in todo)),
                timeout=config.TOTAL_BUDGET_S,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "attendee intel: total budget exhausted researching %d people",
                len(todo),
            )
            done = []
            result.errors.append(
                "Attendee research timed out; some LinkedIn profiles are missing."
            )

        for email, intel in done:
            if intel is not None:
                fresh[email] = intel
        if done and timed_out:
            # Said in the brief's Intelligence Gaps, so a missing card reads as
            # "Cubie ran out of time", not "this person has no public presence".
            result.errors.append(
                "Attendee research ran out of time for "
                + ", ".join(timed_out)
                + "; their cards are missing from this brief."
            )

        # Persist off the event loop: the write is small but it is still I/O, and
        # the caller is a request handler.
        if fresh:

            def _persist() -> None:
                for intel in fresh.values():
                    store.save(intel)

            try:
                await anyio.to_thread.run_sync(_persist)
            except Exception:  # noqa: BLE001 - caching is never load-bearing
                logger.warning("attendee intel: cache persist failed", exc_info=True)

    for query in queries:
        intel = cached.get(query.email) or fresh.get(query.email)
        if intel is None:
            continue
        if not intel.name:
            intel.name = query.display_name()
        result.intel.append(intel)

    researched = [i for i in result.intel if i.has_content()]
    logger.info(
        "attendee intel: %d/%d attendees have content (%d from cache) for %s",
        len(researched),
        len(queries),
        len(cached),
        user_email,
    )
    return result
