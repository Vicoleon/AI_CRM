"""The registry of gated features and the access each one ships with.

One ``FeatureSpec`` per gated surface. The spec is the part that is CODE — what
the feature is, where it shows up, how membership is judged, which env kill
switch it hangs off — and the ``default`` is the access it ships with. The
roster an admin actually wants is DATA: Admin → Feature Access writes a
``feature_access`` row that overrides the default (see ``policy.py``), so
narrowing or widening a feature never needs a deploy.

No default names a department or a person. Everyone in the workspace gets
every user-facing feature; role-scoped features open on the admin and
Exec-team flags only (see "Code defaults" below).

Membership is judged by RAG slug — ``slugify_department`` — never a raw
compare: department rows are uncurated free text and "Sales"/"sales"/" Sales "
are one team (docs/ENGINEERING_NOTES.md). A team whose row does not slugify into the roster is
locked out SILENTLY, so the admin page shows the live department list rather
than asking anyone to type a slug.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from rubik_core.brand import PRODUCT_NAME

Membership = Literal["primary_or_managed", "managed_only"]
Module = Literal["core", "eos", "sales_coach"]


@dataclass(frozen=True)
class FeaturePolicy:
    """Who may reach one feature.

    * ``departments`` — RAG slugs; a person qualifies through their primary
      department or (unless the spec says ``managed_only``) a department they
      manage.
    * ``extra_emails`` — named people admitted regardless of department
      (lower-cased). How a cross-person view gets its audience.
    * ``exec_allowed`` — the Exec-team flag opens the door. Deliberately per
      feature: X-Ray says yes (a seniority-scoped view), CRM Sync says no (it
      acts on one AE's own deals).
    * ``admin_allowed`` — workspace admins get in to support the feature.
      Nearly always true; Smart Teams says no on purpose.
    * ``everyone`` — open to every signed-in user; the roster is ignored. How
      every user-facing feature ships.

    An empty roster with every flag off is a CLOSED door, never an open one.
    """

    departments: frozenset[str] = field(default_factory=frozenset)
    extra_emails: frozenset[str] = field(default_factory=frozenset)
    exec_allowed: bool = False
    admin_allowed: bool = True
    everyone: bool = False

    def to_dict(self) -> dict:
        return {
            "departments": sorted(self.departments),
            "extra_emails": sorted(self.extra_emails),
            "exec_allowed": self.exec_allowed,
            "admin_allowed": self.admin_allowed,
            "everyone": self.everyone,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FeaturePolicy:
        from rubik_core.rag.config import slugify_department  # local: keep leaf light

        depts = frozenset(
            slug
            for slug in (
                slugify_department(str(d or ""))
                for d in (data.get("departments") or [])
            )
            if slug
        )
        emails = frozenset(
            e
            for e in (
                str(x or "").strip().lower() for x in (data.get("extra_emails") or [])
            )
            if e
        )
        return cls(
            departments=depts,
            extra_emails=emails,
            exec_allowed=bool(data.get("exec_allowed", False)),
            admin_allowed=bool(data.get("admin_allowed", True)),
            everyone=bool(data.get("everyone", False)),
        )


@dataclass(frozen=True)
class FeatureSpec:
    """One gated surface, declared once.

    ``description`` and ``notes`` are read by admins on the maintenance page —
    write them as business outcomes, not implementation. ``kill_switch`` names
    a ``settings`` attribute that turns the feature off ENTIRELY regardless of
    roster; the page shows it read-only so an admin can tell "nobody is
    onboarded" from "it is switched off in this environment".

    ``narrows_admins`` marks a feature whose gate also requires the admin role,
    so its row only ever narrows WHICH admins get in. A failed
    ``feature_access`` read then admits nobody, not every admin
    (``policy.closed_policy``): the row may have left all but one team's admins
    out, and a read that failed cannot tell.
    """

    key: str
    label: str
    description: str
    surface: str
    group: str
    default: Callable[[], FeaturePolicy]
    membership: Membership = "primary_or_managed"
    module: Module = "core"
    kill_switch: str | None = None
    notes: str = ""
    narrows_admins: bool = False


# ── Code defaults ────────────────────────────────────────────────────────────
#
# What each feature ships with until an admin overrides it in Admin → Feature
# Access. There are two kinds and nothing in between:
#
# * OPEN — every signed-in person in the workspace (``everyone=True``). Every
#   user-facing feature ships this way; an admin narrows it by department. The
#   admin and exec flags still carry each feature's intent, so unticking
#   "Everyone" leaves the right doors open: a view of a person's OWN deals,
#   calls or CRM records keeps the exec door shut, because access to it
#   follows the work rather than seniority, and the Loose Ends mailbox scan
#   keeps both doors shut, so a flag never adds a mailbox to it.
# * ROLE-SCOPED — no department and no person: only the role flags open it.
#   Admin dashboards, manager views and exec roll-ups ship this way, and so
#   does every cross-person view, which opens on neither flag. An admin names
#   the departments (judged on the departments a person MANAGES for
#   ``managed_only`` features) or the people.
#
# No default names a department or a person: departments differ per company
# and are optional, and a person's address is never code.


def _open_default() -> FeaturePolicy:
    return FeaturePolicy(everyone=True, exec_allowed=True, admin_allowed=True)


def _open_by_the_work_default() -> FeaturePolicy:
    # Open to everyone. Narrowed to departments, seniority still does not open
    # it: the feature acts on a person's own deals, calls or CRM records.
    return FeaturePolicy(everyone=True, exec_allowed=False, admin_allowed=True)


def _smart_teams_default() -> FeaturePolicy:
    # Company-wide by design. Narrowed for a team-by-team rollout, neither flag
    # opens it: a dry run the Exec team can see early is a launch.
    return FeaturePolicy(everyone=True, exec_allowed=False, admin_allowed=False)


def _open_own_mailbox_default() -> FeaturePolicy:
    # Open to everyone. Being on it starts a morning read of the person's own
    # Gmail and Slack, so once an admin narrows it to a team, neither flag adds
    # anyone: an admin or an exec is scanned only as a member of that team.
    return FeaturePolicy(everyone=True, exec_allowed=False, admin_allowed=False)


def _admin_only_default() -> FeaturePolicy:
    # Admins only: no department, no exec flag. A manager or delivery-team view
    # of other people's work, or a support or facilitator tool, that the
    # workspace's admins hold by default. An admin adds the team that should
    # have it, or narrows it to the admins of one team by unticking Admins and
    # naming that team.
    return FeaturePolicy(exec_allowed=False, admin_allowed=True)


def _exec_and_admin_default() -> FeaturePolicy:
    # Built for the people who run the company and the people who run the
    # workspace: execs by flag, admins by flag, no department.
    return FeaturePolicy(exec_allowed=True, admin_allowed=True)


def _chosen_audience_default() -> FeaturePolicy:
    # Nobody until someone is chosen, by department or by name, and neither
    # flag opens it: a cross-person view, or a background import someone has
    # to decide to switch on.
    return FeaturePolicy(exec_allowed=False, admin_allowed=False)


#: The admin page's heading for the copilot's own features. One constant, so a
#: rename is one line.
_COPILOT_GROUP = f"{PRODUCT_NAME} copilot"

_COCKPIT_NOTES = (
    "The four cockpit views ship differently. My Day is open to everyone. Team "
    "Roll-up and Nudge Worklist start with admins only: add the sales team to "
    "Team Roll-up (judged on the departments a person MANAGES) and the delivery "
    "team to Nudge Worklist. Exec Roll-up opens on the Exec-team flag. The "
    "COCKPIT_ENABLED switch decides whether the cockpit runs at all; this page "
    "decides who may reach it once it does."
)

FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        key="copilot",
        label=f"{PRODUCT_NAME} copilot",
        description=(
            "The AI copilot: Daily Briefing, Chat, Productivity, Assets, Meeting "
            "Prep and Settings. Teams outside it see EOS only."
        ),
        surface=f"Today, My Work, Ask {PRODUCT_NAME} and Settings in the rail; all /api copilot routes",
        group=_COPILOT_GROUP,
        default=_open_default,
        notes=(
            "This is the outer door — every other feature on this page also "
            "requires the copilot unless it says otherwise (Smart Teams "
            "does). Opening the copilot to a team hands them Chat, "
            "Assets, Meeting Prep, Briefing and AI Priorities together."
        ),
    ),
    FeatureSpec(
        key="customer_xray",
        label="Customer X-Ray",
        description=(
            "Account health, churn risk and growth plays for a customer, with the "
            "emailed Deep Research PDF."
        ),
        surface="My Work → Sales → Customer X-Ray",
        group="Sales",
        default=_open_default,
        notes="A seniority-scoped view of an account, so the Exec team is admitted by flag.",
    ),
    FeatureSpec(
        key="crm_sync",
        label="CRM Sync",
        description=(
            "Proposed HubSpot contact and deal updates drawn from call "
            "transcripts, approved one item at a time."
        ),
        surface="My Work → Sales → CRM Sync",
        group="Sales",
        default=_open_by_the_work_default,
        kill_switch="CUBIE_CRM_SYNC_ENABLED",
        notes=(
            "Acts on one AE's own deals and transcripts, so access follows the "
            "work rather than seniority: narrowed to departments, the Exec team "
            "is NOT admitted by flag."
        ),
    ),
    FeatureSpec(
        key="sales_call_capture",
        label="Sales Call Capture",
        description=(
            "Today's client calls, live call notes and the post-call wrap-up "
            "that drafts HubSpot updates."
        ),
        surface="My Work → Sales → Today's Calls",
        group="Sales",
        default=_open_by_the_work_default,
        kill_switch="CCS_ENABLED",
        notes=(
            "Creates a recording-adjacent record of a client conversation, so "
            "access follows the people who host those calls: narrowed to "
            "departments, the Exec team is NOT admitted by flag."
        ),
    ),
    FeatureSpec(
        key="sales_meet_addon",
        label=f"{PRODUCT_NAME} in Google Meet",
        description=(
            "The call checklist inside Google Meet's side panel: the questions a "
            f"client call has to cover, for the products picked in {PRODUCT_NAME}, on "
            "screen while the AE talks."
        ),
        surface=f"Google Meet → Activities → {PRODUCT_NAME} (side panel)",
        group="Sales",
        default=_open_by_the_work_default,
        kill_switch="MEET_ADDON_ENABLED",
        notes=(
            "Ships with the Sales Call Capture roster so the two open together; "
            "change it here if a team should get the checklist in Meet without "
            f"call capture. It also needs the {PRODUCT_NAME} copilot row above: a team "
            "outside the copilot gets no answer in Meet either. This page decides "
            f"who {PRODUCT_NAME} answers inside Meet. The add-on still has to be installed "
            "for those people from the Google Admin console before it appears in "
            "their Meet at all."
        ),
    ),
    FeatureSpec(
        key="sales_cockpit",
        label="Sales Cockpit — My Day",
        description="An AE's daily list of commitments, breaches and next steps.",
        surface="My Work → Sales → My Day",
        group="Sales",
        default=_open_default,
        kill_switch="COCKPIT_ENABLED",
        notes=_COCKPIT_NOTES,
    ),
    FeatureSpec(
        key="sales_cockpit_manager",
        label="Sales Cockpit — Team Roll-up",
        description="A sales manager's view of their team's commitments, breaches and call notes.",
        surface="My Work → Sales → Team Roll-up",
        group="Sales",
        default=_admin_only_default,
        membership="managed_only",
        kill_switch="COCKPIT_ENABLED",
        notes=(
            "Judged on the departments a person MANAGES (Admin → Users → Team "
            "Manager), never their own team — every AE sits in Sales, and a "
            "primary-department match would make each of them a manager of "
            "their own team. Also opens team call transcripts."
        ),
    ),
    FeatureSpec(
        key="sales_cockpit_supervisor",
        label="Sales Cockpit — Nudge Worklist",
        description="The delivery team's list of commitments to chase, with one-click nudges.",
        surface="My Work → Sales → Nudge Worklist",
        group="Sales",
        default=_admin_only_default,
        kill_switch="COCKPIT_ENABLED",
        notes="Shows commitment metadata only, never call transcripts.",
    ),
    FeatureSpec(
        key="sales_cockpit_exec",
        label="Sales Cockpit — Exec Roll-up",
        description="Company-wide commitment counts and trend, with no per-person drill-down.",
        surface="My Work → Sales → Exec Roll-up",
        group="Sales",
        default=_exec_and_admin_default,
        kill_switch="COCKPIT_ENABLED",
        notes=(
            "The one cockpit view meant for the Exec-team flag, which is how it "
            "ships: three numbers and a trend, never a per-person breakdown."
        ),
    ),
    FeatureSpec(
        key="smart_teams",
        label="Smart Teams",
        description=(
            "The Work Style Interview, your own profile and guide, and "
            "everyone's published working guides."
        ),
        surface="Teams in the rail",
        group="Teams",
        default=_smart_teams_default,
        notes=(
            "Company-wide by design, and it ships open to everyone. Narrow it "
            "here to roll it out team by team; admins and the Exec team are "
            "then NOT admitted by flag, on purpose: a dry run the Exec team can "
            "see early is a launch. Always limited to accounts on the company's "
            "Workspace domain. Find People, Compare and Team Builder "
            "additionally need the copilot."
        ),
    ),
    FeatureSpec(
        key="smart_teams_support",
        label="Smart Teams support view",
        description=(
            "Lets the admins who support Smart Teams help a colleague with a "
            "bad score or a stuck interview: they can read that person's "
            "interview answers, scores, uploads and unpublished draft guide, "
            "and add or retire skills on the company's shared skill list."
        ),
        surface=(
            "Teams → a person's support view (answers, scores, uploads, "
            "skills, draft guide) and Teams → Curate skills"
        ),
        group="Teams",
        default=_admin_only_default,
        narrows_admins=True,
        notes=(
            "EVERYONE IT ADMITS MUST ALSO BE AN ADMIN. This row narrows which "
            "admins support Smart Teams; it never adds anyone else. Ticking "
            "Everyone, the Exec team or a department admits only the admins "
            "among them: interview answers are private to the person they "
            "describe, and seniority or managing a team must never buy them.\n\n"
            "IT SHIPS WITH EVERY ADMIN ON IT. To hand support to one team, "
            "untick Admins and name that team: only the admins in it (or who "
            "manage it) keep the support view.\n\n"
            "Smart Teams itself (the row above) has to be open to them too."
        ),
    ),
    FeatureSpec(
        key="scheduled_tasks",
        label="Scheduled tasks",
        description=(
            f"Let people schedule recurring {PRODUCT_NAME} work from Chat — a morning "
            "email round-up, a Monday recap of last week's meetings — "
            "delivered to their chat and their Slack DMs."
        ),
        surface=f"Ask {PRODUCT_NAME} (schedule it in chat) and Settings → Scheduled tasks",
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="SCHEDULED_TASKS_ENABLED",
        notes=(
            f"Each task is a full {PRODUCT_NAME} run every time it fires, charged to the "
            "org, so AI spend grows with how many tasks people keep — watch "
            "Admin → AI Cost → Scheduled Tasks, and narrow this row if it runs "
            "ahead. Runs read data and write drafts only; they can "
            "never send an email or book a meeting on someone's behalf. Also "
            "requires the copilot."
        ),
    ),
    FeatureSpec(
        key="meeting_skills",
        label="Meeting Skills (templates, skills, routines)",
        description=(
            f'Ask {PRODUCT_NAME} for any meeting in the shape you want — "my 10am as a '
            'BANT note", "the Acme call as pains, budget and next steps" — and '
            f"{PRODUCT_NAME} writes the outline and fills it from the transcript, every "
            "point quoting the line it came from. Keep an outline as a "
            "template, save a whole job as a skill you run by typing /name in "
            "the chat box, and have a skill run on its own after every meeting "
            "or on a schedule."
        ),
        surface=f"Ask {PRODUCT_NAME} (ask in chat, or type / in the chat box)",
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="MEETING_SKILLS_ENABLED",
        notes=(
            "Reads only meetings the person attended (the same participant list "
            f"Ask {PRODUCT_NAME} uses for transcripts), from the Google Meet / Gemini "
            "transcript Doc in their Drive, Call Capture or Fathom. Every point "
            f"must quote the transcript line it came from; {PRODUCT_NAME} checks each "
            "quote, owner, date and figure against the transcript and leaves out "
            "what it cannot find, and says so. Templates, skills and routines are "
            "private to the person who made them. Saving, changing or deleting "
            "one asks for a yes first. A routine can only leave a Gmail DRAFT, "
            "never send. Cost: one Flash call per write-up, larger for a "
            "many-meeting digest, tracked under Admin → AI Cost → Meeting "
            "Skills; a routine adds one call per matching meeting. Up to 25 "
            "templates, 25 skills and 10 routines per person. Routines on a "
            "clock are scheduled tasks, so the person also needs Scheduled "
            "tasks. Also requires the copilot."
        ),
    ),
    FeatureSpec(
        key="todos",
        label="My To-Dos",
        description=(
            "A personal list of what each person has promised to do, for whom "
            "and by when — added from a priority card, from Chat, from Slack "
            "/todo or typed on the page — grouped by who is waiting, with the "
            "next step one click away. Open items also surface in the daily "
            "AI Priorities until they are kept."
        ),
        surface=f"My Work → My To-Dos, Homefeed cards, Ask {PRODUCT_NAME}, Slack /todo",
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="TODOS_ENABLED",
        notes=(
            "Cost is small: one cheap Flash call to read a typed to-do into "
            "title / who / when, nothing on read. Team To-Dos in EOS are "
            "unaffected — this is the person's own list, and an item can be "
            "promoted to the team's L10 from it. Also requires the copilot."
        ),
    ),
    FeatureSpec(
        key="voice_input",
        label="Voice input",
        description=(
            f"A microphone in the Ask {PRODUCT_NAME} chat box: press it, say the "
            "question, and when the person stops (the button again, or Enter) "
            f"{PRODUCT_NAME} writes it down and sends it as their message. Names of "
            "colleagues and the company's own terms (L10, its products, "
            "HubSpot) come back spelled the way the company writes them."
        ),
        surface=f"Ask {PRODUCT_NAME} (the microphone beside Send)",
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="VOICE_INPUT_ENABLED",
        notes=(
            "Stopping the recording sends it: the dictated words go out as a "
            "chat message straight away (Esc discards instead). Only a stop "
            "the person makes sends; the two-minute limit, a change of "
            "conversation and an answer still loading leave the words in the "
            "box. A misheard question is asked as heard, so the person sees it "
            "in the conversation and can ask again. A recording nobody spoke "
            "in is never sent. The recording is never stored, and the words are "
            "kept only as that chat message. Cost is small: one Flash call per recording, "
            "about 1,900 audio tokens per minute of speech, capped at two "
            "minutes. Tracked under Admin → AI Cost → Voice input, where audio "
            "is priced at the cheaper text rate, so that figure reads low. A "
            "recording nobody spoke in never reaches the AI and costs nothing. "
            "Works in "
            "Chrome, Edge, Safari and Firefox; the browser asks each person "
            "once for microphone permission. Also requires the copilot."
        ),
    ),
    FeatureSpec(
        key="voice_commands",
        label="Voice commands",
        description=(
            f"Run {PRODUCT_NAME} by speaking: press the Voice button (or the keyboard "
            "shortcut, Option+Command on a Mac and Ctrl+Alt on a PC, which each "
            'person can change in Settings) and say what you want. "Open my '
            'work" opens the page; "Add a Rock called … for Q4" shows what '
            f"{PRODUCT_NAME} understood and adds it when the person says yes; a question "
            f"goes to Ask {PRODUCT_NAME}. It covers opening any page the person can already "
            "reach, opening one specific record by what it is about (a company's "
            "X-Ray, an L10, the prep for a meeting, a proposal, a past chat, a "
            "colleague's work style guide), and the EOS changes: add a Rock, "
            "update a Rock's status, add or finish a To-Do, add an Issue, and record "
            "a Scorecard number."
        ),
        surface="Every page (the Voice button above Notifications, and its shortcut)",
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="VOICE_COMMANDS_ENABLED",
        notes=(
            "Nothing changes until the person confirms: every EOS change is "
            'shown as a card first, and only Enter, a click or a spoken "yes" '
            "sends it. The change is then made exactly as the page's own button "
            "makes it, as that person, so it follows the same permissions. "
            f"{PRODUCT_NAME} can only open pages the person's own menu shows. Cost is "
            "small: one Flash call per command (a few seconds of audio), "
            "tracked under Admin → AI Cost → Voice commands, where audio is "
            "priced at the cheaper text rate, so that figure reads low. A "
            "recording nobody spoke in never reaches the AI. The recording is "
            "never stored. Also requires the copilot."
        ),
    ),
    FeatureSpec(
        key="chat_file_upload",
        label=f"Files in Ask {PRODUCT_NAME}",
        description=(
            f"A paperclip in the Ask {PRODUCT_NAME} chat box: attach a Word document, PDF, "
            "Excel or CSV file, PowerPoint deck or picture (or drop it on the box, "
            f"or paste a screenshot) and ask about it. {PRODUCT_NAME} reads it, including the "
            "words and charts in a picture or a scanned page, and answers from it. "
            "The file goes into that person's own private library, so they can ask "
            "about it again in a later conversation. Nobody else can see it: not "
            "their team, not their manager, not an admin."
        ),
        surface=f"Ask {PRODUCT_NAME} (the paperclip beside the microphone) and Assets → My private files",
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="CHAT_FILE_UPLOAD_ENABLED",
        notes=(
            "Private means the person who attached it, and nobody else. The files "
            "are not documents in the shared Library: they never appear in "
            "anyone else's Assets, in an admin's view of the Library, in a "
            "department's answers or in an offboarding hand-off, and an admin "
            f"viewing {PRODUCT_NAME} as someone cannot open them. When the private library "
            "is unavailable, a file is still read and used, but only in the chats "
            'it is attached to; the file says "This chat only". An answer that '
            "drew on someone's private files is not copied into the interaction "
            "log or the feedback text admins can read. When a person is "
            "offboarded or their account is deleted, their private files are "
            "deleted too, never handed to a successor. The owner can publish a "
            "COPY of a file to Assets (Assets → My private files → Publish), which "
            "needs the ordinary Assets upload permission and then follows the "
            "Library's own rules; the private file stays private. Up to 5 files "
            "per message, "
            "20 MB each, 100 new files per person per day. Older .doc, .xls and "
            ".ppt files must be "
            "re-saved as .docx, .xlsx or .pptx first. Each person can download "
            "or delete their files under Assets → My private files, and keeps "
            "that page even if they are taken off this roster. Cost: one Flash "
            "call per new file (a picture or a scanned PDF costs more than a Word "
            "file, because the model reads every page), tracked under Admin → AI "
            f"Cost → Files in Ask {PRODUCT_NAME} (reading attachments); attaching the same file again costs "
            "nothing. Also requires the copilot."
        ),
    ),
    FeatureSpec(
        key="promise_accountability",
        label="Promise follow-through in AI Priorities",
        description=(
            f"{PRODUCT_NAME} listens for the things a person says they will do (in "
            "meetings, L10s, the email they send and their Slack replies) and, "
            "once the date passes with no sign it happened, puts it in their "
            f"morning priorities: what they said, to whom, when, and what {PRODUCT_NAME} "
            "checked. Nothing is ever marked done on its own; the person "
            "confirms it."
        ),
        surface=(
            "Homefeed → AI Priorities ('You said you would…' cards), the "
            f"morning Slack DM, and the '{PRODUCT_NAME} heard you promise' tray on My "
            "To-Dos for teams that have it"
        ),
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="PROMISE_ACCOUNTABILITY_ENABLED",
        notes=(
            "Runs inside the overnight priorities batch, before the briefing "
            "is written. Each meeting, email batch and Slack batch is read by "
            "one cheap Flash call once, and never again; a second Flash call "
            "per person checks the promises that are past their date. Tracked "
            "under Admin → AI Cost → Promise follow-through. A promise is only "
            "ever heard from the person's own words, and only from meetings "
            "they were in."
        ),
    ),
    FeatureSpec(
        key="whats_new",
        label="What's New",
        description=(
            f"A short, plain-language release note about each change to {PRODUCT_NAME}, "
            "so people find out what is new and where to try it without anyone "
            "writing an announcement. RubikSoft writes each note once; people "
            "only see notes about features they can use."
        ),
        surface=(
            "The What's new button at the bottom of the sidebar, the drawer, and "
            "the spotlight bar on the Daily Briefing"
        ),
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="WHATS_NEW_ENABLED",
        notes=(
            f"The notes are the {PRODUCT_NAME} changelog, read-only here; nothing in this "
            "workspace drafts, approves or edits them. The optional weekday "
            "morning Slack post of new notes, and its channel, are set in "
            "Admin → What's New (posting ships off). Opens are counted as "
            "people per note, never who."
        ),
    ),
    FeatureSpec(
        key="proposal_generator",
        label="Proposal Generator",
        description=(
            f"After a sales call, {PRODUCT_NAME} drafts the short client proposal the "
            "prospect asked for: it reads the call and the HubSpot record, "
            "fills a brief the AE confirms (who it is for, scope, the outcomes "
            "the prospect stated, pricing), then produces a one-page branded "
            "PDF or Word draft the AE can revise by typing what to change. "
            f"Nothing is sent by {PRODUCT_NAME}; the AE sends it."
        ),
        surface="My Work → Sales → Today's Calls → wrap-up → Draft proposal",
        group="Sales",
        default=_open_by_the_work_default,
        kill_switch="PROPOSALS_ENABLED",
        notes=(
            "Reached from a call the person hosted, which also needs Sales "
            "Call Capture. Cost is bounded: one PRO call to read a transcript "
            "into the brief, one per generated version, one per revision — a "
            "handful of dollars per proposal; "
            "watch Admin → AI Cost → Proposal Generator after a new team is "
            "added. The pricing table is printed from the confirmed brief, "
            "never from the model's prose, so a number the AE did not confirm "
            "cannot reach a prospect."
        ),
    ),
    FeatureSpec(
        key="open_commitments",
        label="What's On Your Plate",
        description=(
            "The morning briefing answers 'what is waiting on me?' instead of "
            "'what is on my calendar?'. It names the messages someone is still "
            "waiting on an answer to — in both directions — the meetings and "
            "decisions nobody has come back to you on, the things you promised "
            "with a date on them, and the commitments whose date has quietly "
            "stopped being believable because the other side has gone silent. "
            "For Sales that is a deal whose close date has not been tested "
            "against three weeks of client silence. For every other team it is "
            "the same question about their own work — an audit report promised "
            "for a date, an approval nobody has given back, a number nobody "
            "filled in before the meeting that reads it."
        ),
        surface=(
            "Morning briefing — the Homefeed priorities card and the Slack morning DM"
        ),
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="OPEN_COMMITMENTS_ENABLED",
        notes=(
            "WHAT IT ADDS. A person's morning briefing includes four things it "
            "would otherwise leave out: a "
            "message they owe someone an answer on; a message they are still "
            "waiting on; a request nobody answered; and an overdue commitment, "
            "which would otherwise be quietly down-ranked for having no future "
            "date on it. Nothing new is collected about anyone — these are the same "
            f"mailboxes, calendars and Traction items {PRODUCT_NAME} already reads for "
            "them. It is a personal view only: nobody sees anyone else's "
            "plate, and there is no manager roll-up behind this key.\n\n"
            "IT ALSO NEEDS THE COPILOT. The briefing lives on the home page, "
            "whose audience is the copilot row: a team narrowed out of the "
            "copilot loses this card with it.\n\n"
            "WRITE EACH TEAM'S PLAYBOOK ROW (Admin → Priorities). The card "
            "shapes are the same for every team, but "
            "'high value' and 'gone quiet' are not: Sales measures silence in "
            "days, an audit team measures it against a 30-day document "
            "window. A department with no playbook row gets the default one, "
            "which is written in sales time and will read as slightly off for "
            "them.\n\n"
            "REMOVAL TAKES EFFECT THE NEXT MORNING, not immediately: the "
            "briefing is written overnight, and a card already written keeps "
            "rendering until the next run. Cost rides on the existing AI "
            "Priorities budget — no new AI call per card — so watch "
            "Admin → AI Cost → AI Priorities."
        ),
    ),
    FeatureSpec(
        key="close_date_rollup",
        label="Weekly close-date roll-up",
        description=(
            "One Slack message a week naming the deals whose estimated close "
            "date is hardest to believe — the date, who owns it, and how long "
            "it has been since anyone logged talking to the client. It is the "
            "pipeline review a sales leader does by hand, done once a week "
            "for the whole team at once, including the deals that are only "
            "starting to drift and the ones whose owner has left."
        ),
        surface="A weekly Slack DM to the named recipients",
        group="Sales",
        default=_chosen_audience_default,
        kill_switch="CLOSE_DATE_ROLLUP_ENABLED",
        notes=(
            "IT SHIPS WITH NOBODY ON IT. THIS IS A CROSS-PERSON VIEW. Everyone "
            "on this list sees other people's deals with their names against "
            "them. Add people by EMAIL, one at a time, and only people who are meant to be "
            "reviewing the team's pipeline — adding a department here would "
            "hand the whole department a weekly list of their colleagues' "
            "work.\n\n"
            "IT IS READ-ONLY. The message has no buttons and no link back "
            f"into {PRODUCT_NAME}, deliberately: the reps' own 'done' and 'not now' "
            "marks are shared per deal, so a manager acting on this message "
            "would silence the rep who owns it. Rows link to HubSpot.\n\n"
            "IT DOES NOT REPLACE THE REP'S OWN CARD. Anyone onboarded to "
            "What's On Your Plate already gets the same question about their "
            "own deals every morning; this is the same arithmetic, read "
            "across the team, once a week. A deal only starting to drift "
            "appears HERE and deliberately not on the rep's card — not "
            "everything is worth interrupting someone over.\n\n"
            "DEFERRED DEALS ARE LISTED, NOT RANKED. A deal a rep deferred (see "
            "'Defer a deal') is off that rep's own lists, never off this "
            "message: it appears in its own section with the owner, the "
            "follow-up date and the rep's note.\n\n"
            "NO AI COST. The ranking is arithmetic and the message is a "
            "template, so this feature makes no model call and adds nothing "
            "to Admin → AI Cost."
        ),
    ),
    FeatureSpec(
        key="sales_rhythm",
        label="Sales daily rhythm",
        description=(
            f"The sales team's daily operating checklist, built into {PRODUCT_NAME}: a "
            "morning, mid-day and end-of-day set of checks on each rep's own "
            "deals — which deals closing this month need action, which "
            "proposals are waiting on the client, which close dates keep "
            "slipping, which big deals hang on one contact, which lost deals "
            "are worth a check-in, and what needs tidying before they log off. "
            f"Each check is one click away as an Ask {PRODUCT_NAME} prompt, and on My Day "
            "as a short list with the next step on every row."
        ),
        surface=(
            f"Ask {PRODUCT_NAME} (a Morning / Mid-day / End-of-day row of starter prompts) "
            "and Sales Cockpit → My Day (the Daily rhythm section)"
        ),
        group="Sales",
        default=_open_by_the_work_default,
        kill_switch="SALES_RHYTHM_ENABLED",
        notes=(
            "PERSONAL VIEW ONLY. Every check reads the person's own HubSpot "
            "deals, matched by their email to a HubSpot owner; nobody sees a "
            f"colleague's list through this key. Someone whose {PRODUCT_NAME} address "
            "does not match a HubSpot owner gets a sentence saying so, not an "
            "empty list.\n\n"
            "IT ALSO READS THE PERSON'S OWN GMAIL AND CALENDAR, read-only, so a "
            "deal they emailed or met about without logging it does not read "
            "as quiet, and a booked meeting counts as a next step. From email "
            "only dates are fetched, plus — for the end-of-day recap — the "
            "recipients and subject of what they sent that day; from the "
            "calendar, meeting times, titles and invitees. Never anyone else's "
            "mailbox.\n\n"
            "THE MY DAY HALF ALSO NEEDS THE COCKPIT. My Day is behind 'Sales "
            "Cockpit — My Day' and the COCKPIT_ENABLED switch; this key adds "
            f"the Daily rhythm section to it. The Ask {PRODUCT_NAME} prompts need only "
            "this key and the copilot.\n\n"
            "NO NEW AI COST CATEGORY. The checks are HubSpot searches and "
            "arithmetic. When someone clicks a prompt, the answer is an "
            "ordinary chat turn and is counted under Chat and HubSpot "
            "retrieval in Admin → AI Cost, like any other question."
        ),
    ),
    FeatureSpec(
        key="sales_rhythm_team",
        label="Sales daily rhythm — team view",
        description=(
            "The same daily checks read across the whole sales team, for a "
            "leader: the team's top 10 deals closing this month that need "
            "action, the records the team touched today that are missing a "
            "key field, the proposals waiting on clients, the close dates that "
            "keep moving, and the big deals that hang on one contact."
        ),
        surface=f"Ask {PRODUCT_NAME} (the team version of the daily-rhythm starter prompts)",
        group="Sales",
        default=_chosen_audience_default,
        kill_switch="SALES_RHYTHM_ENABLED",
        notes=(
            "IT SHIPS WITH NOBODY ON IT. THIS IS A CROSS-PERSON VIEW. Everyone "
            "on this list sees other people's deals with their owners named. "
            "Add people by EMAIL, one "
            "at a time, and only people who review the team's pipeline — "
            "adding a department here would give the whole department a view "
            "of their colleagues' work.\n\n"
            "IT NEVER READS ANYONE'S EMAIL OR CALENDAR. It counts only what is "
            "logged in HubSpot, and every answer says so — a deal a rep has "
            "been emailing without logging it can show as quiet here.\n\n"
            "IT LISTS DEALS, NOT PEOPLE. There are no per-rep counts or "
            "rankings, and the team version leaves out colleagues' overdue "
            "tasks and their day's activity log on purpose: past-due close "
            "dates are a view of the pipeline, a colleague's to-do list is a "
            "view of the person.\n\n"
            "Someone on this list gets the team prompts instead of the "
            "personal ones, which suits a leader who owns no deals."
        ),
    ),
    FeatureSpec(
        key="deal_defer",
        label="Defer a deal",
        description=(
            f"A Defer button on every deal {PRODUCT_NAME} lists for a rep. A long-term "
            "deal with no real close date can be set aside until a follow-up "
            "date, with a note saying why. Until then it stops showing up on "
            "the rep's My Day, daily-rhythm checks and morning priorities card; "
            f"on the date it comes back by itself, and {PRODUCT_NAME} puts a follow-up "
            "task on the deal in HubSpot for that day. Managers still see every "
            "deferred deal, with who deferred it, until when and why."
        ),
        surface=(
            "Sales Cockpit → My Day (the deal rows, the Daily rhythm checks and "
            "the Deferred deals list) and the deal cards on What's On Your Plate"
        ),
        group="Sales",
        default=_open_by_the_work_default,
        notes=(
            "IT WRITES TO HUBSPOT. Each defer creates one follow-up task on the "
            "deal, assigned to the deal's owner and due on the follow-up date, "
            "with the note as the task body. It never changes the deal's stage "
            "or close date.\n\n"
            "MANAGERS ARE NOT HIDDEN FROM. The Team Roll-up and the weekly "
            "close-date roll-up list every deferred deal with its owner, date "
            "and note, so a defer cannot be used to bury a deal. Taking a team "
            "off this list removes the button; their open defers still count "
            "until the follow-up date.\n\n"
            "LIMITS. A follow-up date must be in the future and at most 180 "
            "days out, and a note is required. A rep can only defer a deal they "
            f"own in HubSpot or have an open {PRODUCT_NAME} commitment on.\n\n"
            "NO AI COST. Deferring is a date and a note; nothing here calls a "
            "model."
        ),
    ),
    FeatureSpec(
        key="loop_closure",
        label="Loose Ends",
        description=(
            f"Every weekday morning {PRODUCT_NAME} checks each account executive's own "
            "email and Slack against HubSpot and lists their loose ends: client "
            "emails and calls that never made it into HubSpot, clients still "
            "waiting on a reply after two business days, and promises or "
            "HubSpot tasks past their date. Each item has one real next step "
            "(log it in HubSpot, draft the reply, open the task) and clears "
            "itself when the work is actually done, so the pipeline record "
            "stays complete without anyone chasing it by hand."
        ),
        surface=(
            "Sales → Loose Ends (the AE's own list), the Loose Ends panel on "
            "My Day, the weekly 'Loose ends over 7 days' scorecard row and "
            f"Ask {PRODUCT_NAME} ('what loose ends do I have?')"
        ),
        group="Sales",
        default=_open_own_mailbox_default,
        kill_switch="LOOP_CLOSURE_ENABLED",
        notes=(
            "EVERYONE ON THIS LIST GETS A MORNING SCAN OF THEIR GMAIL AND "
            "SLACK. From email only who wrote to whom, when, the subject and "
            "Gmail's short preview are read — never a full message — and the "
            "subjects and AI summaries are shown only to the person they "
            "belong to. It ships open to everyone, so once "
            "LOOP_CLOSURE_ENABLED is on every person in the workspace is "
            "scanned; narrow it to the team that works client threads (or "
            "people by name) if that is who it is for. Once narrowed, admins "
            "and the Exec team are not included by their flags, on purpose.\n\n"
            "TEAM SURFACES NEVER SHOW MESSAGE CONTENT. The Team tab, the "
            "daily Slack digest, the scorecard and the team chat answer carry "
            "only the person, the kind of loose end, the client's name and "
            "how old it is.\n\n"
            "NOTHING CLOSES BY ITSELF UNLESS IT WAS DONE. An item clears when "
            "HubSpot or Gmail shows the work happened. The only manual exit is "
            "'Not a real miss', which is counted as noise on the Team tab.\n\n"
            "AI COST. One small AI call per person per morning (judging which "
            "client emails really need a reply), plus one per 'Draft a reply' "
            "click — counted under Loose Ends in Admin → AI Cost."
        ),
    ),
    FeatureSpec(
        key="loop_closure_team",
        label="Loose Ends — team view",
        description=(
            "The sales leader's view of the same lists: every scanned account "
            "executive, A to Z, with how many loose ends they have, how old "
            "they are, and which client each one is with — the aging list a "
            "leader brings to the L10, plus how often a flag turned out not "
            "to be a real miss."
        ),
        surface=f"Sales → Loose Ends → Team, and Ask {PRODUCT_NAME} (the team question)",
        group="Sales",
        default=_chosen_audience_default,
        membership="managed_only",
        kill_switch="LOOP_CLOSURE_ENABLED",
        notes=(
            "IT SHIPS WITH NOBODY ON IT. THIS IS A CROSS-PERSON VIEW. Everyone "
            "here sees other people's loose ends with their names against "
            "them. Sales managers qualify "
            "through the departments they MANAGE (Admin → Users → Team "
            "Manager), never their own team, so no AE becomes a viewer of "
            "their colleagues; anyone else is added by EMAIL.\n\n"
            "IT LISTS PEOPLE A–Z AND NEVER RANKS THEM. Every person is shown "
            "the same way; there is no 'worst AE'.\n\n"
            "IT NEVER SHOWS SUBJECTS, SNIPPETS OR AI SUMMARIES — only the "
            "person, the kind of loose end, the client's name and the age. "
            "Every read is written to the audit log."
        ),
    ),
    FeatureSpec(
        key="insights",
        label="Insights dashboards",
        description=(
            f"Dashboards that show what {PRODUCT_NAME} produces and how it is used: who "
            f"uses it and how often, how much people rely on Ask {PRODUCT_NAME} and how "
            "its answers are rated, and what AI Priorities caught and what "
            "people did with it. Every number can be filtered by department "
            "and date range and is shown with what it is out of, so leaders "
            f"can see where {PRODUCT_NAME} is working and where a team needs help."
        ),
        surface=f"Insights in the rail: Overview, Adoption, Ask {PRODUCT_NAME}, AI Priorities",
        group="Insights",
        default=_exec_and_admin_default,
        membership="managed_only",
        kill_switch="INSIGHTS_ENABLED",
        notes=(
            "WHAT A VIEWER SEES IS SCOPED BY DEPARTMENT. Admins and company-wide "
            "execs (Exec team with 'All departments') see every department. "
            "Anyone else sees the departments they MANAGE (Admin → Users → Team "
            "Manager) plus their own, and nothing outside them. A department "
            "added here is judged on the departments a person manages, never "
            "their own team, so adding 'Sales' gives Sales managers their team, "
            "not every AE a view of their colleagues.\n\n"
            "COUNTS ONLY, NEVER CONTENT. No dashboard shows a question, an "
            "email, a priority's text or anything said in a meeting. Call "
            "capture numbers appear per department only, and Smart Teams data "
            "never appears.\n\n"
            "Per-person tables and the person page are a separate row below "
            "(Insights — people view). Without it a viewer sees department "
            "totals only."
        ),
    ),
    FeatureSpec(
        key="insights_people",
        label="Insights — people view",
        description=(
            "Adds the per-person tables to the Insights dashboards and a page "
            "per person: their 90-day activity calendar, their numbers on each "
            f"dashboard, and the {PRODUCT_NAME} features they can reach versus the ones "
            f"they use, so a leader can see who is getting value from {PRODUCT_NAME} and "
            "who needs a hand."
        ),
        surface="Insights → people tables and the person page",
        group="Insights",
        default=_exec_and_admin_default,
        membership="managed_only",
        kill_switch="INSIGHTS_ENABLED",
        notes=(
            "THIS IS A FULL PER-PERSON DRILL-DOWN. Everyone on this list sees "
            "named people's activity in the departments they may see (the same "
            "scoping as the dashboards). It also needs the 'Insights dashboards' "
            "row above. Every person page that is opened is written to the "
            "audit log. Narrow this row, not the dashboards, if a leader "
            "should see team totals without names."
        ),
    ),
    FeatureSpec(
        key="page_tours",
        label="Page tours",
        description=(
            "The first time someone opens a page, a few short cards walk them "
            "through what it is for and how to use it, pointing at the parts "
            "of the page they describe. When a page gains a new feature, the "
            "people who already know the page get a card about just that "
            "feature the next time they open it. Every card can be skipped, "
            "and a tour can be replayed from the account menu."
        ),
        surface=(
            "Every page that has a tour (first visit, and after a new feature), "
            "the account menu (Replay this page's tour) and Settings → General "
            "(Page tours)"
        ),
        group="Help",
        default=_open_default,
        notes=(
            "No AI and no cost: the cards are written by RubikSoft and "
            f"shipped with the app. {PRODUCT_NAME} remembers, per person, which tours "
            "they finished or skipped, so a tour never repeats and follows "
            "them to another browser. Each person can stop tours from "
            "starting by themselves in Settings → General, or bring every "
            f"tour back. Tours never start while an admin is viewing {PRODUCT_NAME} as "
            "someone else, and the live L10 meeting page never starts one by "
            "itself. Open to everyone, EOS-only teams included: the EOS pages "
            "have tours too."
        ),
    ),
    FeatureSpec(
        key="eos_rocks_all_teams",
        label="Every team's Rocks (except the Exec team)",
        description=(
            "Lets the person who keeps the company's Rocks see and fill in "
            "every team's Rocks in one place, the way they do today in each "
            "team's rock-setting document: add Rocks for any team, update their "
            "status, milestones and weekly notes, and filter the Rocks page by "
            "any team. The Exec team's Rocks stay with the Exec team."
        ),
        surface=(
            "EOS → Rocks (every team in the Team filter and the new-Rock form), "
            "and the Rock detail view"
        ),
        group="EOS",
        default=_chosen_audience_default,
        module="eos",
        notes=(
            "IT SHIPS WITH NOBODY ON IT. ADD PEOPLE BY EMAIL, ONE AT A TIME. "
            "Everyone on this list sees "
            "every team's Rocks, personal (Individual) Rocks included, and can "
            "edit them. Adding a department here would hand a whole team that "
            "reach.\n\n"
            "THE EXEC TEAM IS NEVER INCLUDED. Rocks marked Exec Team stay "
            "visible to the Exec team only, and nobody on this list can mark a "
            "Rock as Exec Team unless they are on the Exec team themselves.\n\n"
            "ROCKS ONLY. It does not open other teams' Issues, To-Dos, "
            "Scorecards or L10 meetings, and it does not make anyone a Team "
            "Manager.\n\n"
            "NO DELETING OTHER PEOPLE'S ROCKS. Deleting a Rock stays with its "
            "owner, whoever created it, the owner's manager and the team's "
            "leadership. People on this list can delete the Rocks they created "
            "themselves.\n\n"
            "Admins and company-wide execs already see every Rock and do not "
            "need to be on this list."
        ),
    ),
    FeatureSpec(
        key="l10_meeting_restart",
        label="Restart a live L10",
        description=(
            "A Restart button on a live L10: everyone in the room jumps back to "
            "the top of the agenda with a fresh timer, and the notes and "
            "check-ins already captured are kept. For a false start, or a "
            "meeting run again for a demo."
        ),
        surface="EOS → Meetings (the Restart button on a live meeting)",
        group="EOS",
        default=_admin_only_default,
        module="eos",
        notes=(
            "IT SHIPS TO ADMINS ONLY. Add a team (judged on the people in it, "
            "or the teams they manage) or people by name to give their meeting "
            "leads the button.\n\n"
            "Whoever has it must still be able to run that meeting: its "
            "manager, the team's leadership, an admin, or whoever started "
            "today's session. The Exec team's L10 can never be restarted, "
            "whoever is on this list."
        ),
    ),
    FeatureSpec(
        key="chrome_extension",
        label=f"{PRODUCT_NAME} for Chrome",
        description=(
            "A Chrome extension that shows a person's day on every new tab: "
            "the promises they made and have not kept yet (overdue L10 To-Dos, "
            f"My To-Dos, and the commitments {PRODUCT_NAME} heard them make on calls and "
            "in email), then this morning's top priorities. Each line opens "
            f"the right page in {PRODUCT_NAME}, and a promise can be ticked off right "
            f"there. A small number on the {PRODUCT_NAME} icon counts what is past due. "
            "It is built to stay out of the way: no pop-ups, no sounds, and it "
            "can be shrunk to one line or hidden for the day."
        ),
        surface=(
            f"Chrome's new tab page and toolbar (the {PRODUCT_NAME} for Chrome "
            f"extension), {PRODUCT_NAME} → Connect {PRODUCT_NAME} for Chrome, and Settings → "
            f"General ({PRODUCT_NAME} for Chrome: connected browsers)"
        ),
        group=_COPILOT_GROUP,
        default=_open_default,
        kill_switch="CHROME_EXTENSION_ENABLED",
        notes=(
            "THE EXTENSION HAS TO BE INSTALLED TOO. Being on this list lets a "
            "person connect the extension; it does not put it in their Chrome. "
            "Until it is published privately on the Chrome Web Store and "
            "installed for a group by the Workspace admin, each person loads "
            "it by hand (docs/chrome_extension/README.md).\n\n"
            "WHAT EACH PERSON SEES DEPENDS ON THEIR OTHER ACCESS. Overdue L10 "
            "To-Dos show for everyone on this list. The priorities and the "
            f"promises {PRODUCT_NAME} heard need the {PRODUCT_NAME} copilot row above; the My "
            "To-Dos promises need the My To-Dos row. Someone on this list with "
            "only EOS access gets a new tab of their overdue To-Dos.\n\n"
            f"NO AI COST. The new tab only reads the priorities {PRODUCT_NAME} already "
            "made this morning; it never asks for new ones, however many tabs "
            "are opened. If this morning's list is not ready, the tab says so "
            f"and links to {PRODUCT_NAME}.\n\n"
            "DISCONNECTING. Each connected browser is listed in Settings → "
            "General and can be disconnected there. A connection lapses after "
            "90 days, and offboarding someone disconnects all of theirs."
        ),
    ),
    FeatureSpec(
        key="fathom_auto_ingest",
        label="Fathom auto-import on sign-in",
        description=(
            "When someone on this list opens the app (at most once an hour), the "
            "most recent call recordings in the company's Fathom account are "
            "imported into the shared meeting library, so the briefing and Chat "
            "can draw on them without anyone uploading a file."
        ),
        surface="Runs in the background when the person opens the app; no page of its own",
        group=_COPILOT_GROUP,
        default=_chosen_audience_default,
        notes=(
            "IT SHIPS OFF. Add the team that records client calls in Fathom, or "
            "people by name. It imports the company account's last 20 "
            "recordings each time it runs, not only the person's own. It needs "
            "the copilot too, and a Fathom API key connected to this workspace; "
            "without the key nothing is imported. The Fathom webhook, where configured, "
            "imports each call as it ends whoever is on this list — this is the "
            "catch-up when someone opens the app."
        ),
    ),
    FeatureSpec(
        key="ai_priorities",
        label="Overnight AI Priorities",
        description=(
            f"Each night {PRODUCT_NAME} reads a person's HubSpot deals, email, Slack, "
            "calendar, meeting transcripts and Drive files, and ranks the few "
            "things that most need them, weighed against the company's Rocks "
            "and goals. The list is waiting in their morning briefing; nobody "
            "has to ask for it."
        ),
        surface=(
            "Overnight run; the cards land in Homefeed → AI Priorities. No page "
            "of its own"
        ),
        group=_COPILOT_GROUP,
        default=_open_default,
        notes=(
            "Everyone in the workspace by default: the briefing is pushed to "
            "people, not fetched by the few who remember to look. Narrowed to "
            "departments, the Exec team and admins stay in by flag. Each person "
            "on it adds to the overnight run's model calls, tracked under "
            "Admin → AI Cost → AI Priorities and Signals Judge, so narrowing it "
            "is how to cut that cost: someone left out of it, or out of the "
            "copilot, has nothing read overnight and gets no card. A change "
            "takes effect from the next night's run. Also requires the copilot."
        ),
    ),
)

_BY_KEY: dict[str, FeatureSpec] = {spec.key: spec for spec in FEATURES}
assert len(_BY_KEY) == len(FEATURES), "duplicate feature key in FEATURES"


def feature_by_key(key: str) -> FeatureSpec:
    """The spec for ``key``; ``KeyError`` names the unknown key."""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"unknown feature {key!r}; register it in rubik_core/features/registry.py"
        ) from None


def feature_keys() -> tuple[str, ...]:
    return tuple(spec.key for spec in FEATURES)
