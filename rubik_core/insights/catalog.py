"""The metric catalog: what every Insights number means, in plain words.

Every KPI a dashboard emits carries only an ``id`` and its numbers; the label,
unit, definition and which direction is good come from here, once. The
frontend shows ``definition`` behind each ⓘ, lists the catalog in its
Definitions drawer, and colours a change green or red by ``good_direction``.
``backend/tests/test_insights_catalog.py`` fails if a dashboard emits an id
that is not here.

Definitions are read by executives: business language, no table or column
names, and a percentage always says what it is out of (``denominator``) —
the house rule since two accuracy figures over different totals caused an
escalation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from rubik_core.brand import PRODUCT_NAME

Unit = Literal["count", "pct", "usd", "ms", "days", "ratio", "score"]
Direction = Literal["up", "down", "neutral"]


@dataclass(frozen=True)
class MetricDef:
    id: str
    label: str
    definition: str
    denominator: str | None
    unit: Unit
    good_direction: Direction
    #: The unit of a ``pct`` KPI's numerator and denominator when they are not
    #: counts (a share of dollars: "$57 of $215"). None means counts.
    part_unit: Unit | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_ACTIVE = (
    f"Active means at least one recorded use of {PRODUCT_NAME} on a calendar day in the "
    "company's business time zone: "
    f"signing in, asking Ask {PRODUCT_NAME}, acting on an AI Priorities card, Meeting Prep, "
    "a proposal, a CRM Sync approval, a My To-Dos change, or (since Insights "
    f"launched) opening any part of {PRODUCT_NAME}."
)

_METRICS: tuple[MetricDef, ...] = (
    # ── Adoption ────────────────────────────────────────────────────────────
    MetricDef(
        "active_people",
        "Active people",
        f"People in the selected departments who used {PRODUCT_NAME} on at least one day "
        "in the period. " + _ACTIVE,
        None,
        "count",
        "up",
    ),
    MetricDef(
        "active_rate",
        "Active share",
        "Active people as a share of everyone in the selected departments. "
        f"Everyone at the company can sign in to {PRODUCT_NAME} (EOS is open to the whole "
        "company), so everyone counts. " + _ACTIVE,
        "people in the selected departments",
        "pct",
        "up",
    ),
    MetricDef(
        "avg_dau",
        "Average daily active people",
        f"On an average day in the period, how many people used {PRODUCT_NAME}. Weekends "
        "count as days, so this reads lower than a weekday would.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "stickiness",
        "Stickiness",
        "Average daily active people divided by the people active in the 30 "
        "days ending on the period's last day: of this month's users, how many "
        "come back on a typical day.",
        "people active in the last 30 days",
        "pct",
        "up",
    ),
    MetricDef(
        "new_people",
        "Newly active people",
        f"People whose first recorded use of {PRODUCT_NAME} falls in the period.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "dormant_people",
        "Dormant people",
        f"People in the selected departments with no recorded use of {PRODUCT_NAME} in "
        "the 14 days up to the period's last day, including people who have "
        "never used it.",
        None,
        "count",
        "down",
    ),
    MetricDef(
        "active_days",
        "Active days",
        f"Days in the period on which this person used {PRODUCT_NAME} at least once.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "surfaces_used",
        f"Parts of {PRODUCT_NAME} used",
        f"How many different parts of {PRODUCT_NAME} (Ask {PRODUCT_NAME}, AI Priorities, Meeting "
        "Prep, EOS, Proposals and so on) this person used in the period.",
        None,
        "count",
        "up",
    ),
    # ── Ask Cubie ───────────────────────────────────────────────────────────
    MetricDef(
        "questions",
        "Questions asked",
        f"Questions and requests sent to Ask {PRODUCT_NAME} in the period. Scheduled "
        f"tasks, which {PRODUCT_NAME} runs on its own, are counted separately.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "conversations",
        "Conversations",
        f"Ask {PRODUCT_NAME} conversations that had at least one question in the period.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "turns_per_conversation",
        "Questions per conversation",
        "Questions asked divided by conversations: how far people take a "
        f"conversation with {PRODUCT_NAME}.",
        "conversations",
        "ratio",
        "up",
    ),
    MetricDef(
        "people_asking",
        "People asking",
        f"People who asked Ask {PRODUCT_NAME} at least one question in the period.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "people_asking_rate",
        "Share of people asking",
        f"People who asked Ask {PRODUCT_NAME} at least one question, out of the people in "
        f"the selected departments who have the {PRODUCT_NAME} copilot.",
        f"people with the {PRODUCT_NAME} copilot",
        "pct",
        "up",
    ),
    MetricDef(
        "chat_success_rate",
        "Answers that succeeded",
        f"Questions {PRODUCT_NAME} answered without an error, out of all questions asked.",
        "questions asked",
        "pct",
        "up",
    ),
    MetricDef(
        "chat_p50_ms",
        "Typical answer time",
        "The median time from sending a question to the full answer: half of "
        "the answers took less, half took more.",
        None,
        "ms",
        "down",
    ),
    MetricDef(
        "chat_p95_ms",
        "Slow answer time",
        "95 of every 100 answers arrived faster than this. It shows how long the "
        "slowest answers make people wait.",
        None,
        "ms",
        "down",
    ),
    MetricDef(
        "voice_share",
        "Spoken questions",
        "Questions dictated with the microphone, out of all questions asked.",
        "questions asked",
        "pct",
        "neutral",
    ),
    MetricDef(
        "chat_useful_rate",
        "Answers rated useful",
        "Answers people rated 'useful', out of the answers they rated. A "
        "re-rating replaces the earlier one, so each answer counts once.",
        "answers rated",
        "pct",
        "up",
    ),
    MetricDef(
        "scheduled_runs",
        "Scheduled task runs",
        f"Times {PRODUCT_NAME} ran a scheduled task (a recurring request set up in Ask "
        f"{PRODUCT_NAME}) for people in the selected departments.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "scheduled_success_rate",
        "Scheduled runs that succeeded",
        "Scheduled task runs that finished successfully, out of the runs that "
        "finished (succeeded or failed).",
        "scheduled runs that finished",
        "pct",
        "up",
    ),
    # ── AI Priorities ───────────────────────────────────────────────────────
    MetricDef(
        "priorities_surfaced",
        "Priorities surfaced",
        "Distinct AI Priorities cards shown to people in the selected "
        "departments, counted on the day each first appeared. A card shown on "
        "several days counts once.",
        None,
        "count",
        "neutral",
    ),
    MetricDef(
        "priorities_acted",
        "Cards acted on",
        "Cards the person marked done themselves or acted on from the card "
        "(a Slack follow-up).",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "priorities_acted_rate",
        "Acted-on rate",
        "Cards acted on by the person they were shown to, out of the cards "
        f"surfaced in the period. Cards that closed because {PRODUCT_NAME} saw the work "
        "done are not counted as acted on.",
        "priorities surfaced",
        "pct",
        "up",
    ),
    MetricDef(
        "priorities_dismiss_rate",
        "Priorities dismissed",
        "Cards people dismissed as not needed, out of the cards surfaced. A high "
        f"rate for one kind of card is a sign {PRODUCT_NAME} is surfacing noise.",
        "priorities surfaced",
        "pct",
        "down",
    ),
    MetricDef(
        "priorities_time_to_action_days",
        "Typical time to act",
        f"For cards that were acted on, the median time from when {PRODUCT_NAME} first "
        "surfaced the item to when the person acted, in days (0.5 is half a "
        "day).",
        None,
        "days",
        "down",
    ),
    MetricDef(
        "briefings_generated",
        "Briefings generated",
        "Daily AI Priorities briefings prepared for people in the selected "
        "departments, one per person per day.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "briefings_per_person",
        "Briefings per person",
        "Briefings generated divided by the people in the selected departments "
        f"who have the {PRODUCT_NAME} copilot.",
        f"people with the {PRODUCT_NAME} copilot",
        "ratio",
        "up",
    ),
    MetricDef(
        "priorities_run_failure_rate",
        "Failed priorities runs",
        "On-demand AI Priorities runs that failed, out of all on-demand runs started.",
        "priorities runs started",
        "pct",
        "down",
    ),
    MetricDef(
        "slack_dms_delivered",
        "Slack briefings delivered",
        "AI Priorities cards delivered to people by Slack direct message.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "critic_score",
        "Briefing quality score",
        f"{PRODUCT_NAME}'s own quality check of each briefing before it is shown, scored "
        "0 to 10 and shown here as 0 to 1 (0.8 means 8 out of 10), averaged "
        "over the briefings in the period.",
        None,
        "score",
        "up",
    ),
    MetricDef(
        "priorities_useful_rate",
        "Priorities rated useful",
        "Priorities cards people rated 'useful', out of the cards they rated.",
        "priorities rated",
        "pct",
        "up",
    ),
    # ── Follow-through ──────────────────────────────────────────────────────
    MetricDef(
        "loose_ends_flagged",
        "Loose ends flagged",
        "Loose ends the morning Loose Ends check first put on someone's list in "
        "the period: a client email or conversation not logged in HubSpot, a "
        "client waiting on a reply, or a promise or HubSpot task gone late.",
        None,
        "count",
        "neutral",
    ),
    MetricDef(
        "loose_ends_noise_rate",
        "Marked 'not a real miss'",
        "Loose ends flagged in the period that the person marked 'Not a real "
        "miss', out of all the loose ends flagged in the period. This is how the "
        "team judges whether the check is accurate: lower is better.",
        "loose ends flagged",
        "pct",
        "down",
    ),
    MetricDef(
        "loose_ends_closed_rate",
        "Loose ends closed",
        "Loose ends flagged in the period that have since closed because the "
        "record shows the work was done (a reply sent, an activity or a "
        "completed task in HubSpot, a promise kept), out of all the loose ends "
        "flagged. Items that aged out or were rescheduled are not counted as "
        "closed.",
        "loose ends flagged",
        "pct",
        "up",
    ),
    MetricDef(
        "loose_ends_days_to_close",
        "Typical days to close a loose end",
        "For loose ends flagged in the period that closed, the median time from "
        "when the check first flagged the item to when the record showed it "
        "done, in days.",
        None,
        "days",
        "down",
    ),
    MetricDef(
        "loose_ends_open",
        "Loose ends open",
        "Loose ends still open at the end of the period's last day.",
        None,
        "count",
        "down",
    ),
    MetricDef(
        "promises_kept_rate",
        "Promises kept",
        "My To-Dos promises marked kept in the period, out of the promises kept "
        "or dropped in it. Promises handed to someone else are left out.",
        "promises kept or dropped",
        "pct",
        "up",
    ),
    MetricDef(
        "promises_slipping_now",
        "Promises slipping now",
        "My To-Dos promises that are past their date and not yet kept or "
        "dropped, as of right now. This number does not change with the date "
        "range.",
        None,
        "count",
        "down",
    ),
    MetricDef(
        "promises_promoted_eos",
        "Promises made EOS To-Dos",
        "My To-Dos promises that the person turned into a team EOS To-Do in the "
        "period.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "cockpit_breach_rate",
        "Sales commitments missed",
        "Sales Cockpit commitments (a statement of work owed, a call wrap-up, a "
        "first touch on a new lead) that went past their deadline, out of the "
        "commitments whose deadline fell in the period. Shown per department "
        "only, never per person: wrap-up commitments come from recorded calls.",
        "commitments due in the period",
        "pct",
        "down",
    ),
    # ── CRM & HubSpot ───────────────────────────────────────────────────────
    MetricDef(
        "crm_hubspot_updates",
        f"HubSpot updates by {PRODUCT_NAME}",
        f"Changes {PRODUCT_NAME} made in HubSpot after a person approved them: CRM Sync "
        "suggestions applied, Call Capture wrap-up items written, Sales Cockpit "
        "nudges (each one creates a HubSpot task), deal stages corrected from "
        "the Cockpit, and emails logged from Loose Ends. Only changes HubSpot "
        "confirmed count. On a person's page Call Capture is left out: it is "
        "only ever counted per department.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "crm_sync_suggestions",
        "CRM Sync suggestions",
        "Changes to HubSpot that CRM Sync proposed from people's call "
        "transcripts in the period, whatever happened to them next.",
        None,
        "count",
        "neutral",
    ),
    MetricDef(
        "crm_sync_acceptance_rate",
        "CRM Sync suggestions approved",
        "Of the CRM Sync suggestions made in the period that people decided on, "
        "the share they approved. An approval whose write to HubSpot then "
        "failed still counts as approved (it shows under failed writes). "
        "Suggestions still waiting, replaced by a newer one or expired "
        "unanswered are not decisions and are left out.",
        "CRM Sync suggestions approved or dismissed",
        "pct",
        "up",
    ),
    MetricDef(
        "crm_sync_time_to_apply_days",
        "Typical time to approve",
        "For CRM Sync suggestions made in the period and written to HubSpot, "
        "the median time from the suggestion to its approval, in days (0.25 is "
        "six hours).",
        None,
        "days",
        "down",
    ),
    MetricDef(
        "crm_write_failure_rate",
        "Failed HubSpot writes",
        "Writes to HubSpot that failed, out of all the writes attempted in the "
        "period, for the kinds of change that record a failure: CRM Sync and "
        "Call Capture wrap-ups. A write that failed and then went through on a "
        "retry counts once, as a success.",
        "HubSpot writes attempted",
        "pct",
        "down",
    ),
    MetricDef(
        "loose_ends_emails_logged",
        "Emails logged from Loose Ends",
        "Loose ends closed because the person clicked 'Log it in HubSpot', which "
        "logs their own sent emails on the client's HubSpot records.",
        None,
        "count",
        "up",
    ),
    # ── Proposals & Prep ────────────────────────────────────────────────────
    MetricDef(
        "proposals_started",
        "Proposals started",
        "Proposals people started in the Proposal Generator in the period, from "
        "a sales call.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "proposals_drafted",
        "Proposals drafted",
        f"Proposals started in the period for which {PRODUCT_NAME} generated at least one "
        "draft, after the person confirmed the facts it is built from.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "proposals_draft_rate",
        "Proposals that reached a draft",
        "Proposals that reached a draft, out of the proposals started in the period.",
        "proposals started",
        "pct",
        "up",
    ),
    MetricDef(
        "proposal_revisions_per_proposal",
        "Revisions per proposal",
        "Drafts generated after the first one ('tighten the summary', 'change "
        "the price'), per proposal drafted in the period. Higher means more "
        "back-and-forth before a proposal is ready.",
        "proposals drafted",
        "ratio",
        "neutral",
    ),
    MetricDef(
        "proposals_deal_linked_rate",
        "Proposals linked to a deal",
        "Proposals started in the period that are tied to a HubSpot deal, out of "
        "all the proposals started.",
        "proposals started",
        "pct",
        "up",
    ),
    MetricDef(
        "proposals_days_to_draft",
        "Typical time to a first draft",
        "For proposals started in the period that reached a draft, the median "
        "time from starting the proposal to its first draft, in days.",
        None,
        "days",
        "down",
    ),
    MetricDef(
        "prep_briefs",
        "Meeting Prep briefs",
        f"Meeting Prep briefs {PRODUCT_NAME} prepared: briefs generated or regenerated on "
        "screen, plus deep prep briefs sent by email. A hand edit, a section "
        "rewrite or a restore of an existing brief is not a new brief.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "prep_deep_share",
        "Deep prep share",
        "Deep prep briefs (the longer, researched brief sent by email), out of "
        "all Meeting Prep briefs.",
        "Meeting Prep briefs",
        "pct",
        "neutral",
    ),
    MetricDef(
        "prep_edit_rate",
        "Briefs edited or rewritten",
        f"On-screen briefs the person then edited by hand, or had {PRODUCT_NAME} rewrite a "
        "section of, out of the on-screen briefs generated in the period. A high "
        "rate means the first draft often needs work.",
        "on-screen briefs generated",
        "pct",
        "neutral",
    ),
    MetricDef(
        "prep_failure_rate",
        "Failed Meeting Prep briefs",
        "Brief generations that failed, out of all the brief generations "
        "attempted (on screen and deep prep).",
        "brief generations attempted",
        "pct",
        "down",
    ),
    MetricDef(
        "xray_snapshots",
        "Customer X-Ray snapshots",
        "Customer X-Ray snapshots people generated (quick or deep research), not "
        "counting runs that failed.",
        None,
        "count",
        "up",
    ),
    # ── Overview ────────────────────────────────────────────────────────────
    MetricDef(
        "ai_spend_per_active_person",
        "AI spend per active person",
        f"What {PRODUCT_NAME}'s AI calls cost in the period, divided by the people active "
        "in it. For the whole company this is all AI spend. When departments "
        "are filtered, only spend that can be tied to a person in those "
        "departments counts. Since Insights launched almost all spend carries "
        "a person; before that most did not (the nightly AI Priorities run "
        "included), so a department's figure for earlier periods is partial "
        "and reads low. Features that are only ever reported for the whole "
        "company are never counted in a department's figure.",
        "active people",
        "usd",
        "down",
    ),
    # ── AI Spend ────────────────────────────────────────────────────────────
    MetricDef(
        "ai_spend_total",
        "AI spend",
        f"What {PRODUCT_NAME}'s AI calls cost in the period, in US dollars, at the rates "
        "in force when each call was made. For the whole company this is every "
        "dollar. When departments are filtered (or on a person's page) only "
        "spend tied to a person in view counts, which is complete from the "
        "Insights release on and partial before it; features that are only "
        "ever reported for the whole company are left out of those views.",
        None,
        "usd",
        "neutral",
    ),
    MetricDef(
        "ai_spend_attributed_share",
        "Spend tied to a person",
        "AI spend made on behalf of a named person, out of all AI spend in the "
        "period. The rest is background work no one asked for at that moment "
        "(document ingestion, overnight jobs). Only shown for the whole company.",
        "AI spend in the period",
        "pct",
        "up",
        part_unit="usd",
    ),
    MetricDef(
        "ai_cost_per_briefing",
        "AI cost per briefing",
        "AI spend on AI Priorities divided by the daily briefings it prepared in "
        "the period (one per person per day). For a department view only spend "
        "tied to a person counts, so periods before the Insights release read low.",
        "briefings generated",
        "usd",
        "down",
    ),
    MetricDef(
        "ai_cost_per_answer",
        "AI cost per answer",
        f"AI spend on Ask {PRODUCT_NAME}, including the searches it runs while answering, "
        "divided by the questions people asked in the period.",
        "questions asked",
        "usd",
        "down",
    ),
    MetricDef(
        "ai_cost_per_proposal",
        "AI cost per proposal",
        "AI spend on the Proposal Generator (reading the call, drafting and "
        "revising) divided by the proposals whose first document was drafted "
        "in the period.",
        "proposals drafted",
        "usd",
        "down",
    ),
    # ── Knowledge & Content ─────────────────────────────────────────────────
    MetricDef(
        "documents_added",
        "Documents added",
        f"Documents people in the selected departments added to {PRODUCT_NAME}'s "
        "knowledge base in the period. Meeting transcripts are counted "
        "separately.",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "document_versions_added",
        "New versions uploaded",
        "New versions of existing knowledge-base documents uploaded in the "
        "period (the first upload of a document is not counted here).",
        None,
        "count",
        "up",
    ),
    MetricDef(
        "transcripts_ingested",
        "Meeting transcripts added",
        f"Meeting transcripts {PRODUCT_NAME} added to the knowledge base in the period "
        "for people in the selected departments. Counted per department only, "
        "never per person.",
        None,
        "count",
        "neutral",
    ),
    MetricDef(
        "answers_citing_documents_rate",
        "Answers citing documents",
        f"Ask {PRODUCT_NAME} answers that cited at least one document as a source, out of "
        "all answers given in the period. A rising share means people's "
        "questions are being answered from the company's own documents.",
        "answers given",
        "pct",
        "up",
    ),
    # ── Quality & Trust ─────────────────────────────────────────────────────
    MetricDef(
        "useful_rating_rate",
        "Rated useful",
        "Items people rated 'useful', out of every item they rated in the "
        f"period, across Ask {PRODUCT_NAME}, AI Priorities, Meeting Prep, CRM Sync and "
        "Customer X-Ray. A re-rating replaces the earlier one.",
        "items rated",
        "pct",
        "up",
    ),
    MetricDef(
        "chat_error_rate",
        "Answers that failed",
        f"Questions Ask {PRODUCT_NAME} could not answer because of an error, out of all "
        "questions asked in the period.",
        "questions asked",
        "pct",
        "down",
    ),
    MetricDef(
        "quality_meeting_prep_rewrite_rate",
        "Meeting briefs rewritten",
        "Meetings whose first brief was prepared in the period and that the "
        "person then regenerated, had a section rewritten, or edited by hand, "
        "out of all meetings briefed in the period. Brief versions are kept "
        "from the Meeting Prep versions release on.",
        "meetings briefed",
        "pct",
        "down",
    ),
    MetricDef(
        "quality_loose_ends_noise_rate",
        "Loose ends dismissed as noise",
        "Loose ends the account executive dismissed as not a real miss, out of "
        "all loose ends first flagged in the period. This is Loose Ends' own "
        "success measure.",
        "loose ends flagged",
        "pct",
        "down",
    ),
    MetricDef(
        "quality_crm_sync_acceptance_rate",
        "CRM Sync suggestions approved",
        f"HubSpot changes {PRODUCT_NAME} suggested that the owner approved, out of the "
        "suggestions the owner decided on (approved or dismissed) in the period.",
        "suggestions decided",
        "pct",
        "up",
    ),
    MetricDef(
        "quality_proposal_redraft_rate",
        "Proposals revised after the first draft",
        "Proposals whose first document was drafted in the period and that "
        "were then revised at least once, out of all proposals drafted in the "
        "period. Many revisions mean the first draft missed what was needed.",
        "proposals drafted",
        "pct",
        "down",
    ),
)

METRICS: dict[str, MetricDef] = {m.id: m for m in _METRICS}
assert len(METRICS) == len(_METRICS), "duplicate metric id in the Insights catalog"


def metric(metric_id: str) -> MetricDef:
    try:
        return METRICS[metric_id]
    except KeyError:
        raise KeyError(
            f"unknown Insights metric {metric_id!r}; add it to rubik_core/insights/catalog.py"
        ) from None


def catalog_dict() -> dict[str, dict]:
    return {m.id: m.to_dict() for m in _METRICS}
