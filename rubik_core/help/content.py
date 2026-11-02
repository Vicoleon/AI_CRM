"""Cubie's own product help, as text the chat coordinator can retrieve.

WHY THIS FILE EXISTS
--------------------
Help is what users read: one page per topic at ``/help/<id>``, written in
``frontend/src/pages/help/topics/<Topic>.tsx`` and listed in
``frontend/src/pages/help/helpTopics.ts`` (the registry that drives the Help
panel, the Help home and the topic route). That is JSX and it is NOT shipped
into the backend container (the Dockerfile copies only the built
``frontend/dist``), so the agent cannot read it at runtime. This module is the
backend mirror: the same guidance in plain text, keyed by the same topic ids, so
``AgentTools.search_cubie_help`` can answer "how do I…" questions in Chat and
link the reader to the exact Help page.

KEEPING THE TWO IN STEP
-----------------------
The Help pages stay canonical for what users read; this file must cover the same
topics. ``backend/tests/test_help_chat_link.py`` fails if a topic is added to (or
renamed in, or re-gated in) ``helpTopics.ts`` without a matching entry here —
that test is the guard, so when you ship a feature and update its Help page,
update the matching ``body`` below in the same PR.
"""

from __future__ import annotations

from collections.abc import Collection

from rubik_core.brand import PRODUCT_NAME, SUPPORT_EMAIL, VENDOR_NAME
from rubik_core.help._retrieval import (
    HelpIndex,
    HelpSection,
    render_directory,
    render_sections,
)

# Route of the in-app Help home. Every topic is a page under it, so a section's
# link is "<path>/<section id>" — the `id` of the matching topic in
# frontend/src/pages/help/helpTopics.ts. (Links Chat wrote before topics became
# pages read "/help#<id>"; the Help home redirects those to the page.)
HELP_PAGE_PATH = "/help"


# ``HelpSection`` comes from `_retrieval`.
# On Cubie sections, ``requires`` is None or one of the gate names in the ``gates`` dict
# of ``rubik_core/adk_agent/tools.py::_help_allowed_section_ids`` ("xray", "crm_sync",
# "scheduled_tasks", "todos", "proposals", "voice_commands", "insights", "whats_new",
# "chrome_extension"). A name missing from that dict hides the section from everyone
# (fails closed), so add the gate there first.


def help_link(section: HelpSection) -> str:
    """In-app link to ``section``'s Help page — e.g. ``/help/chat``."""
    return f"{HELP_PAGE_PATH}/{section.id}"


HELP_SECTIONS: tuple[HelpSection, ...] = (
    HelpSection(
        id="getting-started",
        title="Getting started",
        keywords=(
            "login",
            "sign in",
            "connect",
            "accounts",
            "what is",
            "begin",
            "new user",
            # How people ask where things are. Both vocabularies on purpose: the
            # nav is a rail + panel now, but readers will keep typing "sidebar"
            # and "left menu" for a long time and must still land here.
            "navigation",
            "left menu",
            "sidebar",
            "rail",
            "panel",
            "menu",
            "getting around",
            "where do i find",
            "collapse",
            "help topics",
            "help panel",
            # Page tours ("Walk me").
            "tour",
            "page tour",
            "walk me",
            "walkthrough",
            "replay tour",
            "skip tour",
        ),
        body=(
            "Cubie is your company's AI assistant. It lives in your browser and answers "
            "questions about your work — pipeline, documents, meetings, email and calendar — "
            "in one place. You don't pick a data source: you ask in plain language, Cubie "
            "decides where to look, and it cites what it used.\n"
            "Most things work the moment you log in. Gmail, Calendar and Drive come "
            "automatically from your Google Workspace sign-in; Slack you connect once "
            "yourself in Settings.\n"
            "GETTING AROUND — the left edge has two parts. A narrow RAIL of icons lists "
            "the areas you can reach, top to bottom: Today, Work, EOS, Ask, Teams, "
            "Insights, Settings, Help, and Admin for admins. Click one and the PANEL "
            "beside it shows just that area's pages, so you only read the destinations "
            "that belong to where you are. Click the area you are already in to collapse "
            "the panel and give the page more room; click any icon to bring it back, and "
            "clicking that same icon again brings it back, while clicking a different icon "
            "takes you to that area. Cubie remembers which way you left it. An area with a "
            "single page (Chat) skips the panel altogether and gives the "
            "room to the page, since it would hold nothing but the name already under the "
            "icon. Administration is the opposite case: all four of its groups render in the "
            "panel, so the Admin page itself is just the section you picked. The rail shows "
            "only what your access opens, so a shorter rail is normal rather than a "
            "fault. Chat is under Ask; the scorecard, rocks, to-dos, issues and L10 are "
            "under EOS; the Work Style Interview and guides are under Teams; Help is "
            "Help.\n"
            "FINDING HELP — click Help in the rail and its panel lists every help topic, "
            "filed under the same headings as the app itself: Start here, Ask Cubie, My "
            "Work, Sales, EOS & Teams and Account. Each topic is a "
            "short page of its own; Help home at the top of the panel lists them all with a "
            "line on what each covers, and a search box to find one by name.\n"
            "PAGE TOURS — the first time you open a page, Cubie may walk you through it "
            "with a few short cards, each pointing at the part of the page it describes. "
            "Use Next and Back to move through them, or Skip tour (or the Esc key) to "
            "close it. A tour you finish or skip doesn't come back by itself, on this "
            "computer or any other. When a page you already know gets something new, a "
            "card about just that feature shows the next time you open it. To see a "
            "page's tour again, open the menu under your name at the bottom left and "
            "choose Replay this page's tour (during a live L10, where the menu is hidden, "
            "use Tour of this page under Exit meeting; that tour never starts by itself, "
            "so it can't interrupt the meeting). To stop tours from starting by themselves, "
            "or to bring every tour back, go to Settings -> General -> Page tours (without "
            "Settings, the same menu under your name has Turn off page tours)."
        ),
    ),
    HelpSection(
        id="daily-briefing",
        title="Daily Briefing (your home page)",
        keywords=(
            "home",
            "homepage",
            "priorities",
            "briefing",
            "cards",
            "snooze",
            "dismiss",
            "focus block",
            "urgency",
            "empty",
            "pto",
            "look-ahead",
            "drive label",
        ),
        body=(
            "Opening Cubie lands you on your Daily Briefing. It greets you by name and leads "
            "with AI Priorities — an auto-generated list of what needs your attention today, "
            "pulled from your email, calendar, meeting transcripts, HubSpot, Slack and "
            "recently-updated Google Drive documents you've labeled as important.\n"
            "Before showing a saved briefing, Cubie checks every HubSpot deal used to build "
            "it against your workspace's current access. If any deal loses access, the saved "
            "briefing stays hidden. Older briefings without enough source information stay "
            "hidden until a fresh one is built.\n"
            "The briefing is generated overnight. If the overnight run fails for your "
            "account, Cubie retries and, failing that, rebuilds the list when you open the "
            "page — a quiet day may show fewer items, but you're never handed an empty "
            "briefing while real work was waiting. The very first load (or a refresh) can "
            "take a couple of minutes while Cubie indexes; it updates itself when ready.\n"
            "Drive documents: mark a Drive file with a label your admin flagged as important "
            "(e.g. Important, Cubie, Project) and update it, and Cubie surfaces it here. It "
            "reads what's inside the file (Docs, Sheets, Slides, PDFs, Office files), not "
            "just the name. Only recently-touched labeled files appear. Admins manage which "
            "labels count under Administration → Drive Labels.\n"
            "EOS: your Rocks, To-Dos, Issues (IDS) and Rock milestones surface alongside "
            "email and calendar. Managers and leadership also see team members' at-risk "
            "items. Finishing something (a to-do, an issue, a milestone, a Rock) clears it "
            "from your Priorities and your manager's automatically. From a team member's "
            "card you can send a Slack follow-up — Cubie drafts the nudge and you preview "
            "and edit it before it sends; one nudge covers all of that person's late items, "
            "and the card steps aside for three days afterwards.\n"
            "Each card shows an urgency badge, why it matters, a suggested next step, and "
            "source badges. On a card you can:\n"
            "- Mark it done (✓) or dismiss it (✗). For work that FINISHES — a to-do, a "
            "rock, an email you answered — done is permanent: it won't come back on its "
            "own, so use ✗ or snooze for 'not right now'. For a situation that can come "
            "BACK — a deal whose close date has stopped looking "
            "realistic — done means 'handled for now' and Cubie re-checks in a couple of "
            "weeks, because the same deal can drift again in November after you fixed it "
            "in September. Everything you mark done, dismiss or snooze is listed under "
            "Settings → AI Personalization → Handled priorities, where you can reopen "
            "it.\n"
            "- A dismissed card stays away until something new actually happens on "
            "what it cites: a new reply on the email thread, a new message, a change "
            "to the To-Do, a client contact logged on the deal. A meeting time, a "
            "meeting starting or a due date does not bring it back, so dismissing the "
            "prep card for one meeting keeps it away for that meeting.\n"
            "- Two exceptions. Dismissing a 'close date at risk' deal card hides it for 90 days, and "
            "then Cubie checks the date again. A dismissed To-Do or Rock with nothing "
            "new around it stays away until you reopen it under Handled priorities.\n"
            "- On a recurring meeting, a calendar-with-an-✗ button means 'Never prep this "
            "again' (✗ alone only clears today's occurrence). It asks you to confirm, and it "
            "is undoable from Settings → AI Personalization.\n"
            "- Snooze it (tomorrow, in 3 days, next week, or a custom date).\n"
            "- On a card about one of your deals, 'Defer the deal…' sets the whole deal "
            "aside until a follow-up date you pick, with a note — wider than snoozing one "
            "card. Every card about that deal stays away until then, and Cubie adds a "
            "HubSpot task for that day (see Deferring a deal on the "
            "Sales cockpit page in Help).\n"
            "- Schedule a 30-minute focus block — Cubie finds a free slot.\n"
            "- Take the next action: open the deal in HubSpot, reply to an email, generate "
            "meeting prep, or draft a follow-up.\n"
            "'Get me other important things' surfaces more items, and the 👍 / 😐 / 👎 "
            "buttons record what's useful and guide future answers; older card text and "
            "comments are never replayed into a new answer. The strip at the top shows unread email, "
            "your next meeting, and free slots left today — click it to open Productivity.\n"
            "Cards know whose job it is: a meeting card names the person you're actually "
            "meeting (read off the invite), and an email you were only Cc'd on is shown as "
            "awareness of what someone else owes rather than a task on your plate. A CRM "
            "record silent for roughly three months is treated as probably out of date — "
            "never called urgent — and drops out of Priorities entirely past six months.\n"
            "Fridays add a next-week look-ahead, badged violet 'Next week' — prep you can do "
            "before Monday, including a nudge to align with your subject-matter expert "
            "before a prospect or client call. They respond to the same done / dismiss / "
            "snooze actions and drop off once that week starts.\n"
            "In Slack: if your department is enrolled (or you're on the exec team), Cubie "
            "DMs your top priorities every weekday morning with one-tap links. On Friday "
            "afternoons a second DM carries only the next-week look-ahead, under a header "
            "naming the week it covers. You never get the same morning list twice in one "
            "day. Admins turn it on per department under Administration → AI Priorities.\n"
            "How old is this list? Both the Slack DM and the AI Priorities card on your "
            "home page say when Cubie last compiled them — 'Compiled 6:04 AM EDT', in your "
            "own timezone — next to the Refresh button. The list is built overnight, so by "
            "mid-afternoon it can be several hours old; Refresh rebuilds it from scratch. "
            "The stamp is when Cubie finished thinking, not a claim that every mailbox and "
            "CRM record was re-read at that moment.\n"
            "What's On Your Plate: teams switched on for this (Administration → "
            "Feature Access) get a briefing about what is WAITING ON them, not only "
            "what is coming up — a message you owe someone an answer on, a message "
            "you are still waiting on, a request nobody came back to you about, and "
            "a commitment whose date has quietly stopped being believable because "
            "the other side has gone quiet. For a sales team that is a deal whose "
            "close date nobody has tested against three weeks of client silence; for "
            "an operations team it is the same question about a report "
            "promised for a date, or an approval nobody has given back. Nothing new "
            "is collected — the same mailboxes, calendars and Traction items Cubie "
            "already reads — and it is a personal view: nobody sees anyone else's "
            "plate.\n"
            "A quiet deal, or a quiet client? HubSpot only puts an email on a deal when "
            "the person you wrote to is on that deal; email someone else at the client "
            "and it is filed on the client's company instead. So a deal card reads both: "
            "when the company heard from us more recently than the deal shows, the card "
            "gives both numbers ('nothing logged on this deal in 82 days, though the bank "
            "was contacted 13 days ago') and weighs less than a deal whose whole client "
            "has gone quiet. Which deals get a card does not change, because that newer "
            "email may have been about a different deal with the same client.\n"
            "'You said you would…': Cubie listens for the things YOU say you will do "
            "— in meetings you were in, in your L10s, in the email you send and in "
            "your Slack messages — and, once the date passes (or a few days go by "
            "when no date was given), checks your sent email, Slack, Drive and EOS "
            "To-Dos for a sign it happened. If there is none, it shows up in your "
            "briefing with your own words, when and where you said them, exactly "
            "what Cubie looked at, and a link back to the meeting or message. It "
            "says 'no sign of it yet', never 'you didn't do it': calls and other "
            "tools are invisible to Cubie, so tick the card if it's done and it is "
            "marked kept. Only your own promises are heard, only from meetings you "
            "attended, and nothing is ever marked done on its own.\n"
            "Ask about it in Chat: 'What have I promised people this week?' or, if "
            "you manage people, 'Who did my sales team promise a follow-up, quote or "
            "resource to this week and hasn't sent it?' Cubie answers grouped by the "
            "contact who is waiting — who promised it, their words, when and where, "
            "the due date, and whether there is any sign it happened. A manager sees "
            "the promises of the people who report to them and the teams they lead; "
            "an org-wide exec sees every team. Cubie reads overnight, so something said today "
            "shows up tomorrow.\n"
            "The close-date roll-up: separately, a short weekly Slack message can "
            "go to whoever reviews the sales pipeline, listing the deals whose "
            "estimated close date is hardest to believe — the date, who owns it, and "
            "how long since anyone logged talking to the client, on the deal and on the "
            "client's company record. When the client heard from us more recently than "
            "the deal shows, the row gives both, and that deal is listed after the ones "
            "whose whole client has gone quiet. It is the review a "
            "sales leader does by hand, for the whole team at once, and it catches two "
            "things a rep's own briefing cannot: deals only starting to drift, and "
            "deals whose owner has left. Read-only — no buttons, and it links to "
            "HubSpot rather than back into Cubie, because the reps' own 'done' marks "
            "are shared per deal and a reviewer acting on the list would silence the "
            "rep who owns it. Recipients are named individually under Administration → "
            "Feature Access.\n"
            "Already answered it? A Slack question you've replied to — in its thread, or "
            "later in the same DM — is treated as handled and won't come back as a "
            "priority. Email works the same way: when the newest message in a thread is "
            "your reply to someone else's request (an 'action needed' note you answered, a "
            "decision you confirmed), Cubie counts that thread as done and only nudges you "
            "about a thread when YOU are the one still waiting on an answer. Only asks you "
            "haven't responded to make the list.\n"
            "On PTO, Cubie reads your calendar's out-of-office, holds your priorities and "
            "wishes you time off instead — surfacing something only if it genuinely can't "
            "wait. When an admin offboards someone, Cubie stops their daily run, hands "
            "their docs to a named successor and sweeps their open work into a handoff "
            "dossier — see the Handoffs page in Help. Inherited items then appear in the "
            "successor's daily priorities, marked 'Inherited from', until closed. A "
            "departed teammate whose account is merely deactivated is still dropped from "
            "the daily run and the team digest automatically.\n"
            "Before you go: in the week before an out-of-office block starts, Cubie puts a "
            "hand-off reminder at the TOP of your priorities. It lists what actually lands "
            "inside your absence — L10s you manage that meet while you're gone, to-dos and "
            "rock milestones due those days, rocks already off track, and scorecard numbers "
            "you're the one who enters — and suggests who could cover each one, drawn from "
            "your direct reports, your own manager and your teammates. Anyone whose own time "
            "off overlaps yours is left out of the suggestions. The reminder repeats each "
            "day until you leave, because the deadline is the day you go. Handing off the "
            "L10s matters most: once you're out, only your team's leader or an admin can "
            "re-point them (your team can also take one over — see EOS → Meetings)."
        ),
    ),
    HelpSection(
        id="chat",
        title="Chat",
        keywords=(
            "chat",
            "ask",
            "conversation",
            "session",
            "folders",
            "mention",
            "prompt",
            "citation",
            "sources",
            "chime",
            "draft email",
            "transcripts",
            "voice",
            "dictate",
            "microphone",
            "speak",
            "attach",
            "attachment",
            "paperclip",
            "upload a file",
            "picture",
            "image",
            "screenshot",
            "private files",
            "publish",
            "share a file",
            "how do i",
            "confirm",
            "approve",
            "approval",
            "yes or no",
            "outside your company",
            "template",
            "skill",
            "routine",
            "slash",
            "meeting summary",
            "meeting notes",
            "bant",
            "debrief",
            "action items",
            "write up a meeting",
        ),
        body=(
            "Chat is your conversational assistant. Ask in plain language and it picks the "
            "right source automatically. Conversations are saved in the list on the left; "
            "start a new one any time.\n"
            "Your workspace admin can choose a compatible AI provider for Chat in Admin → "
            "Workspace. Chat sends your prompt and the context it needs to answer to that "
            "provider. Chat's Gmail tools use your main work mailbox; connected extra "
            "mailboxes join your own AI Priorities and scheduled summaries instead. "
            "Chat web research has a separate model "
            "choice because it needs Google Search grounding. Pictures and built-in "
            "Google research require a compatible Google model. If the selected provider "
            "cannot answer, Chat shows a provider error. Ask your workspace admin to "
            "check the model and credentials in Admin → Workspace, then retry.\n"
            "An empty chat offers one-click starter prompts — click one and it asks it "
            "straight away. They are a random handful drawn from a much wider set, one "
            "per part of Cubie (your day, email, calendar, meetings, CRM, the knowledge "
            "base, your company's products, Slack, who's out, the web, and Cubie itself, plus "
            "scheduled tasks and Smart Teams where your team has them), so you keep "
            "meeting things you have not tried. You get a fresh set each time you start "
            "a new conversation, or click 'Show me other things to ask' to re-draw them "
            "on the spot. Prompts for features your team does not have never appear.\n"
            "Folders keep the sidebar tidy: create one with the folder-plus icon at the top "
            "of the list, then use the folder icon on any chat to file it (or make a new "
            "folder on the spot). Click a folder to collapse or expand it. Deleting a folder "
            "moves its chats back to Ungrouped — it never deletes them.\n"
            "Chat can answer about:\n"
            "- Company documents & the knowledge base — policies, products, procedures; "
            "answers cite clickable sources, and it can open a document by name.\n"
            "  To look one entry up inside a long document, name the document in your "
            "question — 'what's this client's acronym in the Client Acronym List' — and "
            "Cubie opens that file and reads the whole thing, not just the first pages. If "
            "it can only read part of a very long file, it tells you so instead of "
            "reporting the entry as missing.\n"
            "- Products — offerings, pricing, fulfillment.\n"
            "- CRM / HubSpot — deals, pipeline, sales activity.\n"
            "- Your Gmail — search, summarize unread, read a thread, and draft replies or "
            "new emails in your writing style.\n"
            "- Meeting transcripts — search and summarize past Google Meet / Fathom "
            "meetings, by name, most-recent, or topic. Ask for the FULL or COMPLETE notes "
            "and it returns the whole meeting, action items and all, instead of a summary.\n"
            "- Your calendar — list events, check availability, create events.\n"
            "- Who's out of office — PTO windows and return dates; it flags an attendee "
            "who's away when you schedule.\n"
            "- The web and your company directory.\n"
            "- Slack — recent messages, channel catch-ups, your @-mentions (connect Slack in "
            "Settings first).\n"
            "- What you're waiting on — ask 'what am I waiting on?' (no need to say email or "
            "Slack) and Cubie sweeps your recent Gmail threads and Slack mentions and DMs "
            "together, follows each one to its latest reply, and comes back with what needs "
            "your action (by priority, with who and when), what you're waiting on from "
            "others, and what it skipped because the thread was already closed or someone "
            "else has it. The footer says exactly what was checked; if Slack isn't "
            "connected it says so rather than showing an empty section.\n"
            "- How to use Cubie itself. Ask 'how do I…' or 'where is…' about the product — "
            "for example 'how do I add a document to the knowledge base?' — and Cubie "
            "answers from the Help pages and links you to the page it used.\n"
            "Approving actions: when Chat is about to do something rather than just answer "
            "(book a calendar event; save, change or delete a scheduled task) it stops and shows a card with "
            "exactly what will happen, taken from the action itself: the title and time, "
            "every person invited, the full text of a description, a task's "
            "instruction and schedule. Click 'Yes, go ahead' or 'No, cancel'; typing yes or "
            "no works too. A yellow warning flags what you might not expect, such as an "
            "invitation going to people outside your company. Cubie reads text other people wrote, like emails and documents, and "
            "that text can try to steer it, so read the card rather than Cubie's earlier "
            "message: the card is what will run. If anything on it is not what you asked "
            "for, say no.\n"
            "When an answer comes from your documents, a Sources panel sits underneath it — "
            "one card per document, naming the document, the folder it lives in, and a "
            "preview of the passage Cubie read. Open a card to see the exact wording it "
            "quoted; several passages from the same document are grouped on one card. Long "
            "lists show the first few, with 'Show all' for the rest.\n"
            "Type @ to mention a person or # to reference a Slack channel. The thumbs "
            "buttons under any answer give feedback. While Cubie works, a timer shows how "
            "long the answer has been running; the sound icon next to Send toggles a chime "
            "when a longer answer finishes.\n"
            "Voice input, where your team has it switched on: a microphone sits next to the "
            "Send button. Click it and say your question. To finish, click it again (it turns "
            "into a stop square while it listens) or press Enter: Cubie writes down what you "
            "said and sends it straight away, punctuated, with the um's and uh's left out. "
            "Anything you had already typed in the box goes out with it, in front of the "
            "spoken words. Press Esc while it listens, or while it is still writing it down, "
            "to throw the recording away and send nothing. While the mic is on it is the send "
            "button: the Send button and the starter prompts wait until it is done. Your words "
            "stay in the box instead of being sent, with a note saying why, when the recording "
            "reaches its two-minute limit, when you move to another conversation or change what "
            "is in the box while Cubie is writing it down, or when Cubie is still answering your "
            "previous question; press Send when you are ready. If what it heard isn't what you "
            "meant, just ask again. Shift+Enter still starts a new line, and Enter in an @mention "
            "list still picks the person. It spells colleagues' names and your company's own "
            "terms (its name and products, L10, HubSpot) the way the company writes them, and "
            "writes down whatever language you speak without translating it. A "
            "recording can run up to two minutes. If nobody spoke, Cubie says it didn't "
            "hear anything instead of guessing. The recording itself is never saved. "
            "The first time, your browser asks for permission to use the microphone; if "
            "you said no, allow it again in the "
            "browser's site settings.\n"
            "Files and pictures, where your team has them switched on: a paperclip sits beside "
            "the microphone. Attach a Word document (.docx), a PDF, an Excel or CSV file, a "
            "PowerPoint deck (.pptx), a text file or a picture (PNG, JPG, WEBP, GIF, HEIC); you "
            "can also drop files on the chat box or paste a screenshot into it. Up to five files "
            "per message, 20 MB each. Cubie reads each file as soon as you add it (a spinner shows "
            "while it works), including the words in a picture and what the picture shows, and a "
            "scanned PDF page by page; then ask your question, or send the file on its own and "
            "Cubie says what is in it. Your files are private to you: nobody else can see them, "
            "not your team, not your manager, not an admin. A file marked 'Private library' is "
            "kept in your own library, so you can ask about it again in a later conversation; one "
            "marked 'This chat only' could not be added to your library at the time, and Cubie "
            "still uses it in the conversation you attached it to. Attaching the same file again "
            "reuses what Cubie already read. See, download or delete your files under Assets → My "
            "private files. Deleting a file takes it out of your library, so no other conversation "
            "finds it; a conversation you attached it to keeps what Cubie read there until you "
            "delete that conversation. Removing a file from the chat box before you send also "
            "deletes it, unless you had attached it before. You can attach up to 100 new files a "
            "day. Older .doc, .xls and .ppt files need to be saved as .docx, .xlsx or .pptx first. "
            "To share one with your team, open Assets → My private files and click Publish (the "
            "share icon) on the file, then pick a topic and a department, or company-wide, the "
            "same choices as adding a Drive file. A copy goes into Assets for that department (or "
            "everyone) and Cubie can answer their questions from it; your private file stays "
            "private, and deleting it later does not remove the copy. A Word file, PDF, PowerPoint "
            "deck or text file goes in as itself; a picture, a scanned PDF or a spreadsheet goes in "
            "as a Word document with the picture or pages and what Cubie read from it, because the "
            "knowledge base can't search those directly. Publishing needs the same permission as "
            "uploading a document to Assets.\n"
            "Meeting write-ups, templates, skills and routines, where your team has them "
            "switched on. Ask for any meeting in the shape you want: 'my 10am as a BANT note', "
            "'the Acme call as pains, budget and next steps', 'my last meeting like a SOAP "
            "note'. Cubie writes the outline from what you asked and fills it from the "
            "meeting's transcript (the Google Meet or Gemini transcript in your Drive, a Call "
            "Capture call or Fathom). Every point ends in a number like [1]; the Sources panel "
            "shows who said it, roughly when (Google marks the time every few minutes, so "
            "times with a ~ are approximate) and their exact words. Cubie checks every quote, "
            "owner, date and figure against the transcript and leaves out anything it cannot "
            "find there, and the write-up says how many points it left out. Action items are "
            "grouped by owner, with a due date only if someone said one; an owner nobody "
            "named goes under 'Owner not clear'. Cubie only uses meetings you attended. "
            "A template is just the outline: say 'save that as my BANT template', or attach a "
            "form you already use and say 'use this as a template' (Cubie keeps its headings "
            "and their order). A skill is a whole job you run again: what to look for, the "
            "outline, the tone, how much detail, one meeting or every meeting from the last "
            "few days, and whether to also leave a Gmail draft for you or for the other "
            "attendees (Cubie never sends it). Say 'save that as a skill', then type / in the "
            "chat box to pick it: /client-debrief the Acme call. Saving under a name you "
            "already have makes a new version. A routine runs a skill on its own: 'after every "
            "client call, run /client-debrief' fires when the transcript is ready (only for "
            "meetings from then on); 'every Monday at 9, run /weekly-digest' is a scheduled "
            "task. Results arrive as conversations in your 'Scheduled tasks' chat folder and "
            "as a Slack DM. Saving, changing or deleting a template, skill or routine asks for "
            "your OK first. Ask 'what skills do I have?' to list them, or 'pause my debrief "
            "routine'. Your templates, skills and routines are private to you. A routine that "
            "fails several times in a row pauses itself.\n"
            "If Google's AI service stalls while Cubie is answering, Cubie switches to a "
            "second AI model once by itself, so a brief outage usually costs a few extra "
            "seconds instead of an error. If an answer still fails, the message says why. "
            "'Trouble reaching the AI service' means Google's AI was briefly unavailable: "
            "ask the same question again in a minute, there is no need to simplify it. 'Took too long' means the question "
            "itself needed more work than one answer allows, so splitting it up helps.\n"
            "Email is draft-only — Cubie NEVER sends on your behalf. Anything it writes "
            "lands in your Gmail Drafts for you to review and send."
        ),
    ),
    HelpSection(
        id="crm",
        title="Ask Cubie about CRM",
        keywords=(
            "crm",
            "hubspot",
            "deals",
            "pipeline",
            "win rate",
            "forecast",
            "revenue",
            "quota",
            "contacts",
            "cold",
            "last touch",
            "sales",
        ),
        body=(
            "Cubie reads your LIVE HubSpot pipeline, so CRM answers are current rather than "
            "a stale copy. Phrase questions however feels natural. Add a timeframe (this "
            "week, this month, last quarter, this year) or scope to a rep ('…for Maya', '…by "
            "Jordan'); no timeframe means all-time. Your workspace admin must connect HubSpot "
            "first. If that connection is removed, CRM answers pause until it is restored. "
            "Cubie reads deals only in this workspace's chosen pipelines; company "
            "and contact answers require a linked deal there. Prospect-only records "
            "without one are unavailable until the team links them in HubSpot.\n"
            "Live now: deals won, new deals created, win rate, average deal value, "
            "sales-cycle length, open pipeline value, meetings booked & held, new contacts, "
            "activities logged, turn times by product, deals expected to close (forecast), "
            "last touch with a client/contact/deal, deals going cold by outreach recency, a "
            "specific deal's stage/amount/owner, deal lists, and finding a contact.\n"
            "Not yet — each needs HubSpot set up first, and Cubie says it can't answer "
            "rather than guessing a number: ARR / recurring revenue (needs MRR & "
            "contract-term fields), churn rate (needs a churn date + active/churned status), "
            "upsell / cross-sell rate (needs a flag on deals), revenue per client (needs the "
            "deal-to-company link), quotes / proposals sent (HubSpot Quotes aren't used), "
            "stage conversion rate (needs the two stages defined), lead response time (needs "
            "first-activity tracking), forecasting score (no forecast field — ask 'expected "
            "to close' instead), follow-up ratio (needs its definition pinned down).\n"
            "How long since a deal's last contact reads the deal AND its client: HubSpot only "
            "puts an email on a deal when the person you wrote to is on that deal, and files "
            "it on the client's company otherwise, so when the company heard from us more "
            "recently than the deal shows, the answer gives both dates rather than calling "
            "the deal cold.\n"
            "Turn times by product read two deal fields your company created in HubSpot: "
            "how long a deal took to turn around, and what the deal was for. An admin tells "
            "Cubie once which fields those are; until then Cubie says turn times aren't set "
            "up rather than guessing.\n"
            "Cubie covers the CRM — deals, contacts, companies, meetings and activities in "
            "HubSpot — not invoicing or accounting. If a number looks off, it's reading "
            "HubSpot live, so it usually means the deal data in HubSpot needs a tidy-up."
        ),
    ),
    HelpSection(
        id="productivity",
        title="Productivity",
        keywords=(
            "productivity",
            "email tab",
            "unread",
            "calendar tab",
            "slack tab",
            "documents tab",
            "team folders",
            "knowledge",
            "drive",
            "add document",
            "tasks",
        ),
        body=(
            "The Productivity page gives full-page views of your day, with a Refresh button. "
            "Tabs:\n"
            "- Email — your unread mail ADDRESSED TO YOU: To: first, Cc'd mail tagged and "
            "below. Distribution-list and bulk/newsletter mail is hidden, and anything your "
            "own Gmail filters route out of the inbox stays out. Once you've read "
            "everything it shows 'You're all caught up', and likely-important messages carry "
            "a 'May be important' badge. If the selected AI provider cannot classify "
            "importance, the inbox still shows the messages with an AI triage unavailable "
            "status and no importance badges until the connection works again.\n"
            "If your workspace admin enables multiple Gmail accounts, choose a connected "
            "mailbox in the Email tab to see that inbox. Your work mailbox stays the "
            "default in Email. Connected mailboxes also join your own AI Priorities "
            "and scheduled summaries automatically, with a mailbox label on items that cite email; "
            "personal-mail items are kept out of manager digests. Connect or remove "
            "extra accounts under "
            "Settings → Email Preferences.\n"
            "- Calendar — upcoming events plus free-slot suggestions.\n"
            "- Slack — unread channels and the @-mentions & DMs aimed at you. Hover a "
            "channel for an on-demand AI summary of its unread messages (connect Slack in "
            "Settings).\n"
            "- Documents — your recent Google Drive files, plus a one-click way to add one "
            "to the knowledge base.\n"
            "- Team Folders — your department's shared documents.\n"
            "Why it opens instantly: as soon as your AI Priorities load on the home page, "
            "Cubie quietly gets your Email, Calendar, Slack and Documents ready in the "
            "background, so the Productivity tabs usually open with everything already "
            "there. Anything more than a minute old is refreshed automatically while you "
            "look at it (the Refresh button spins meanwhile), and Refresh pulls the latest "
            "straight away.\n"
            "The Documents tab shows files you've touched in Drive over the last week, your "
            "own plus anything shared with you (a small people icon marks shared files). It "
            "mirrors your Drive exactly — a personal document you haven't shared stays "
            "visible only to you. The Folders button narrows the list to specific Drive "
            "folders and remembers your choice.\n"
            "To add a document to the knowledge base: click '+ Knowledge' on the file's row, "
            "choose a Topic and Department (both required) and tick Company-Wide if everyone "
            "should find it, then confirm. Cubie pulls the file in — Google Docs, Sheets and "
            "Slides are converted to Word, Excel and PowerPoint automatically — the row "
            "switches to 'Added', and the document becomes searchable in Chat and listed on "
            "the Assets page.\n"
            "WARNING: adding a document to the knowledge base SHARES it with your team. "
            "Everyone in the Department you choose (or everyone, with Company-Wide) can then "
            "find it in Chat and on Assets, even if the original file is private in Drive. "
            "It also needs upload permission — if 'Upload a document' and 'Knowledge' are "
            "greyed out, your account doesn't have it; ask an admin to turn it on.\n"
            "Your Drive and Slack stay private by default: the Documents tab only shows "
            "files your own Google account can already open, and the Slack tab only the "
            "channels and DMs you're in. Nothing becomes visible to colleagues unless you "
            "deliberately add a Drive file to the knowledge base."
        ),
    ),
    HelpSection(
        id="assets",
        title="Assets & uploading",
        keywords=(
            "assets",
            "upload",
            "library",
            "knowledge base",
            "topic",
            "department",
            "company-wide",
            "transcripts",
            "sync",
            "ingest",
            "share",
            "pdf",
            "word",
            "tags",
            "version",
            "versions",
            "new version",
            "restore",
            "history",
            "what changed",
            "update a document",
        ),
        body=(
            "Assets is your document and file library — the same knowledge base Chat draws "
            "on. Its tabs:\n"
            "- Documents — uploaded knowledge documents (not meeting transcripts): a "
            "searchable, sortable list; filter by name, owner, topic, tag or description.\n"
            "- Transcripts — meetings you hosted or attended, with status and participant "
            "counts. Meeting transcripts live here, not under Documents.\n"
            "- My private files — files you attached in Ask Cubie, where your team has that "
            "switched on. Only you can see them (not your team, your manager or an admin), "
            "and they never join the shared knowledge base. Each shows what Cubie read, "
            "whether it is in your private library or for one chat only, and lets you "
            "download or delete it, or publish a copy to Assets for your department or the "
            "whole company (the share icon; it needs the same permission as uploading a "
            "document). The private file stays private, and the copy then behaves like any "
            "other document here.\n"
            "Documents load a page at a time. Use 'Load More Assets' to keep browsing. "
            "When Customer X-Ray brief access still needs checking, the count can say more "
            "files may be available until the next page is checked; it never counts a brief "
            "Cubie cannot confirm belongs to your workspace.\n"
            "Documents Cubie generated for you (Meeting Prep and Customer X-Ray briefs saved "
            "via 'Save to Assets') appear under Documents with a 'cubie-generated' tag. Tick "
            "one or more rows and an action bar appears: download, share with a "
            "teammate (they see it on their Assets page with a 'Shared with you' badge plus a "
            "notification), or draft a Gmail with the file attached (the draft opens in your "
            "own Gmail for you to review and send). Generated briefs are deliberately kept "
            "out of Chat's knowledge base so Cubie never cites its own write-ups as "
            "evidence.\n"
            "A saved Customer X-Ray brief appears only while Cubie can confirm that the "
            "customer's HubSpot deals belong to your workspace. Older briefs without a "
            "confirmed customer link may be unavailable until a new brief is saved. "
            "Saved Meeting Prep briefs follow the same rule for the client records used "
            "to build them; a brief becomes unavailable if that access changes.\n"
            "To add a file, use 'Upload a document': pick a PDF, Word, text or Markdown "
            "file, set its Topic and Department (both required), and optionally add a "
            "description and tags. Tick Company-Wide if everyone should find it; otherwise "
            "the Department controls who can.\n"
            "You can also bring files in straight from Google Drive with the '+ Knowledge' "
            "button on Productivity → Documents — the same Topic / Department / Company-Wide "
            "rules apply.\n"
            "UPDATING A DOCUMENT YOU ALREADY UPLOADED (versions). When a policy, price "
            "sheet or deck gets a new edition, do not upload it as a second document: tick "
            "the row, click 'Versions' in the action bar, and upload the new file as a new "
            "version. The document keeps its place in the list, its shares and everyone's "
            "bookmarks — only the file changes — and Chat switches to the new edition once "
            "it is indexed (it keeps answering from the previous one until then). Every "
            "earlier version stays in the history: download any of them, or 'Restore' one "
            "to make it current again (a restore is recorded as a new version, so nothing "
            "is overwritten). For each new version Cubie compares the old and new files and "
            "writes a short 'What changed' note — sections added or removed, numbers, dates "
            "and names that moved. You can add your own one-line note too. Documents with "
            "more than one version show a 'v2', 'v3'… badge next to their name; click it to "
            "open the history. The owner, an admin, or an editor for the document's "
            "department can add and restore versions; anyone who can read the document can "
            "see and download its history. Archived documents, meeting transcripts and "
            "Cubie-generated briefs do not take versions.\n"
            "Meeting transcripts ingest AUTOMATICALLY (about every 15 minutes) — you never "
            "upload them yourself.\n"
            "Two places share the name 'Documents': Productivity → Documents is a live, "
            "private view of your own Google Drive; Assets → Documents is the shared "
            "knowledge base Chat answers from. A file only moves from the first to the "
            "second when you add it with '+ Knowledge'."
        ),
    ),
    HelpSection(
        id="eos",
        title="EOS",
        keywords=(
            "eos",
            "traction",
            "l10",
            "level 10",
            "rocks",
            "scorecard",
            "issues",
            "ids",
            "to-dos",
            "todos",
            "milestones",
            "measurable",
            "processes",
            "fba",
            "quarter",
            "carry over",
            "carried over",
            "next quarter",
            "vto",
            "import",
            "milestone",
            "solve",
            "sticky header",
            "cell note",
            "note",
            "notes",
            "bullets",
            "bullet points",
            "4 week average",
            "13 week average",
            "rolling average",
            "headlines",
            "covered",
            "parking lot",
            "check-in",
            "not syncing",
            "resync",
            "search issues",
            "recap",
            "recap email",
            "meeting recap",
            "meeting notes email",
            "recording",
            "recorded meeting",
        ),
        body=(
            "EOS (the Entrepreneurial Operating System) is the operating cadence your "
            "company runs on, built into Cubie. Open EOS in the left rail for:\n"
            "SCORECARD — weekly numbers against target goals, grouped by owner, each value "
            "in its natural format (currency, percent, count) and green or red against its "
            "goal. Give a measurable a description and it shows as a tooltip on hover. Click "
            "any week to edit it; 4- and 13-week averages update automatically. Percent "
            "measurables are typed as WHOLE percents (50 for 50%, 9.25 for 9.25%) in the "
            "grid, the L10, and targets; while you set a target the dialog spells out what "
            "it will mean ('colours a week green when it's >= 98%'), so a percent can't be "
            "off by a factor of 100 without you seeing it. A measurable's GOAL reads the "
            "same everywhere — the same '> 98%' in the grid's Goal column and on its card "
            "in the L10 — and in the meeting a week is coloured against the goal's "
            "DIRECTION, so on a lower-is-better measurable like 'late reports < 2%' a good "
            "week is the green one. If you sit on more than one team, a Team selector "
            "switches between the Executive View scorecard and your department's. The "
            "confidential Leadership Scorecard is visible only to the Exec team, leadership, "
            "and each metric's owner. WHO CAN ENTER THE NUMBERS: anyone on a measurable's "
            "team can type its weekly numbers and notes, on the Scorecard page and in the "
            "L10, so one person can fill in the whole team's week. Changing the measurable "
            "itself (name, goal, owner, team) or deleting it stays with its owner, the "
            "owner's manager and the team's leadership; the Leadership Scorecard stays with "
            "the Exec team and each measurable's owner. If a number can't be saved, the "
            "message says why instead of just 'Failed to save'. A team that meets every other week reads its "
            "scorecard by its meetings: only its real periods show (no alternating blank "
            "weeks), each column is dated by the team's meeting day, and the 4- and "
            "13-period averages count meetings rather than calendar weeks. "
            "Measurables can be reordered: hover one and drag the grip handle on the "
            "left to move it up or down within its owner's block, so the grid follows "
            "the order your team reads them in rather than alphabetical. The order is "
            "saved, shared, and shows the same way inside the L10; you can only reorder "
            "measurables you could already edit. "
            "Import: download the Excel template (a blank sheet, "
            "one column per week, dropdowns for owner and team), type one row per "
            "measurable, and upload it back. Existing name+owner+team rows are updated in "
            "place; anything else is added (and the import tells you which). Blank cells are "
            "left alone — an import never clears a number. It applies only if EVERY row "
            "checks out, and only for measurables and teams you could already edit. The "
            "template contains NO data. The grid has a sticky header: week columns stay in "
            "view as you scroll down, Owner/Measurable/Goal stay put as you scroll right. "
            "The 4-WEEK AND 13-WEEK AVERAGES ARE PINNED TO THE RIGHT EDGE the same way, so "
            "they stay on screen at every scroll position — you no longer scroll to the "
            "far right to read them and lose sight of which measurable you were on. The "
            "page uses the full width of a large monitor rather than a fixed column, so "
            "more weeks fit before anything scrolls; inside the L10 the Scorecard, Rocks, "
            "To-Dos and IDS sections get the whole workspace too. "
            "The 4-week and 13-week averages colour green or red against the goal like any "
            "other cell. Any cell can carry a NOTE — what the number is made of ('3 "
            "renewals, 2 upsells'), the spreadsheet cell comment back again: click a cell and "
            "type the note under the value. Drag the corner of the note box to make it "
            "bigger for a longer note. A cell with a note shows a small dot; hover to "
            "read it. Notes are visible to everyone who can see the measurable. Inside the "
            "L10 the Scorecard now shows this SAME grid (the full quarter, newest week "
            "first) instead of one card per measurable, with a Grid/Cards switch that "
            "remembers your choice. Only the CURRENT week is editable in the meeting — "
            "correcting an earlier week is a Scorecard-page job. Weeks nobody has filled in "
            "show as empty columns rather than disappearing. Colouring needs BOTH halves "
            "of the goal — a Compare direction AND a Target number. The Goal column shows "
            "the goal's LABEL, so a measurable can read as having a goal ('> 98%') and "
            "still colour nothing if its Target was never filled in; the Goal column now "
            "flags EITHER missing half with a small warning that says which one, and the "
            "measurable dialog warns you before you save. Where the label already reads as "
            "a goal on its own ('> 98%', '<10,000 usd'), Cubie fills the missing half in "
            "from it, so a measurable that shows a goal gets coloured.\n"
            "ROCKS — quarterly priorities with owners, milestones and status, in one list "
            "grouped by team, with a 'By team / By person / By status' toggle (also inside "
            "the L10 Rock Review). A health summary shows how many are On track, Off track "
            "and Done. Each row shows milestone progress as a done-out-of-total count and "
            "the next upcoming milestone date, and expands to reveal all milestones inline. "
            "Long rock names wrap rather than truncate. The page opens on the current "
            "quarter (or the most recent quarter with rocks); switch with the filter at top "
            "right. New rocks default to Department visibility — visible to everyone on that "
            "team, and always to the owner. A Department rock saved without a team takes its "
            "OWNER'S team, so it shows under that team on the Rocks page and in that team's "
            "L10 alike (rocks saved before this rule were filed the same way). When the Team "
            "filter is hiding rocks from the period on screen — other teams', company-wide — "
            "the page says how many and offers 'Show all teams'. The Exec team can tick "
            "'Exec Team Rock' to keep one visible to the Executive team only; an Exec Team "
            "Rock never carries a department. WHO CAN CHANGE A ROCK: anyone on the "
            "Rock's team can update it — edit it, tick a milestone, log the weekly "
            "update — because Rock Review is something the team does together in the "
            "L10, not a queue of edits waiting on each owner. DELETING a Rock is "
            "narrower: removing the team's commitment stays with the Rock's owner, "
            "whoever created it, the owner's manager and the team's leadership. A Rock "
            "belonging to ANOTHER team opens read-only, with the controls hidden rather "
            "than failing when you save. Executive-team Rocks and someone's personal "
            "(Individual) Rock are never opened up by this. KEEPING EVERY TEAM'S ROCKS: "
            "whoever writes up the company's rock-setting sessions can be given every "
            "team's Rocks in one place (Admin → Feature Access → 'Every team's Rocks "
            "(except the Exec team)', granted by name). They see every team's Rocks, "
            "personal ones included, can pick any team in the Team filter and when adding "
            "a Rock, and can update any of them. The Exec team's Rocks are never part of "
            "it; deleting stays with the people above (they can delete the Rocks they "
            "added themselves); and it covers Rocks only, not other teams' Issues, "
            "To-Dos or Scorecard. CARRYING A ROCK OVER TO NEXT "
            "QUARTER: at quarter end an unfinished Rock is either dropped or carried into "
            "the next quarter, usually re-dated or re-scoped. On the Rocks page, a Rock "
            "that isn't Done has a 'Carry over' button — on its row when you hover, in its "
            "expanded panel and in its detail view. It opens the Rock as a DRAFT for the "
            "next quarter (the quarter after the Rock's own, or the current quarter if "
            "that's later) with the same description, owner, team and visibility, status "
            "back to On Track, and ONLY the milestones not yet done — a milestone date "
            "that falls before the new quarter is cleared. Everything is editable before "
            "you save, the quarter included. 'Update for Q1 2027 with AI' (named for "
            "whichever quarter you're carrying it to) re-dates the Rock for that quarter "
            "and turns the remaining work into milestones dated inside it — a draft you "
            "edit; nothing is saved until you save. Saving creates a NEW Rock in that "
            "quarter and leaves last quarter's Rock EXACTLY AS IT WAS: its status, "
            "milestones and weekly updates stay as the record of what was and wasn't "
            "done. Both Rocks then say so — 'Carried over to …' on the old one, 'Carried "
            "over from …' on the new — and clicking that label jumps to the other "
            "quarter. A Rock can be carried over ONCE; delete the new one to carry it "
            "again. Anyone who can update the Rock can carry it over (the same rule as "
            "editing). It lives on the Rocks page, not inside the L10.\n"
            "TO-DOS — short, 7-day action items with an owner and due date. A summary shows "
            "Open, Due this week, Overdue and Completed above one list grouped by due date, "
            "with overdue items highlighted. The same list appears inside the weekly L10. "
            "Every to-do takes notes: hover a row and click the speech-bubble icon. Anyone "
            "who can see the to-do can add one; each note is stamped with author and time, "
            "and a row with notes shows a '2 notes' link. The note box is several lines "
            "tall, grows as you type and can be dragged taller by its corner, and it takes "
            "BULLETS: start a line with '-' (or select lines and click Bullets) and Enter "
            "carries the list on; Enter on an empty bullet ends the list. Enter is a new "
            "line, so save with Cmd/Ctrl+Enter. Saved notes read back as real bullets. The "
            "same box is used for IDS ISSUE NOTES, where the discussion is usually a "
            "list. A Team filter appears if you sit "
            "on more than one team; Exec-team to-dos carry a violet Exec badge. A to-do is "
            "ONE LINE — the text field stops at 120 characters, and to-dos created from an "
            "Issue, the Slack /todo command or a parked idea are trimmed to fit. If it needs "
            "more than a line it's an Issue, which has a full description and a detail view. "
            "Long to-dos already on the list wrap to two lines with the full text on hover "
            "instead of being cut off mid-word.\n"
            "ISSUES — the IDS list (Identify, Discuss, Solve), grouped by priority with an "
            "at-a-glance summary (open, solved this week, solve rate, average age). Each "
            "issue has a short title in the list; click it for a read-only view with full "
            "details, and edit from the pencil icon on hover. Rows show who raised it, the "
            "date, and the owner. Solving an issue is a single click on the circle at the "
            "left of its row. You can turn an issue into one or more to-dos assigned to "
            "specific people — the issue stays open and can't be solved until every one of "
            "its to-dos is done. Team filter and Exec badge work as they do for To-Dos. "
            "There is a SEARCH box on the Issues list and inside the L10's IDS step; in a "
            "meeting it searches SOLVED issues too, so 'we talked about this last week' can "
            "be found without leaving the meeting. SOLVED ISSUES STAY VISIBLE IN THE L10: "
            "the IDS step shows what the team has already solved in a Solved section under "
            "the open list, with LAST 30 / 60 / 90 DAY buttons to widen the lookback (the "
            "Issues page has the same buttons plus All). Solve an issue during the meeting "
            "and it moves straight into that section, and the 'Solved this week' and "
            "'Solve rate' figures at the top of the step count it — previously the meeting "
            "only ever held the OPEN list, so those two read 0 no matter how many issues "
            "the team solved in the room.\n"
            "L10 — the weekly meeting itself. Everyone follows the facilitator: they move the "
            "agenda on and every other screen follows. If your screen stops keeping up you "
            "see it — a 'Not syncing' strip appears with a 'Resync now' button, and when the "
            "room is ahead of you a 'Jump to…' button takes you to the section everyone else "
            "is on, so you never have to refresh to find out. While a meeting is open the "
            "LEFT NAVIGATION IS HIDDEN so a stray click can't drop you out mid-agenda; leave "
            "deliberately with 'Exit meeting' in the meeting's own sidebar. The CHECK-IN is "
            "optional and has two independent halves: pick a 0-10 for how your week is "
            "going, add a line of good news, or both — whichever you fill in is what gets "
            "saved. The button reads 'Save' once there is something to save and 'Skip' when "
            "there isn't (which records you as attended and nothing else). HEADLINES now "
            "appear live: a teammate's headline shows on your screen within a couple of "
            "seconds. They read NEWEST FIRST, so the one just added is at the top rather "
            "than buried at the bottom, and checking one off MOVES IT OUT OF THE LIST into "
            "a collapsed 'Covered this meeting' section — still there if you need it, and "
            "un-checking puts it straight back. The tab count is what is left to cover, "
            "not the all-time total. Pre-meeting prep on the Meetings page is ordered the "
            "same way. The old 'Ideas' tab is now the PARKING LOT — for things not ready to "
            "act on. It is NOT the IDS list: issues to solve this meeting go in the IDS "
            "step, now titled 'IDS — Issues' so the two can't be confused. Every clock in "
            "a meeting is YOUR clock: 'Session started' in the footer, and the timestamps "
            "on notes, are shown in your own timezone and named ('11:01 AM CST'), so a team "
            "spread across timezones reads one moment the same way instead of a UTC clock "
            "nobody actually sits in. A meeting's weekly start time is in THE COMPANY'S "
            "BUSINESS TIME ZONE: the time on the invite it was imported from, or the time "
            "typed when it was scheduled in Cubie. Cubie used to read it as UTC, so voice "
            "'open my L10' named an 11:00 AM meeting as 6:00 AM, and the wrong meeting "
            "could come up as your next L10. Both now use the real start time, shown on "
            "your own clock. If your Google Calendar is set to a timezone other than "
            "the company's, check the time on the card after you import.\n"
            "RESTARTING A LIVE MEETING — after a false start, or to run a meeting again "
            "for a demo, 'Restart' on a live meeting's card takes everyone back to the "
            "top of the agenda with a fresh timer; notes and check-ins already captured "
            "are kept. Admins have it by default, and an admin can give it to other "
            "people or teams in Admin → Feature Access (Restart a live L10). You still "
            "have to be able to run that meeting (its manager, the team's leadership, "
            "an admin, or whoever started today's session), and the Exec team's L10 can "
            "never be restarted.\n"
            "IF YOU CANCEL AN L10 ON YOUR GOOGLE CALENDAR — the meeting card follows the "
            "calendar. Cubie used to keep counting the weekly slot forward forever, so a "
            "cancelled L10 went on promising a 'Next: …' date that was never going to "
            "happen. Now Cubie checks each calendar-imported meeting against Google "
            "OVERNIGHT, EVERY NIGHT — and again whenever you open EOS → Meetings: "
            "one whose event has been cancelled "
            "is flagged 'Cancelled on calendar', its schedule line reads 'No upcoming "
            "occurrence', and Start is switched off. It also stops being named as your next "
            "L10 in reminders and in the hand-off list when you book time off. A meeting "
            "that MOVED is handled the same way — the card shows the date Google actually "
            "has, not the old weekly slot. Put the event back on the calendar and click "
            "'Sync' to bring the meeting back, or delete it in Cubie when the team is done "
            "with it. The invite is also where the Check-in attendee list comes from: if "
            "it includes a Google Group (a distribution list, like the Exec team's), Cubie "
            "lists the people in that group rather than the group's address, so everyone "
            "invited through it can be picked at Check-in; someone added to the invite "
            "later shows up after the next Sync. Two things worth knowing: only the meeting's OWNER (the calendar "
            "organizer) changes this for everyone — if you remove your personal copy of "
            "somebody else's meeting, your own card corrects itself and nobody else's "
            "changes; and if Google can't be reached, Cubie leaves the schedule exactly as "
            "it was rather than announcing a cancellation it isn't sure about. You do "
            "not have to open Cubie for any of this — the overnight check means a "
            "meeting you cancelled on your calendar has stopped being promised by the "
            "next morning, including in the reminders and the time-off hand-off list "
            "you would never think to go looking at.\n"
            "Open a past meeting's History to read its Cubie recap. Your workspace admin "
            "can choose the AI provider for this on-page recap separately from the recap "
            "email. If that provider cannot answer, Cubie shows a retryable error and "
            "keeps any earlier recap.\n"
            "RECAP EMAIL AFTER A RECORDED L10 — if your L10 was RECORDED (Google Meet "
            "notes by Gemini, or Fathom), Cubie emails the meeting a recap once the "
            "recording is ready, usually within the hour. It is written from the "
            "recording TOGETHER WITH what the team entered in the meeting — closing "
            "notes, ratings, check-ins, issues solved, new to-dos — so it covers both "
            "what was said and what was logged: a short summary, the decisions and "
            "issues solved, the new to-dos with owners, and anything carrying into next "
            "week. Two things to know: (1) it goes ONLY to the people the RECORDING "
            "shows were actually in the room, so someone who was invited but didn't "
            "join doesn't get it; (2) it obeys the same rules as the meeting itself — "
            "the Exec L10's recap reaches Exec team members only, and on a meeting "
            "imported from Google Calendar only people on the invite are emailed. Each "
            "meeting gets exactly one recap.\n"
            "RECAP EMAIL IF YOUR L10 WASN'T RECORDED — you still get one. If no "
            "recording turns up, Cubie waits until one definitely isn't coming (about a "
            "day and a half, so a late transcript is never missed) and then emails a "
            "recap written from what the team entered in the meeting itself: closing "
            "notes, ratings, check-ins, issues solved and new to-dos. It says so at the "
            "bottom, and because there's no transcript behind it, anything discussed "
            "but never logged in Cubie won't be in it — what you type during the L10 is "
            "what you get back. It goes to the meeting's team and follows the same "
            "access rules as the recorded version. If the meeting wasn't recorded AND "
            "nothing was entered, no email is sent — there'd be nothing to say.\n"
            "PROCESSES — your documented core processes (FBA = 'Followed By All').\n"
            "CAPTURE FROM SLACK — first connect your Slack account in Cubie Settings. "
            "In any Slack channel or DM, type /todo to add a To-Do or "
            "/ids to raise an Issue. A short form sets team, owner, and due date (To-Dos) or "
            "priority (Issues). It lands on that team's list instantly and appears in the "
            "L10. Add words after the command (e.g. '/todo send the renewal paperwork') to "
            "pre-fill the form. Only you see it; nothing is posted to the channel.\n"
            "IF YOU SIT ON MORE THAN ONE L10 — everything you add carries ONE team, and that "
            "team decides which L10 it shows up in. Added during a meeting it goes to that "
            "meeting's team automatically, so there's nothing to choose. Added anywhere else "
            "— the Issues, To-Dos, Rocks or Scorecard page, or /todo and /ids in Slack — a "
            "Team picker decides, and by default it offers only your own department. To get a "
            "second team in that picker, an ADMIN adds it under Admin > Users > 'Additional "
            "EOS teams'; ask whoever runs Cubie. Once added you can also see and search that "
            "team's Rocks, Issues, To-Dos and Scorecard on the standalone pages, not just "
            "inside the meeting. It does NOT let you edit other people's items or schedule "
            "that team's meetings; those stay with the team's own leader, and the Exec team is "
            "never reachable this way. Two things keep it honest: an admin can only add a team "
            "whose L10 invite currently includes you, so a one-off guest appearance never "
            "becomes a permanent team; and the moment you come off that meeting's calendar "
            "invite the team drops away by itself. That's also what makes a MOVE between "
            "departments clean — off the old invite, on the new one.\n"
            "SEEING ONE TEAM AT A TIME — when you're on more than one team, the Issues, "
            "To-Dos, Rocks and Scorecard pages open on YOUR OWN department, so the page "
            "stays 'your' list. A Team dropdown at the top right switches to another of "
            "your teams, or to 'All my teams' to see everything together, and whatever you "
            "pick is remembered for next time. The L10 itself is never mixed: inside a "
            "meeting you only ever see that team's items, whichever teams you belong to.\n"
            "The EOS Dashboard (a cross-department executive rollup that trends the "
            "percentage of issues resolved each week) and the VTO (Vision/Traction Organizer) "
            "appear only for the Exec team and admins. The executive scope is called "
            "'Executive View' everywhere it appears."
        ),
    ),
    HelpSection(
        id="meeting-prep",
        title="Meeting prep",
        keywords=(
            "meeting prep",
            "briefing",
            "generate prep",
            "deep prep",
            "get ready",
            "agenda",
            "intent",
            "1:1",
            "invite",
            "decline",
            "attendee intelligence",
            "linkedin",
            "attendee",
            "who is in the room",
            "personality",
            "communication style",
            "posts",
            "last met",
            "prior meetings",
            "meeting history",
            "version",
            "versions",
            "version history",
            "previous version",
            "older version",
            "restore",
            "regenerate",
            "keep my edits",
            "edit section",
            "edit the briefing",
            "rewrite section",
            "regenerate section",
            "built from",
            "hubspot",
            "x-ray",
            "promises",
            "background",
            "built in the background",
            "notify",
            "notified",
            "prep is ready",
            "briefing is ready",
            "leave the page",
            "taking long",
            "still building",
        ),
        body=(
            "Cubie can build a briefing for an upcoming meeting — reach it from a 'Generate "
            "prep' action on an AI Priority or a calendar event. You get an executive "
            "summary, the meeting objective, a client profile, who's involved, and a "
            "suggested agenda.\n"
            "NO MORE WAITING ON THE PAGE. Clicking 'Get Ready' (or 'Regenerate' on a "
            "briefing you already have) starts building the briefing in the background. You "
            "can keep working, open something else, or leave the page altogether. When it's "
            "ready, Cubie sends you a notification with a link straight to it: a pop-up if "
            "you're anywhere in Cubie, and an entry in the new bell in the left sidebar, "
            "which keeps it until you open it. If Cubie can't build the briefing — or it "
            "takes far longer than it should — you get a notification saying so too, and "
            "opening it lets you try again. While a new version is being built, the version "
            "you already have stays open and readable. Clicking the button again while a "
            "briefing is on its way doesn't start a second one; you simply get the one "
            "already being built. Cubie builds a few briefings for you at a time; if you "
            "start more than that, it asks you to wait for one to finish. Rewriting a single "
            "section still happens right there on the page. You can mute these "
            "notifications in Settings → General.\n"
            "Add a meeting intent (e.g. 'Push for pilot approval') to focus the briefing, "
            "or print it to PDF (named after the meeting automatically).\n"
            "YOUR BRIEFING IS SAVED, AND EVERY CHANGE IS A NEW VERSION. Opening a meeting's "
            "prep shows your latest saved version straight away instead of rebuilding it "
            "every time you open the page. 'Regenerate' builds a fresh version from the "
            "newest information and keeps any section you edited yourself exactly as you "
            "wrote it — untick 'Keep my edits' if you want those rebuilt too. If Cubie "
            "can't build the new version, it sends you a notification saying so and the "
            "version you had stays as it was.\n"
            "If your workspace loses access to a client in HubSpot, a briefing or saved "
            "Asset based on that client becomes unavailable. Opening Meeting Prep starts "
            "a fresh briefing using sources you can currently access.\n"
            "Go back to any earlier version: the version menu lists every version of the "
            "briefing (v1, v2, v3 and so on) with when it was made and what changed. Open "
            "one to read it, and 'Restore' adds it back as the newest version. Nothing is "
            "overwritten or thrown away, so you can always go back again. Older versions "
            "are read-only; you edit the newest one.\n"
            "Edit right on the page: every section of the briefing has 'Edit', to change the "
            "words yourself and save, and 'Rewrite', to have Cubie redo just that section — "
            "with an optional instruction such as 'shorter' or 'focus on pricing' — without "
            "touching the rest. Each section shows whether you edited it or Cubie rewrote "
            "it, and every save, rewrite or restore becomes its own version.\n"
            "'Word' downloads it as an editable Word document; 'Save to Assets' puts it on "
            "your Assets page, from where you can share it with a teammate or have "
            "Cubie draft a Gmail with it attached — Cubie never sends, you review and send. "
            "Word, PDF and Save to Assets always use the version you have open, and the Word "
            "file and the Assets copy carry its version number in their name.\n"
            "Briefings stay grounded in your records: Cubie sticks to what your transcripts, "
            "emails and documents actually say and cites sources. It won't guess whether a "
            "past deal was won or lost; where records are thin it says so.\n"
            "Built from more of what Cubie knows: for meetings with clients, the briefing "
            "includes the client's HubSpot account — open deals with their stage, amount "
            "and close date, past wins, and when the client was last contacted, both on the "
            "deal and on the company. If you have Customer X-Ray access, it also draws on "
            "the company's latest Customer X-Ray. If you use My To-Dos, the to-dos you still "
            "owe the people on the invite feed the 'What You Owe Them' list. The briefing "
            "also recalls what was agreed in your last meetings with them, and picks up "
            "anything today's AI Priorities flagged about the meeting, the people on it or "
            "the client. For internal meetings, it flags teammates on the invite who have "
            "time off in the next two weeks (only the dates they are out). A 'Built from' "
            "strip at the top shows which sources informed the briefing, so you can see "
            "what it did and didn't have to go on.\n"
            "Internal meetings and 1:1s get real context — Cubie reads your recent email "
            "with the people on the invite, colleagues included, and adds a 'What You Owe "
            "Them' list when a thread is waiting on your reply.\n"
            "Every briefing also checks your actual meeting history: it lists the meetings "
            "you genuinely attended with the people on the invite — when you last met and "
            "who was in the room — from attendance records rather than guesswork.\n"
            "For meetings with people outside your company, briefings include an 'Attendee "
            "Intelligence' panel: each outside attendee's actual position and career "
            "trajectory, the posts, articles and talks they have published, the themes they "
            "keep returning to, a read on how they communicate and decide, and openers built "
            "from something they actually said. Every profile and post is a link you can open "
            "and check.\n"
            "Each style read is labelled with how much evidence is behind it (Well evidenced "
            "/ Partly evidenced / Thin evidence), so you can tell a read built on a dozen "
            "posts from one built on a job title, and Cubie says plainly when it found "
            "nothing instead of inventing a personality. Only people OUTSIDE your company are "
            "researched — colleagues are never profiled, shared inboxes and meeting rooms are "
            "skipped, and it sticks to public professional material about someone's work. "
            "Every post or talk listed links to that item itself, not just a website's front "
            "page, and Cubie refers to people by name rather than guessing he or she. If Cubie "
            "runs out of time researching someone, the briefing says so under Intelligence "
            "Gaps instead of leaving their card out without a word.\n"
            "'Generate deep prep' on a calendar event runs a more thorough, multi-step "
            "review in the background and EMAILS you a PDF when it's ready, tailored to your "
            "team (Sales gets a sales-focused brief). Deep prep is a separate emailed "
            "report, so it doesn't appear in the version menu — the regular briefing is the "
            "one that lands in Cubie with a notification.\n"
            "Only meetings you actually accepted are prepped. Invites you never answered are "
            "listed in one line at the top of AI Priorities instead — accept or decline and "
            "it stops showing up. A 'maybe' still gets prepped; meetings you declined are "
            "dropped entirely (no prep card, no 'Get Ready' button). You can still hit 'Get "
            "Ready' on an unanswered invite whenever you want.\n"
            "THIS NOW INCLUDES YOUR OWN MEETINGS. Google lets you answer 'No' to a meeting "
            "you organize yourself, and Cubie used to ignore that — so a manager who had "
            "stood down from their own recurring meeting kept being prepped for it every "
            "week. Decline it and the prep stops. Leaving your own invite UNANSWERED still "
            "counts as going, so nothing you simply never RSVP'd to disappears.\n"
            "At the bottom of a briefing, the 👍 / 😐 / 👎 buttons (plus an optional note) "
            "train future briefings."
        ),
    ),
    HelpSection(
        id="customer-xray",
        title="Customer X-Ray",
        requires="xray",
        keywords=(
            "x-ray",
            "xray",
            "churn",
            "customer",
            "deep research",
            "expansion",
            "opportunities",
            "cross-sell",
            "relationship",
            "risk score",
            "hubspot link",
            "cubie research link",
            "version",
            "earlier version",
            "history",
            "renewal date",
            "account status",
            "csm",
        ),
        body=(
            "On My Work → Sales → Customer X-Ray, search a customer and generate an X-Ray — "
            "a single view of the whole relationship. Open to everyone unless an admin has "
            "narrowed it. Two flavors:\n"
            "- Fast Research (seconds) — a churn-risk score with an explainable factor "
            "breakdown, a relationship timeline, commercial health, and the top expansion "
            "opportunities: products and services from your company's catalog that the "
            "customer hasn't bought yet. Cubie judges what "
            "they already have from their won deals in HubSpot, so a service that is only on "
            "an open or lost deal still counts as an opportunity.\n"
            "- Deep Research (a few minutes) — adds communication-health metrics from email "
            "and past meetings, documents, and recent online news researched live. It runs "
            "in the background and emails you the finished brief as a PDF.\n"
            "Deep Research costs money every time it runs, so Cubie asks you to confirm "
            "before starting one — save it for a renewal, a QBR or a save, and use Fast "
            "Research for everything else.\n"
            "Your workspace admin can choose a provider for X-Ray analysis. Deep Research's "
            "live web search still uses managed Vertex. If the selected analysis provider "
            "is unavailable, Cubie reports the failure instead of switching accounts.\n"
            "When several workspaces share one HubSpot portal, an X-Ray is available only "
            "for a company whose deals all belong to your workspace. A company linked to "
            "another workspace's deals stays unavailable, including older saved briefs, "
            "until its ownership can be confirmed.\n"
            "Renewal date, account status and the CSM: X-Ray reads HubSpot's standard "
            "fields for every customer. A contract's renewal date, whether it auto-renews, "
            "the account's status, its customer-success manager and the deal field that "
            "says what a customer bought are fields your company created in HubSpot, under "
            "names of its own, so an admin tells Cubie once which fields those are. Until "
            "then the churn score leaves out the renewal and status factors (it never counts "
            "the unknown as healthy), the status and CSM tiles stay empty, and X-Ray "
            "suggests no expansion opportunity, because it can't yet tell what the customer "
            "already owns. An admin can also list more fields for the write-up to take into "
            "account.\n"
            "Can't find an older event on the timeline? It opens on the 100 most recent "
            "moments — use 'Load older events' at the bottom to keep walking back, and the "
            "source chips (Deals, Activity, Email…) to search the customer's full history "
            "for just that kind rather than only what fits in the first page. If a slice of "
            "HubSpot was too large to pull in full, the Intelligence Gaps box at the top "
            "says so explicitly, so a missing record is a stated limit, not a mystery.\n"
            "Deep Research now works out your meeting history with a customer from actual "
            "attendance records — who from that company was in the room, and when — instead "
            "of searching transcripts for the company's name. That means a meeting still "
            "counts even if nobody said the company name in it. You only ever see meetings "
            "you attended yourself.\n"
            "Every X-Ray you run also writes a link back to the customer's HubSpot record, "
            "in the Cubie Research Link field on the company. So a teammate who lives in "
            "HubSpot can see that Cubie has researched this account and open the latest "
            "brief in one click, without knowing Cubie had it. The link always opens the "
            "newest research for that customer, so it never goes stale — and the usual "
            "access rules still apply when they click it.\n"
            "X-Rays are a shared team asset: once generated, teammates can open the latest "
            "one, and a refresh only adds what's new since last time. The churn score is "
            "computed from CRM data (never made up) and the write-up explains it. Rate an "
            "X-Ray with 👍 / 😐 / 👎 to train future ones.\n"
            "Every X-Ray is kept. Once a customer has more than one, version tabs under the "
            "name (v1, v2…) list each finished run and mark the latest. Click an earlier one "
            "to read it as it was — the churn score, signals and write-up from that run, with "
            "the timeline stopping on the day it ran — and 'Back to latest' returns you to the "
            "newest. An earlier version is read-only: you can print it, download it or save "
            "it to Assets, but rating stays on the latest. Its link reopens that same version, "
            "so you can send it to a teammate. Numbers can skip (v2, then v4) because a run "
            "that failed or never finished still uses a number, and 'latest' means the most "
            "recently finished run, which is usually but not always the highest number.\n"
            "To pass one along: PDF prints it (named after the customer and version), Word "
            "downloads an editable copy (its file name carries the version too), and 'Save "
            "to Assets' puts it on your Assets page — from there "
            "share it with a teammate or have Cubie draft (never send) a Gmail with the "
            "document attached."
        ),
    ),
    HelpSection(
        id="crm-sync",
        title="CRM Sync",
        requires="crm_sync",
        keywords=(
            "crm sync",
            "scan my calls",
            "hubspot update",
            "suggestion",
            "approve",
            "write",
            "transcript",
        ),
        body=(
            "CRM Sync reads your recent EXTERNAL call transcripts, compares what was said "
            "against what HubSpot actually holds, and suggests the updates your CRM is "
            "missing — a contact who isn't in there yet, a deal amount or close date that "
            "moved on the call.\n"
            "Click 'Scan my calls' and Cubie works through your last week of meetings in the "
            "background (a couple of minutes — you can leave the page). Each suggestion "
            "arrives as a card showing the exact before → after change, a quote from the "
            "call as evidence, and how confident Cubie is.\n"
            "If this workspace uses its own AI provider and that provider fails, the scan "
            "shows an error. Ask a workspace admin to check Admin → Access → Google "
            "Workspace → AI provider credentials, then try again.\n"
            "NOTHING is ever written to HubSpot on its own. You approve each item "
            "individually and only what you approve is written. You can Edit a value first "
            "if Cubie got a detail slightly wrong, or Dismiss it — a dismissed suggestion "
            "stays out of your way for a month. If a deal moved in HubSpot since the scan, "
            "Cubie refuses the change and re-shows the card against the current state.\n"
            "Limits: only your OWN meetings are read, internal meetings are skipped entirely, "
            "deal changes are limited to deals you own in the company's own pipelines "
            "(and wait until whoever runs your Cubie deployment has named which HubSpot "
            "pipelines may be written to), and Cubie only ever touches names, phone, job "
            "title, deal amount, stage, and close date — it never logs notes or "
            "engagements. Rate suggestions with "
            "👍 / 😐 / 👎 to make future ones sharper."
        ),
    ),
    HelpSection(
        id="sales-calls",
        title="Sales calls (call capture)",
        keywords=(
            "call capture",
            "sales call",
            "client call",
            "start a call",
            "todays calls",
            "scheduled calls",
            "calendar",
            "which company",
            "wrong company",
            "pick the company",
            "company match",
            "meet link",
            "join link",
            "transcription",
            "transcript",
            "recurring",
            "client hosted",
            "not available yet",
            "when can i use call capture",
            "pilot",
            "wrap up",
            "wrap-up",
            "wrapup",
            "confidential",
            "confidential call",
            "suppressed",
            "not transcribed",
            "proposal",
            "proposals",
            "approve",
            "evidence",
            "quote",
            "nothing here",
            "overwrite",
            "vendor field",
            "sow request",
            "checklist",
            "call checklist",
            "no approved checklist",
            "first call",
            "script",
            "call script",
            "what to ask",
            "questions to ask",
            "discovery questions",
            "product",
            "pick the product",
            "more than one product",
            "multiple products",
            "split the lead",
            "not discussed",
            "missed a question",
            "edit the checklist",
            "add a script",
            "google meet",
            "meet add-on",
            "meet addon",
            "add-on",
            "checklist in meet",
            "side panel",
            "activities",
            "open cubie in meet",
            "cubie in google meet",
            "who's on the call",
            "guest cards",
        ),
        # Cockpit and call capture are roster-gated AND flag-gated, and `requires`
        # only knows "xray" and "crm_sync" — a third value would make the section
        # invisible to everyone, silently. So the gate is stated IN THE BODY, the
        # same shape as smart-teams below. Anything not switched on is described
        # as coming soon rather than as available: an unbuilt feature described as
        # working is worse than a gap.
        #
        # The section leads with the whole-feature switch (CCS_ENABLED, off by
        # default) and the admin's roster — but it is NOT deleted, because an AE
        # who has heard about this and asks Chat deserves "not on for you yet,
        # here is what it does" rather than silence. The narrower "coming soon"
        # note further down (the HubSpot Lead grant) is a DIFFERENT kind of
        # blocked, which is why both are stated.
        #
        # Written for the calendar entry point. An earlier copy said a call had
        # to be started from Cubie and bound to its company before it began,
        # because transcription could only be switched on when a Meet was
        # created. That is no longer how it works, and the screen it described
        # was rejected. Two things this copy must keep straight, because getting
        # either wrong is what the redesign exists to prevent:
        #   * the COMPANY is the point of the screen; arming transcription is
        #     secondary and best-effort. A row that cannot be armed is not
        #     broken, and must not read as broken.
        #   * a call Cubie armed is still not a guaranteed transcript. Say
        #     "the wrap-up will tell you", never "it will be transcribed".
        body=(
            "NOT ON FOR YOU? ASK YOUR ADMIN. Sales call capture has to be switched on for "
            "your workspace, and your admin decides who gets it. If you do not see TODAY'S "
            "CALLS under MY WORK > SALES in "
            "your left rail, it is not on for you: nothing of yours is being captured today "
            "and there is nothing to switch on for your account. The rest of this section is "
            "what it does once it is, so you know what is coming.\n"
            "Sales calls: after a client call, Cubie reads that call's own transcript and "
            "proposes the HubSpot updates it heard — a vendor field, a task, an SOW request, "
            "a deal-stage signal — and you accept or reject them on one screen, the WRAP-UP. "
            "Every proposal quotes the exact sentence from the call it came from, and NOTHING "
            "reaches HubSpot until you approve it.\n"
            "Your workspace admin can choose a compatible AI provider for reading call "
            "transcripts into wrap-up suggestions. If that provider is unavailable, "
            "Cubie shows an extraction failure and lets you retry after the admin fixes "
            "the connection. A partial analysis is labeled so you can review it manually. "
            "Cubie does not switch providers or write unapproved HubSpot updates.\n"
            "IT ALL STARTS FROM YOUR CALENDAR. Under MY WORK > SALES > TODAY'S CALLS, Cubie lists today's "
            "client calls — the ones with someone outside your company on them — and for each "
            "one it shows who is on the invite, the Meet link from the invite itself, and the "
            "company it thinks the call is with. You start the call from that row. "
            "There is nothing to set up and no second meeting to create: if it is on "
            "your calendar, it is on the list. An internal-only invite never appears on "
            "the list, and a room booking on an "
            "invite is never mistaken for a company.\n"
            "THE ONE THING WORTH YOUR ATTENTION IS THE COMPANY. Everything the wrap-up later "
            "proposes lands on that company's record, so the row asks you to confirm it BEFORE "
            "the call rather than guessing afterwards. Cubie reads the email addresses on the "
            "invite and offers the company they point at — and when it is not sure, it says so "
            "and asks instead of choosing. If a selected HubSpot record is no longer available "
            "to this workspace, Cubie stops before capture starts; refresh the calls list and "
            "choose a current record. One clear match is filled in for you and is still "
            "changeable. More than one possible match is listed for you to pick from — Cubie "
            "never picks for you. A personal address (gmail, yahoo, an internet-provider "
            "address) leaves the call listed as normal; the address just does not say which "
            "company, so you pick. Nothing found usually means the company is not in HubSpot "
            "yet, or it has no deal in this workspace's chosen pipeline — link it to "
            "a workspace deal in HubSpot before Cubie can update it. And when an invite is "
            "long enough that Google trims the guest list, Cubie treats the company as "
            "uncertain rather than trusting the few names it can still see. A call whose "
            "company you never confirmed is still recorded; the wrap-up just has to ask which "
            "company it was about, later, with less to go on than the invite had.\n"
            "TRANSCRIPTION: CUBIE TRIES, BEFORE THE CALL, AND TELLS YOU WHAT HAPPENED. When "
            "you start a call from its row, Cubie asks Meet to switch that meeting's "
            "transcription on. It can only do that BEFORE the meeting begins — there is no way "
            "to start transcribing a call that is already running. Three situations where it "
            "cannot, all of them normal: (1) THE MEETING HAS ALREADY STARTED — too late to "
            "switch it on; start it from the row anyway if you want the call on the record "
            "against the company, and expect an empty wrap-up. (2) THE CLIENT SENT THE INVITE "
            "— it is their meeting on their system and nobody here can change its settings, so "
            "ask the host to turn transcription on or take your own notes. (3) IT IS A "
            "RECURRING MEETING — every occurrence of a repeating meeting shares one Meet room, "
            "so switching transcription on switches it on for ALL THE FUTURE ONES TOO. The row "
            "tells you that before you click and the choice stays yours. Once in a while "
            "Google itself turns the request down: the row then says COULDN'T TURN "
            "TRANSCRIPTION ON and gives Google's answer. Join with the calendar link and "
            "switch transcription on from inside Meet if you want a transcript — the call is "
            "still tied to the company.\n"
            "EVERY ROW SAYS WHICH OF THESE IT IS, and a row Cubie cannot switch transcription "
            "on for is NOT BROKEN. Confirming the company is the half that matters most and "
            "that half always works. Nothing is ever left looking fine when it is not, and no "
            "call is ever shown as being transcribed when it is not. Even when Cubie does "
            "switch it on, A TRANSCRIPT IS NOT GUARANTEED — whether one gets produced "
            "is Google's call, not ours. If none arrives the wrap-up still opens and "
            "says so, rather than leaving you wondering.\n"
            "A CALL THAT IS NOT ON YOUR CALENDAR. A Meet you open yourself is still noticed "
            "and logged against the company (usually within fifteen minutes), so a call is "
            "never invisible. Cubie cannot switch transcription on for it, though, so unless "
            "transcription was already running there is nothing for it to read. If a client "
            "call matters, put it on your calendar.\n"
            "CONFIDENTIAL CALLS. Starting a call from its row carries a Confidential toggle, "
            "and it is worth knowing what it does before you need it. A confidential call is "
            "LOGGED — it still counts as a call against the company — and is NEVER "
            "transcribed: no transcript, no AI notes, no extraction, no wrap-up, "
            "nothing kept to read back. WARNING: it cannot be un-suppressed later. "
            "There is no undo and no way to "
            "recover the content of a call you marked confidential, which is why Cubie asks "
            "you to confirm once before starting one.\n"
            "THE WRAP-UP. It appears at the top of My Day, usually within fifteen minutes of "
            "the call ending, and it is built to be finished in well under two minutes. "
            "Every row arrives already TICKED, so approving is one click and rejecting is "
            "the deliberate act: untick anything you do not want, then Approve. Under each "
            "row is a QUOTE — the exact sentence from the call that produced that proposal — "
            "so you are checking Cubie against the call rather than against your memory.\n"
            "A ROW FLAGGED AS A CHANGE. If a proposal would replace something HubSpot "
            "already holds, that row is flagged as a change, shows you the value HubSpot has "
            "now, and needs its OWN Confirm. It cannot ride along on 'Approve all', and the "
            "Approve button stays held until you have either confirmed it or unticked it. "
            "Cubie never silently overwrites a field that already has something in it.\n"
            "NOTHING HERE. The button beside Approve is an answer, not a cancel. It records "
            "that you reviewed the call and there was nothing worth filing: the call COUNTS "
            "AS COMPLETE, it does not come back unless a later transcript needs analysis "
            "or adds suggestions, and the answer is used to make the "
            "next call's proposals better. Skipping is not what it does.\n"
            "If more transcript arrives after you finished the wrap-up, Cubie analyzes the "
            "new entries and puts any new suggestions back in My Day for your review. If that "
            "analysis fails, the wrapped call stays there so you can retry it. A HubSpot write "
            "that failed stays available to retry even when the newer analysis finds nothing. "
            "The call remains wrapped and its original completion time stays on the record. "
            "Nothing new is written to HubSpot until you approve it.\n"
            "FATHOM-RECORDED CALLS COUNT TOO. If Fathom recorded your client call, Cubie picks "
            "the recording up within minutes of it finishing and treats it like any captured "
            "call: it appears under TODAY'S CALLS with a wrap-up, the SOW clock starts if the "
            "prospect asked for a proposal, and DRAFT PROPOSAL is offered. The company is matched "
            "from the attendees' email domains; if Cubie cannot tell, the wrap-up asks. Internal-only "
            "meetings are ignored, and a call Cubie already captured through Meet is not duplicated "
            "by its Fathom recording.\n"
            "AN EMPTY WRAP-UP IS NORMAL. Transcription may have been off, Cubie may not have "
            "been able to switch it on, the call may have run under a minute, or it was "
            "confidential. The wrap-up still opens, says which of those happened, and the call "
            "stays on the record — a call is never invisible even when what was said is.\n"
            "WHO CAN READ WHAT WAS SAID. You can read your own calls; your manager can read "
            "the team's. The Nudge Worklist and the Exec Roll-up never show call text at all — "
            "they can see that something is outstanding, never what was said on the call.\n"
            "THE CHECKLIST — WHAT TO ASK ON THIS CALL. Before a call, pick the product or "
            "products it is about. Cubie shows that product's checklist on the row as soon as "
            "you tick it, including on your first call with a client: the facts the call "
            "needs to establish so the record is complete and an SOW can actually be built "
            "from it — how many sessions, how many hours each, the rate, when it starts. Cubie "
            "remembers your pick for each client, so next time those products are already "
            "ticked. Keep it on screen while you talk. Cubie is NOT listening and will not "
            "interrupt you; it is a list of what to ask, not a prompter.\n"
            "IF YOU MISS SOMETHING, THE WRAP-UP TELLS YOU. Anything on the checklist the call "
            "did not cover comes back as the question you did not ask, not as a blank field, so "
            "you can chase it while the call is fresh instead of hearing about it days later "
            "when someone is trying to write the SOW.\n"
            "MORE THAN ONE PRODUCT IN ONE CALL. Pick all of them and you get one checklist "
            "section per product, scored separately. If a product comes up that you did not "
            "pick, the wrap-up says so and offers to split it into its own lead — because a "
            "lead is one product, and a call covering three of them is three leads.\n"
            "NO CHECKLIST FOR YOUR PRODUCT YET? The call runs exactly as it does today. "
            "Checklists are written by the people who own the products rather than by "
            "engineering, so they arrive product by product.\n"
            "THE CHECKLIST INSIDE GOOGLE MEET. Cubie also has a Google Meet add-on, so the "
            "same checklist can sit beside the call instead of in another tab. Once you are "
            "in the call, open ACTIVITIES (the shapes icon, bottom right) and choose CUBIE. It "
            "already knows which call you are on, so it opens on that client with the "
            "products you picked in Cubie — nothing to choose. You can tick questions off as "
            "you ask them and add another product's checklist if one comes up; those ticks "
            "stay in the panel, and Cubie still is not listening. The first time, you sign in "
            "with Google inside the panel, because Meet keeps it separate from your Cubie "
            "tab.\n"
            "IT HAS TO BE OPENED BY YOU. Google does not let any add-on open itself when a "
            "call starts, so that one click in Activities is always yours. Joining from "
            "Today's Calls opens Meet on your work account, which is where the add-on is "
            "installed. If you do not see Cubie under Activities, it has not been installed "
            "for your account yet.\n"
            "LEADS FROM A CALL. The wrap-up can also propose a HubSpot "
            "LEAD, attached to the contact on the call, with a lead temperature (hot / "
            "warm / cold) when the call made it clear. Like every other proposal it "
            "quotes the call and writes nothing until you approve it. Product interest "
            "on the lead is still set in HubSpot by hand for now."
        ),
    ),
    HelpSection(
        id="proposals",
        title="Draft a proposal from a call",
        requires="proposals",
        keywords=(
            "proposal",
            "draft proposal",
            "proposal generator",
            "sow",
            "statement of work",
            "send something in writing",
            "pricing",
            "scope",
            "desired outcomes",
            "one page",
            "pdf",
            "word",
            "revise",
            "brief",
            "template",
            "proposal template",
            "letterhead",
        ),
        body=(
            "WHAT IT IS. When a prospect asks for a proposal on a call, Cubie can draft "
            "it for you from that call — the short one- or two-page document you send to "
            "gauge interest before anyone writes an SOW. It is the same shape the Sales "
            "team has been sending by hand: Executive Summary, Scope of Work, Desired "
            "Outcomes & Key Deliverables, Pricing & Investment, notes, on your company's "
            "letterhead from Company setup.\n"
            "Your workspace admin can choose a compatible AI provider for proposal "
            "drafting and revisions. If that provider fails, Cubie shows the error "
            "and leaves the proposal to retry; it does not send the call or brief "
            "to another provider.\n"
            "THE PROPOSALS PAGE. MY WORK > SALES > PROPOSALS lists your proposals and the captured "
            "calls of the last two weeks you could draft one from, Fathom-recorded calls included, "
            "each with Open or Draft proposal. Admins can switch to everyone's for support.\n"
            "WHERE. Open the call's wrap-up under MY WORK > SALES > TODAY'S CALLS and click "
            "DRAFT PROPOSAL (it appears on any wrapped call you hosted; it is highlighted "
            "when Cubie heard the prospect ask for one). Only the AE on the call can draft "
            "its proposal. If the call was recorded by Fathom, it also works from the Sales "
            "Coach: its Continue in Cubie button opens the same proposal here.\n"
            "STEP 1 — CONFIRM THE BRIEF. Cubie reads the transcript and the HubSpot company, "
            "contact and deal, and fills a short form: who it is for, the service, the "
            "scope items that were actually discussed, the outcomes the PROSPECT stated "
            "(each with the words they used), any pricing that was said, notes, and OPEN "
            "QUESTIONS — the things that were not settled on the call, such as a price that "
            "was never quoted. Anything Cubie is unsure of is flagged. Fix what is wrong, "
            "fill what is missing, and click CONFIRM. Nothing is generated until you do, "
            "because a proposal built on an unconfirmed price is the one you end up "
            "redoing three times.\n"
            "STEP 2 — GENERATE. Cubie writes the proposal from the confirmed brief. It "
            "cannot add a service, a deliverable or a number that is not on the brief: the "
            "pricing table is printed straight from what you confirmed, and any scope item "
            "the model tries to invent is discarded before you see it. Outcomes only list "
            "goals the prospect actually said.\n"
            "STEP 3 — REVISE. Type what to change — 'tighten the summary', 'change the "
            "price to 5,500', 'drop item 3', 'add a note about travel' — and Cubie produces "
            "a NEW version. Every version is kept; open any of them and download it as a "
            "branded PDF or an editable Word file.\n"
            "WHAT CUBIE DOES NOT DO. It never sends the proposal — you download it and send "
            "it yourself, from your own email, after reading it. It does not write SOWs or "
            "contract language.\n"
            "PROPOSAL TEMPLATES. An admin writes your company's proposal templates in Company "
            "setup: a standard write-up of one service, plus the catalog items that price it. "
            "Cubie picks the one that fits the call; change it under TEMPLATE on the brief, or "
            "pick None for the AI-written sections only. With no templates set up, every "
            "proposal is written from the call alone. After your scope of work the download "
            "prints the template's write-up, word for word. Its catalog prices start the "
            "pricing table, where you can change them, and its list-price buttons add a "
            "catalog price as a standard rate for you to set the proposal price against. The "
            "AI never writes a price into the text: every figure comes from the brief you "
            "confirmed.\n"
            "LIMITS. Only your own calls, and a call with no transcript has nothing to read. "
            "Drafting takes about half a minute per step. Each proposal costs a few dollars "
            "of model time, visible to admins under AI Cost."
        ),
    ),
    HelpSection(
        id="sales-cockpit",
        title="Sales cockpit (My Day)",
        keywords=(
            "sales cockpit",
            "cockpit",
            "my day",
            "commitment",
            "commitments",
            "sla",
            "breach",
            "breached",
            "at risk",
            "age chip",
            "aging",
            "overdue",
            "eta",
            "expected date",
            "nudge",
            "escalated",
            "escalation",
            "stage lag",
            "pipeline hygiene",
            "no contact",
            "worklist",
            "roll-up",
            "lead temperature",
            "not available yet",
            "when can i use my day",
            "pilot",
            "defer",
            "defer a deal",
            "deferred",
            "deferred deals",
            "undefer",
            "follow-up date",
            "long-term deal",
            "snooze a deal",
            "hide a deal",
        ),
        # Same shape as sales-calls above: the whole cockpit is behind
        # COCKPIT_ENABLED (off by default) and the admin's rosters, so the
        # section leads with that. Kept rather than hidden — Chat should answer
        # "not on for you yet, and here is what it does" for a reader who has
        # heard about My Day.
        body=(
            "NOT ON FOR YOU? ASK YOUR ADMIN. The sales cockpit has to be switched on for "
            "your workspace. Once it is, My Day is open to everyone; the team roll-up and the "
            "nudge worklist go to the teams your admin adds, and the exec roll-up to the Exec "
            "team. If MY WORK → SALES → MY DAY is not on your rail, it is not on for you and "
            "no My Day is being built for you. What follows is what it does.\n"
            "MY WORK → SALES → MY DAY is your sales morning in the order you already work "
            "it, and the order is the feature — nothing is sorted by urgency across the page. "
            "Five blocks, always the same five: WRAP-UPS WAITING (calls to close out, first "
            "because they decay; a failed AI analysis stays here with a retry status even "
            "after the call was wrapped, and a wrapped call returns for late suggestions "
            "or an earlier HubSpot write that still needs a retry), "
            "LEADS waiting on a first touch, PIPELINE HYGIENE (where "
            "HubSpot's deal stage disagrees with what the call said; it needs your "
            "pipeline's stages, which your admin lists in order under Admin → Access → Google Workspace → "
            "HubSpot settings, and "
            "until then the block stays empty), DEALS running against a clock, and NO "
            "CONTACT IN 90 DAYS (quiet accounts, oldest first). A deal whose "
            "client heard from us inside the 90 days — an email to someone not on the deal is "
            "filed on the client's company — still shows, titled 'No contact logged on this "
            "deal', after the deals whose whole client has gone quiet: logging that email on "
            "the deal clears it.\n"
            "THREE AT A TIME, ON PURPOSE. Each block shows at most three items and the rest "
            "appear as '+4 more', which LINKS to the full list for that block instead of "
            "expanding it. A list of twenty is a list of zero, so the remainder surfaces as "
            "you clear what is in front of you.\n"
            "AN EMPTY BLOCK IS NOT ALWAYS 'YOU ARE CAUGHT UP'. When Cubie could not look — "
            "call capture switched off, HubSpot unreadable, or no HubSpot owner matching "
            "your Cubie address — the block says that in its own words instead of showing a "
            "clean slate. If it says nothing, the block really is empty.\n"
            "THE AGE CHIP. Any row with a clock carries a small chip, and the number is "
            "always printed next to the colour — so a screenshot pasted into Slack still "
            "says what it says:\n"
            "- Grey — open, still inside its time.\n"
            "- Amber — at risk, roughly the last third of the clock.\n"
            "- Red — breached, and it says how far over ('9d, 2d over').\n"
            "- Red with a person's name — escalated: it is now on somebody else's plate, and "
            "naming them is the information.\n"
            "- A plain chip that only counts days since the call claims NO lateness — that "
            "row has no agreed clock.\n"
            "- 'Age unknown' means there is no usable start date on the record. Deliberately "
            "not shown as '0d', because zero would hide exactly the stall worth catching.\n"
            "THE CLOCK IS IN BUSINESS HOURS. A business day is eight hours in the company's "
            "business time zone and weekends do not count, so a Friday-afternoon request is "
            "not late on Monday morning. Holidays and PTO are NOT subtracted yet — a stated "
            "limitation, not a bug.\n"
            "SLA NUMBERS ARE PLACEHOLDERS RIGHT NOW. The thresholds shipped provisional: 3 "
            "business days for a standard SOW, 10 for a non-standard one, 1 business day to "
            "finish a wrap-up, 2 business days for a lead's first touch. Nobody has agreed "
            "them yet — an admin confirms them — so the app marks a placeholder threshold "
            "with a dotted underline that says 'Placeholder — not yet confirmed by an "
            "admin.' when you hover it. Read a breach against a placeholder as a prompt, not "
            "a verdict.\n"
            "SETTING AN ETA. On a deal row, 'Set expected date' takes a date plus a short "
            "reason. A date LATER than the standard SLA moves the clock to your date, so a "
            "three-week RFP stops reading as late on day four — that is the main reason to "
            "use it. A date EARLIER than the SLA never shortens the clock, so promising a "
            "client something tight cannot create an internal breach against you. Your "
            "reason is stored with the date.\n"
            "ACCEPTING A BREACH. Where something is genuinely late and that is genuinely "
            "fine, Accept it and write why. The reason is required and it is kept: it is the "
            "audit trail that is kept by hand today, and it stops the row being the loudest "
            "thing on your manager's screen every fifteen minutes.\n"
            "DEFERRING A DEAL. A long-term deal with no real close date can be set aside "
            "instead of showing up every morning. Press DEFER on its row — in Pipeline "
            "hygiene, Deals, No contact in 90 days or the Daily rhythm checks — or 'Defer the "
            "deal…' on its card in the Daily Briefing. Pick a FOLLOW-UP DATE (after today, at "
            "most 180 days out) and write a NOTE saying why; both are required. Until that "
            "date the deal is off your My Day, your Daily rhythm checks, your Daily Briefing "
            "cards and the morning Slack message about them. On the date it comes back by "
            "itself. Cubie adds one follow-up TASK on the deal in HubSpot, due that morning, "
            "with your note as the task; the deal's stage and close date are not touched. "
            "DEFERRED DEALS, at the bottom of My Day, lists what you have set aside: UNDEFER "
            "brings one back early (its HubSpot task stays; close it there if you no longer "
            "need it), and if the HubSpot task could not be created the row says why and "
            "offers a retry. YOUR MANAGER STILL SEES EVERY DEFERRED DEAL: the Team Roll-up and "
            "the weekly close-date roll-up list each one with its owner, the follow-up date "
            "and your note, and its late commitments still count there. Write the note for "
            "them as much as for yourself.\n"
            "WHAT A NUDGE DOES. On the Nudge Worklist, Nudge does three things and Cubie tells "
            "you which of them landed: it creates a HubSpot TASK for the owner, sends them a "
            "SLACK DM, and LOGS the nudge itself. If the owner is not in the Slack "
            "workspace, the DM is skipped and Cubie says so rather than claiming it sent "
            "one. Two nudges with no answer trigger an ESCALATION rather than a third nudge "
            "— chasing louder is not the answer, changing who is accountable is.\n"
            "THE OTHER THREE VIEWS. They appear under MY WORK → SALES beside My Day, but only for the "
            "people they belong to: your admin adds the team each one is for, and the exec "
            "roll-up goes to the Exec team. TEAM ROLL-UP is a manager's view: who is "
            "outstanding, the reasons people gave, and every deal a rep deferred, with the "
            "date and the note. NUDGE WORKLIST is the delivery team's view and is metadata only — "
            "what is outstanding and who owns it, never the text of a call. EXEC ROLL-UP is "
            "org-level counts and a trend with no per-person drill-down, deliberately, so it "
            "cannot turn into a leaderboard.\n"
            "COMING SOON — these stay off even where the cockpit is on: the daily Slack "
            "digest and the Monday aging roll-up (off on "
            "purpose, so the cockpit is read on screen for a while before it starts "
            "messaging anyone); and filtering leads by temperature. Nothing from the cockpit arrives "
            "in Slack today. Loose Ends is separate: it has its own panel in My Day, and its "
            "once-a-day post to #sales stays off until a Cubie admin turns it on (see the "
            "Loose ends page in Help)."
        ),
    ),
    HelpSection(
        id="sales-rhythm",
        title="Sales daily rhythm",
        keywords=(
            "sales rhythm",
            "daily rhythm",
            "daily checklist",
            "sales checklist",
            "morning routine",
            "mid-day",
            "midday",
            "end of day",
            "end-of-day",
            "eod",
            "closing this month",
            "waiting on the client",
            "decision maker",
            "decision-maker",
            "slipping",
            "close date pushed",
            "single-threaded",
            "single threaded",
            "one contact",
            "re-engage",
            "re-engagement",
            "lost deals",
            "check-in",
            "missing fields",
            "job title",
            "persona",
            "buying role",
            "next step",
            "overdue tasks",
            "recap for my manager",
            "team view",
            "top 10 deals",
            "last contacted",
            "no contact",
            "company record",
        ),
        body=(
            "A daily routine for sellers — a morning, a mid-day and an end-of-day "
            "checklist — built into Cubie. Each step is a question Cubie answers from HubSpot "
            "(or your email, Slack and calendar) about your OWN deals.\n"
            "WHERE YOU FIND IT. In Ask Cubie, a new chat opens with a YOUR SALES RHYTHM row "
            "for the time of day (before 11:00 the morning, before 15:00 mid-day, then the end "
            "of the day); tabs beside it switch between the three. If you have My Day, the "
            "same checks also appear in its DAILY RHYTHM section, below the five usual blocks, "
            "as short lists with the next step on every row.\n"
            "MORNING: messages you have not answered, follow-ups you promised, prep for "
            "today's external meetings, and the deals closing this month that need you today. "
            "MID-DAY: proposals waiting on the client, close dates that keep slipping, big "
            "deals hanging on one contact, and lost deals worth a check-in. END OF DAY: records "
            "you touched today with a missing field, overdue tasks and past-due close dates, "
            "and a drafted recap of your day for your manager.\n"
            "HOW EACH CHECK DECIDES. Needs action this month: closing this month with no "
            "client contact in a week (logged, emailed or met), no next step booked (an "
            "upcoming meeting with the client counts), a close date that has already moved, "
            "or one that has passed. Waiting on the client: in a proposal, SOW, legal-review or "
            "contract stage, your last email or logged contact is three or more business days "
            "old, and the decision-maker has not replied since (no email back, no logged "
            "reply, no meeting) — the decision-maker is whoever HubSpot marks with the "
            "Decision Maker buying role, and when nobody is marked the row says so. Which "
            "stages count can be set by your admin under Admin → Access → Google Workspace → HubSpot "
            "settings (stage IDs or labels); "
            "until they do, Cubie goes by the stage names. Slipping: the close date has been "
            "moved later three or more times, or no client contact in seven days (logged, "
            "emailed or met). Single-threaded: open deals of $30,000 or more with one contact or none. Worth a "
            "check-in: lost 12 to 24 months ago over budget or timing (the Pricing loss "
            "reason, or a note like 'they will pass this year'), where the contact's email has "
            "not bounced and they are still linked to the company; HubSpot cannot confirm "
            "someone still works there, so check LinkedIn before writing. Missing fields: a "
            "contact with no job title or buying role, or a deal with no next step booked (a "
            "meeting on your calendar with the client counts), no amount or no close date.\n"
            "CONTACT LOGGED ON THE CLIENT, NOT THE DEAL. A deal's own HubSpot record only "
            "shows contact attached to that deal. Email someone at the client who isn't on "
            "the deal and HubSpot files it on the client's company instead. Cubie checks that "
            "company record too: when the client heard from us more recently than the deal "
            "shows, the row gives both ('no logged client contact in 13 days, none on this "
            "deal itself in 82 days'), and deals whose whole client has gone quiet are listed "
            "first. The company's date may be about a different deal with the same client, so "
            "the deal's own date is always shown too. To keep the deal current, add the "
            "person you emailed to the deal in HubSpot.\n"
            "TWO FIELDS CUBIE READS ITS OWN WAY. Cubie reads a contact's Buying role in "
            "place of HubSpot's Persona field: Buying role comes with the same options in "
            "every HubSpot account, while each company sets up its Personas differently, or "
            "not at all. And HubSpot has no 'Next steps' field, so a booked next activity "
            "counts as a deal's next step.\n"
            "EVERY ROW HAS ONE NEXT STEP: it opens the record in HubSpot, or it opens Ask "
            "Cubie with a draft request ready — a follow-up or check-in email saved as a "
            "Gmail draft. Nothing is sent for you. Each list shows three rows; '+4 more' opens "
            "Ask Cubie with the whole list, and every count says what it is out of ('2 of 14 "
            "open deals').\n"
            "DEFERRED DEALS ARE LEFT OUT. A deal row also has DEFER, for a long-term deal "
            "you want out of the way until a follow-up date. A deal you deferred is left out "
            "of every check, on My Day and in Ask Cubie, until that date, and the list says "
            "how many it left out (see Deferring a deal on the Sales cockpit page in Help).\n"
            "HUBSPOT, PLUS YOUR OWN EMAIL AND CALENDAR. For your own deals Cubie also checks "
            "your Gmail and calendar, read-only: an email to or from the client, or a meeting "
            "with them, counts as contact even if it was never logged, and a meeting on your "
            "calendar counts as a booked next step. From your email it reads only dates, plus "
            "(for the end-of-day recap) the recipients and subject of each email you sent that "
            "day; from your calendar, meeting times, titles and who was invited. Drafts never "
            "count as sent, out-of-office replies and newsletters never count as the client "
            "answering, and a meeting the client declined is not contact. A phone call only "
            "counts if it is logged in HubSpot or on your calendar. Counting an unlogged email "
            "as contact keeps a deal from looking quiet here, but it does not put the email in "
            "HubSpot: emails and client conversations from past days that are still not "
            "logged in HubSpot are on your Loose ends list (see the Loose ends page in Help), "
            "which keeps each one until it is logged.\n"
            "THE TEAM VIEW. Sales leaders an admin adds by name get the team version "
            "of the prompts: the team's top 10 deals closing this month that need action, the "
            "records the team touched today with a missing field, and the same mid-day checks "
            "across everyone's deals with each deal's owner named. It lists deals, not "
            "people — no per-rep counts or rankings, and colleagues' overdue tasks and "
            "activity logs are left out. It never reads anyone's email or calendar: it counts "
            "only what is logged in HubSpot, on the deal and on the client's company record, "
            "and every answer says so.\n"
            "WHO HAS IT. The Ask Cubie prompts are on for everyone. The My Day section also "
            "needs My Day itself, which has to be switched on for your workspace. Admins can "
            "narrow either from Admin → Feature Access."
        ),
    ),
    HelpSection(
        id="loose-ends",
        title="Loose ends",
        # Ungated on purpose, like sales-rhythm: the page explains who has it, and a
        # Sales leader asking "what is this Loose Ends post in Slack?" should get an
        # answer even before they are on the roster. The rules below mirror
        # docs/loose_ends/README.md (the feature's design contract) and the page in
        # frontend/src/pages/help/topics/LooseEnds.tsx.
        #
        # Keywords stay on the feature's own words. "what am I waiting on" belongs
        # to the chat section (the waiting-on sweep), and the daily-checklist and My
        # Day questions to sales-rhythm and sales-cockpit, so no bare "waiting",
        # "reply", "promise", "my day" or "persona" here.
        keywords=(
            "loose ends",
            "loose end",
            "loop closure",
            "loop-closure",
            "unlogged",
            "unlogged emails",
            "not logged in hubspot",
            "not in hubspot",
            "log it in hubspot",
            "open loops over 7 days",
            "not a real miss",
            "mute",
            "loose ends over 7 days",
        ),
        body=(
            "Every weekday morning Cubie checks each account executive's email, Slack and "
            "HubSpot for the client follow-through that slips quietly: a client conversation "
            "that never made it into HubSpot, a client email still waiting on your answer, and "
            "something you said you would do that is now late. What it finds is your list of "
            "LOOSE ENDS. The list carries over from one day to the next, so each item keeps "
            "its real age until it is closed.\n"
            "THE THREE KINDS, AND WHAT CLEARS EACH ONE.\n"
            "- NOT IN HUBSPOT — you emailed a client (a HubSpot contact, or someone at a "
            "company that has a deal in HubSpot) more than one business day ago, or said in "
            "Slack that you had a call or meeting with a client on one of your open deals, and nothing has "
            "been logged in HubSpot since: no email, call, meeting or note on that contact, "
            "their company or any of their deals. It clears when one of those is logged, dated "
            "from the day before your email onward, or when you log it from Cubie.\n"
            "- WAITING ON YOUR REPLY — a client wrote to you directly (you are in the To line, "
            "not only Cc or a mailing list) and you have not answered in two business days. It "
            "clears when the thread shows your reply (or a colleague's reply on the same "
            "thread), or when a follow-up is logged in HubSpot on that contact or company after "
            "their message. A reply you only saved as a draft does not count.\n"
            "- YOU SAID YOU WOULD — a promise Cubie heard you make, in an email, in Slack or in "
            "a meeting, is five days past its date (five days after you said it, if you gave "
            "no date), or a HubSpot task of yours is five or more days overdue (only tasks that "
            "went overdue in the last 30 days; older ones are left alone). Something less than "
            "five days overdue is not on the list yet. It clears when "
            "the promise is kept (Cubie finds a sign it happened, or you tick it on your "
            "briefing's 'You said you would' card), dropped or handed off, or when the task is "
            "completed, deleted or reassigned in HubSpot. When the date is moved later (you "
            "snooze the promise or move the task), it leaves the list as RESCHEDULED, which is "
            "never counted as done; if the new date also passes, it comes back.\n"
            "YOU CAN'T TICK AN ITEM OFF HERE. There is no Done button on this list. Cubie "
            "checks HubSpot and your Gmail each morning, and a loose end clears itself when "
            "the record shows it: your reply in the thread, the activity in HubSpot, the task "
            "marked complete. Nothing closes just because it fell out of the dates Cubie looks "
            "at.\n"
            "WHERE IT SHOWS UP. MY WORK → SALES → LOOSE ENDS is your own list, in the three "
            "groups above, oldest first, with one real action on every row; Sales leaders also "
            "get a TEAM tab, and a leader who has only the Team tab lands straight on it. If "
            "you have My Day, a Loose Ends panel under the five usual blocks "
            "shows your three oldest (My Day itself is off until your company switches it on; "
            "see the Sales cockpit page). In SLACK, there can be one post a day to #sales, but the daily post is "
            "off until a Cubie admin turns it on in Admin → Loose Ends; "
            "the admin can change the channel there. When it is on, a day with nothing to "
            "report posts nothing, and anyone Cubie could not check that morning is named on "
            "a line of their own and not counted as scanned. When EOS is enabled "
            "for your workspace, the Team tab shows scorecard setup and, on THE SCORECARD, each "
            "account executive has a weekly 'Loose ends over 7 days' row on the Sales "
            "scorecard, filled in by Cubie, with a goal of zero; when Cubie could not read "
            "someone's mailbox, or their scan failed, their number is left as it was rather "
            "than written as zero. In ASK CUBIE, ask 'What loose ends do I have?' or, with the "
            "Team tab, 'Which loose ends on my team are over 7 days?'\n"
            "THE BUTTONS ON EACH ROW.\n"
            "- LOG IT IN HUBSPOT (Not in HubSpot, emails only) puts your actual email, full "
            "text, on the contact, company and deal in HubSpot as an email, the same as "
            "HubSpot's Gmail extension, so the contact's last-contacted date moves too. First "
            "it shows you each email you sent in that thread (who it went to, when, and the "
            "start of the text), each with a tick box, and nothing is written until you "
            "confirm. Only the emails you sent to that contact start ticked; one you sent to "
            "someone else, such as a forward to a partner, is logged only if you tick it, and "
            "a forward to a personal address like Gmail is never offered. An email HubSpot "
            "already has is shown greyed out and is not added again. A very long email is "
            "cut at 65,000 characters, the most HubSpot holds, and one whose formatting is "
            "too long for HubSpot is logged as plain text. Attachments are not included. "
            "Cubie never writes notes (call notes come from Fathom) and never writes AI text "
            "into HubSpot. The row clears once the emails are logged, and clicking it a "
            "second time, or trying again after an error, does not log them twice. When "
            "Cubie can't tell which HubSpot contact the email went to, the row opens "
            "HubSpot instead so you can log "
            "it yourself.\n"
            "- OPEN IN HUBSPOT (Not in HubSpot, from Slack): a call or meeting you mentioned "
            "in Slack has no email to log, so there is no log button. The row opens the "
            "company or deal in HubSpot, with the Slack message next to it, so you can log "
            "the conversation there.\n"
            "- DRAFT A REPLY (Waiting on your reply): Cubie reads the thread in your own "
            "mailbox and saves a reply as a Gmail draft in that thread, set up as a reply to "
            "the client's message so it stays in the same conversation on their side too. It "
            "never sends anything. The row stays until you actually send a reply.\n"
            "- OPEN (You said you would) opens the email thread or the HubSpot task. A promise "
            "heard in a meeting with no saved record has nothing to open, and the button says "
            "so.\n"
            "- NOT A REAL MISS is for a row that should never have been flagged. Pick a reason "
            "(not a client, automated, internal, handled elsewhere, not a promise, or other) "
            "and the row leaves your list for good: the same item is never flagged again. You "
            "can also mute that sender, or their whole email domain, for you only; your mutes "
            "are listed on the page and you can remove one any time.\n"
            "'Not a real miss' is the only way to take an item off the list by hand, and it is "
            "counted: the share of flags marked that way is how the team tells whether the "
            "check is finding real misses or noise. An admin viewing Cubie as you sees your "
            "list without email subjects or summaries, and can't log to HubSpot, draft a "
            "reply, mark anything not a real miss or remove a mute for you.\n"
            "WHAT YOUR TEAM SEES, AND WHAT IT NEVER SEES. The Team tab, the Slack post, the "
            "scorecard and a team question in Ask Cubie show only WHO, WHICH KIND, WHICH "
            "COMPANY OR CONTACT (the names in HubSpot only: a name taken from an email or a "
            "meeting is never shown) and HOW LONG. They never show an email "
            "subject, a message preview or an AI summary; those are shown to you alone. The "
            "Team tab lists everyone A to Z, each person's items oldest first, all in the same "
            "colour. There is no ranking and no 'worst' list. For each person it shows how many "
            "are open, by kind and by age (0 to 2 days, 3 to 7, 8 to 14, 15 or more), how many "
            "are 7 days or older, how many closed this week, and whether Cubie could look at "
            "each of their sources. A health panel covers the loose ends flagged in the last 30 "
            "days, with each number out of its total, for example '3 of 41 flags were marked "
            "not a real miss', plus how many of those have closed and why, the typical days to "
            "close, and a weekly trend. Rescheduled ones are listed but never counted as "
            "closed.\n"
            "WHO HAS IT. Loose Ends starts open to everyone, once your company switches it "
            "on, and everyone who has it gets the morning check of their Gmail and Slack. Your "
            "admin can narrow it to the team that works client threads, or to people by name; "
            "admins and the Exec team are then checked only if they are on that list. The Team "
            "tab is for the managers and leaders added to it. Admins change either list from "
            "Admin → Feature Access.\n"
            "THE TIME LIMITS. 1 business day to log an email you sent before it counts as Not "
            "in HubSpot. 2 business days to answer a client before it counts as Waiting on "
            "your reply. 5 days past a promise's date, or 5 days after you said it if it had "
            "no date; a HubSpot task 5 or more days overdue, counting only tasks that went overdue "
            "in the last 30 days. A task's age counts calendar days: due on the 8th is 5 days "
            "overdue on the 13th, whatever the hour. 7 days: the age chip on a row is "
            "amber until the item is 7 days old and red from then on, with the number printed "
            "beside the colour ('4 days'), and the scorecard row counts the ones at 7 days or "
            "more. 60 days: anything still open after 60 days drops off as aged out; it is "
            "counted separately and never as fixed. A promise or HubSpot task whose date is "
            "moved later, so it is no longer five days overdue, leaves as rescheduled, also never "
            "counted as fixed. The age counts from when it happened (the "
            "email you sent, the message you received, the date you promised), not from when "
            "Cubie first noticed. A business day is counted the way My Day counts it: eight "
            "working hours in the company's business time zone, with weekends left out. "
            "Holidays and PTO are not subtracted yet.\n"
            "WHEN CUBIE COULD NOT LOOK. An empty list is not always 'you are caught up'. When "
            "a source could not be read, the page says so in its own words, and that source "
            "neither adds nor clears anything that morning:\n"
            "- Slack is not connected: your Slack messages were not checked. Connect it in "
            "Settings → Slack.\n"
            "- Your mailbox could not be read: nothing from email was flagged or cleared that "
            "morning.\n"
            "- Your mailbox was only partly read: there was more email in the last two weeks "
            "than Cubie reads in one morning (300 threads each for the inbox and sent mail), so "
            "the oldest threads were not checked.\n"
            "- Slack could not be read: for example, the Slack connection is missing "
            "permission to search your messages. Reconnect it in Settings → Slack.\n"
            "- No HubSpot owner matches your Cubie address: your clients' records are still "
            "checked, but your overdue HubSpot tasks are not. An admin can fix the match.\n"
            "- HubSpot could not be read: no Not in HubSpot items were flagged and nothing was "
            "cleared from HubSpot that morning.\n"
            "- HubSpot answered only in part: a very busy week can return more activity than "
            "HubSpot hands back at once, or an activity in a shared portal may not be provably "
            "yours. Cubie does not flag an email as Not in HubSpot when the proof could be in "
            "the part it could not check.\n"
            "- The AI check didn't run: no new Waiting on your reply or Slack items that "
            "morning; the ones already on your list are still re-checked.\n"
            "- The AI check could not get to everything: more possible loose ends were waiting "
            "than it reads in one morning; the rest are checked on the next mornings. Ones it "
            "already turned down are not asked about again.\n"
            "- Your scan could not finish: nothing was added or cleared that morning; what is "
            "on your list is from earlier scans.\n"
            "Every count says what it is out of, and the page says what was checked ('checked "
            "52 email threads, 31 Slack messages') and when.\n"
            "WHAT CUBIE READS. GMAIL: the last 14 days of your inbox and sent mail, leaving out "
            "chats, promotions, social and forum mail. For each message the morning check "
            "reads the sender, the recipients, the date, the subject and Gmail's short "
            "preview line, not the full email or its attachments. Your colleagues, automated "
            "and no-reply senders, and anyone you muted are left out. Only when you click "
            "Log it in HubSpot does Cubie read the full text of the emails you sent in that "
            "one thread, from your own mailbox, to show you what will be logged. SLACK: your own messages from the last 7 days, "
            "through the Slack connection in Settings. HUBSPOT: contacts, companies, deals, "
            "the emails, calls, meetings and notes logged on them, and your open tasks; it "
            "only writes to HubSpot when you click Log it in HubSpot, and then only the "
            "emails you sent, exactly as you sent them. ONE AI CHECK PER PERSON "
            "EACH MORNING decides which unanswered emails really expect a reply and which "
            "Slack messages describe a call or meeting with a client. Its one-line summary of "
            "a row is shown to you only. The Sales daily rhythm still answers 'what haven't I "
            "answered today?' in the moment; Loose Ends is the record that keeps what stayed "
            "open, day after day, until it is closed.\n"
            "WHAT IT CAN'T MEASURE. How many real misses it catches needs a hand check: pick "
            "one account executive, go through one day of their email and HubSpot by hand, and "
            "compare it with what Cubie flagged. Time saved is an estimate for the team to "
            "make, not something Cubie counts. A conversation that left no trace in your "
            "email, Slack or HubSpot is invisible to it; a phone call nobody logged never "
            "shows. What it does measure is noise: how many of its flags were marked 'Not a "
            "real miss', out of how many it raised. If that runs high in the first weeks, the "
            "two-business-day reply window is the first thing to change."
        ),
    ),
    HelpSection(
        id="handoffs",
        title="Handoffs (departed teammates)",
        keywords=(
            "handoff",
            "handoffs",
            "offboard",
            "offboarding",
            "departed",
            "left the company",
            "successor",
            "inherited",
            "inheritance",
            "dossier",
            "exit",
        ),
        body=(
            "When a teammate leaves your company, their open work lands with a named "
            "successor — usually their manager. Successors get a Handoffs item under MY WORK → HR & CULTURE.\n"
            "The dossier: at offboard time Cubie inventories everything the person left "
            "open — EOS to-dos, rocks and milestones, L10 meetings they ran, scorecard "
            "numbers they entered, HubSpot deals they owned, and commitments made in recent "
            "email — and writes the successor a short handoff brief. If IT had already "
            "suspended the Google account, the dossier says so: the email/calendar side may "
            "be incomplete, so also check the mailbox and files Google Workspace "
            "transferred.\n"
            "The checklist: each inherited item can be marked done, dismissed (not needed) "
            "or reopened, with links to the original thread, deal or document where one "
            "exists.\n"
            "Cubie keeps following up: open inherited items appear in the successor's daily "
            "AI Priorities as 'Inherited from …' cards until closed — closing the card and "
            "ticking the checklist are the same action, whichever happens first.\n"
            "Email and calendar commitments link to a source Cubie verified during the sweep; "
            "an older commitment without enough source information stays hidden.\n"
            "Past priorities: from the handoff the successor can read the departed person's "
            "old priority cards, read-only; these views are logged. Org-wide executives see "
            "every handoff, not just their own.\n"
            "HubSpot deal details and inherited cards remain visible only while Cubie can "
            "confirm that those deals still belong to your workspace. If access changes, an "
            "older item or part of the handoff may disappear; a past card without enough "
            "source information stays hidden.\n"
            "For admins: offboard from Administration → Users → Offboard, and run it BEFORE "
            "IT suspends the Google account — Cubie's last pass over the person's email and "
            "calendar only works while the account can still be read. The plain Delete "
            "action is for junk or test accounts only; if one has leftover priorities "
            "cards, Delete shows how many and asks a second time before removing it."
        ),
    ),
    HelpSection(
        id="insights",
        title="Insights dashboards",
        requires="insights",
        keywords=(
            "insights",
            "dashboard",
            "dashboards",
            "analytics",
            "adoption",
            "engagement",
            "who uses cubie",
            "who is using",
            "active people",
            "daily active",
            "stickiness",
            "dormant",
            "retention",
            "department comparison",
            "metrics",
            "kpi",
            "kpis",
            "acted on",
            "person profile",
            "people view",
            "summarize this view",
            "ai summary",
            "follow-through dashboard",
            "noise rate",
            "kept rate",
            "crm dashboard",
            "hubspot updates by cubie",
            "acceptance rate",
            "write failures",
            "proposals dashboard",
            "draft rate",
            "eos health",
            "ai spend",
            "cost per briefing",
            "cost per answer",
            "knowledge and content",
            "answers citing documents",
            "quality and trust",
            "trust scorecard",
        ),
        body=(
            "Insights (the Insights area in the left rail) shows what Cubie produces and how it is used, in ten dashboards:\n"
            "- Overview: the headline number from each dashboard, AI spend and AI spend per active person, and a department comparison table. Click a department's row to filter everything to it.\n"
            "- Adoption & Engagement: how many people used Cubie in the period, daily, weekly and monthly active people, stickiness (how many of the month's users come back on a given day), active people by department, which parts of Cubie each department uses, how well each week's new users keep coming back, when in the week people use Cubie (in the company's business time zone), people who started using it, and people who have not used it for 14 days or more.\n"
            "- Ask Cubie: questions asked, conversations, people asking, how many answers succeeded, how long answers took, the mix of Ask, Draft and Investigate, how often people speak instead of type, how answers were rated, and scheduled-task runs.\n"
            "- AI Priorities: what Cubie surfaced and what happened to it (acted on, closed by Cubie when it saw the work was done, dismissed, snoozed or still open), which sources the catches came from, how fast people act, briefings generated, Slack messages delivered, failed runs and ratings.\n"
            "- Follow-through: whether loose ends and promises are closing. For Loose Ends: how many were flagged, how many closed because the record shows the work done, how many were marked 'not a real miss' (the noise rate, which is how the team judges whether the check is accurate), how long closing takes, and how old the open ones are. For My To-Dos: promises kept out of those kept or dropped, promises slipping now, and promises made EOS To-Dos. Sales Cockpit commitments missed out of those due are shown per department only. The people table lists people A to Z, with no ranking.\n"
            "- CRM & HubSpot: what Cubie wrote into HubSpot after someone approved it (CRM Sync, call wrap-ups, Cockpit nudges, deal stage corrections, emails logged from Loose Ends), how many CRM Sync suggestions people approved out of the ones they decided on, how long approval takes, and how often a write failed.\n"
            "- Proposals & Prep: proposals started, drafted, revised and linked to a deal; Meeting Prep briefs, how often a brief is edited or rewritten, deep prep and failures; and Customer X-Ray snapshots.\n"
            "- AI Spend: what Cubie's AI cost in the period, per active person, by feature (named as in Admin → AI Cost; the smaller ones together as 'Other features'), by model and by department, and the cost of one result: per daily briefing, per Ask Cubie answer and per proposal drafted. The whole-company view counts every dollar; a department view counts only spend tied to a person in it, which is complete from the Insights release on. Costs include the models' thinking (see Admin → AI Cost).\n"
            "- Knowledge & Content: documents added, new versions uploaded, meeting transcripts added (per department only, never per person), knowledge entries Cubie proposed from email and how many were published, and how many Ask Cubie answers cited a document.\n"
            "- Quality & Trust: one trust scorecard across Ask Cubie, AI Priorities, Meeting Prep, Proposals, CRM Sync, Loose Ends and Customer X-Ray: how often each was rated useful, how often answers failed, cards dismissed, the briefing quality score, meeting briefs rewritten, proposals revised after the first draft, CRM Sync suggestions approved and loose ends dismissed as noise, each with what it is out of and the change from the period before.\n"
            "For how teams are running EOS (rocks, to-dos, issues, L10s), open the EOS Dashboard; Insights does not repeat it.\n"
            "Filters: pick one or more departments and a period (last 7, 30 or 90 days, quarter to date, year to date, or your own dates, up to a year). Every number is compared with the period of the same length just before it, and days are counted in the company's business time zone. Filters are kept in the page address, so a link opens the same view. Numbers refresh every 10 minutes; Refresh fetches them now.\n"
            "Every percentage says what it is out of ('12 of 15 people'), and every number has a definition behind its info icon.\n"
            "Who sees what: admins and company-wide executives see every department; anyone else sees the departments they manage plus their own. People with the people view also see per-person tables and can open a person's page: a 90-day activity calendar, their numbers on each dashboard, and the Cubie features they can reach versus the ones they use. Opening a person's page is recorded in the audit log.\n"
            "AI summary: 'Summarize this view' asks Cubie for a short written read of the dashboard you are on. It is given department totals only, never names, and any sentence with a number that is not on the page is removed.\n"
            "Where the numbers come from: sign-ins, Ask Cubie questions, AI Priorities cards and what people did with them, Meeting Prep, proposals, CRM Sync approvals, My To-Dos and, from the release of Insights onwards, a daily count of which parts of Cubie each person used. 'Active' means at least one of those on a calendar day in the company's business time zone. Counts only: no dashboard shows what anyone asked, wrote or said (never an email subject, a loose end's summary or a proposal's text), anything from recorded calls (call wrap-ups written to HubSpot, call scripts, Sales Cockpit commitments) is shown per department only and hidden for a department where fewer than 3 people record calls, and Smart Teams results never appear. Some sources are newer than others (Loose Ends counts only from the day your company turned it on); each dashboard says in a note where its data starts.\n"
            "Access is set by admins in Administration → Feature Access ('Insights dashboards' and 'Insights — people view')."
        ),
    ),
    HelpSection(
        id="smart-teams",
        title="Smart Teams",
        # Company-wide, so no `requires` gate — every user in the workspace can
        # take the interview and manage their own guide, skills and official
        # report (plan.md Decision 4). Compare and Team Builder are the
        # copilot-gated half; `requires` only knows "xray" and "crm_sync", so the
        # gate is stated IN THE BODY instead. That is the right shape anyway: a
        # user narrowed out of the copilot asking Chat "how do I compare myself
        # with someone?" needs the answer "that half comes with the copilot", not
        # silence — and silence is what withholding the whole section would give
        # them, along with the interview help they are entitled to.
        keywords=(
            "smart teams",
            "work style",
            "working style",
            "work style interview",
            "how to work with me",
            "personality",
            "disc",
            "strengths",
            "motivators",
            "enneagram",
            "profile",
            "guide",
            "my guide",
            "publish guide",
            "unpublish",
            "approve guide",
            "generate guide",
            "skills",
            "my skills",
            "declare a skill",
            "official report",
            "upload disc report",
            "assessment report",
            "team builder",
            "compare",
            "pair guide",
            "retake",
            "find people by skill",
            "who knows",
            "curate skills",
            "add a skill to the list",
            "raw profile lookup",
            "heatmap",
            "team map",
            "disc map",
            "where do we sit",
            "where do we overlap",
            "who covers what",
            "why is my guide stuck",
            "interview not scored",
        ),
        body=(
            "Smart Teams is your work-style profile — a 'How to work with me' guide built "
            "from your own answers, so colleagues can get the best out of working with you "
            "without having to guess.\n"
            "It starts with the Work Style Interview, under Teams in the left rail. "
            "Part of it is quick statements you rate on a scale (answerable straight "
            "from the keyboard — press the number and it jumps to the next statement), and "
            "part of it is a short conversation where you describe work you enjoy in your own "
            "words. It takes a little over half an hour end to end.\n"
            "You do NOT have to do it in one sitting. Every answer is saved as you give it, so "
            "closing the tab, coming back tomorrow, or being signed out half way through all "
            "resume on the statement you were on with your earlier answers still filled in. If "
            "a save fails you are told, with a Retry button — answers are never quietly "
            "dropped.\n"
            "When you finish, Cubie scores your answers in the background. That takes a few "
            "minutes and you can leave the page; the page says the scoring is still running "
            "rather than showing an empty profile.\n"
            "MY GUIDE — once your answers are scored, open My Guide under Teams in the "
            "left rail and press Generate. Cubie writes a 'How to work with me' guide "
            "from your own answers (how to "
            "reach you, what gets the best out of you, what drains you, how to give you "
            "feedback); it takes a couple of minutes. NOBODY SEES THE DRAFT BUT YOU — it stays "
            "private until you press Approve, which is what publishes it, and not even an "
            "admin can publish it for you: approving is your consent and there is no path that "
            "lets anyone else give it. You can unpublish at any time and colleagues stop being "
            "able to open it immediately. Approving also lets Cubie Chat answer from your guide; "
            "removing it from Chat is a background step, so for a short while after an unpublish "
            "Chat may still quote it — the page says which state it is in rather than claiming it "
            "is done. You can download the guide as a Word document. A retake makes a new version "
            "and marks the published one out of date (the page offers Regenerate); your published "
            "wording is never overwritten underneath you and the new one needs approving again. "
            "READING A COLLEAGUE'S GUIDE works the same way in reverse — you can open anyone's "
            "once they have published it, and you cannot tell whether somebody has an "
            "unpublished draft.\n"
            "MY SKILLS — a short list of what people can come to you for. Add them from a curated "
            "list (search it: 'gdpr' finds 'Data privacy (GDPR / CCPA)') and optionally say how "
            "deep you are on each. Cubie also SUGGESTS skills from your own activity at work "
            "— meetings you were in and documents you uploaded — "
            "and nothing outside your company is ever looked at. Each suggestion shows what it was based "
            "on, and that reasoning is private to you. A suggestion is invisible to colleagues "
            "until you confirm it. Saying no sticks: Cubie will not suggest it again, and that is "
            "also how you take back a skill you added yourself. Colleagues see only what you "
            "confirmed, with no evidence attached. 'Check for suggestions' asks Cubie to look "
            "again at your recent work.\n"
            "OFFICIAL ASSESSMENT REPORT (optional, and most people don't have one) — if you "
            "hold an official DISC report you can upload the PDF from My Guide and Cubie reads "
            "the scores out of it. It never changes anything on its own: it shows you what it "
            "found, you decide, and confirming replaces just that one part of your profile and "
            "leaves the rest alone. Your profile then changes, so your published guide comes out "
            "of company view until you regenerate and approve it — you are told that at the point "
            "you confirm. Nothing is waiting on you if you never upload anything.\n"
            "COMPARE AND TEAM BUILDER come with the copilot, which everyone has unless an "
            "admin has narrowed it. If they do not appear in your Teams panel, the copilot is "
            "not on for you; everything above is yours either "
            "way. COMPARE gives you a working-relationship guide about one colleague, written to "
            "you: where you two will click, where you will grate, and what to do about it. It "
            "needs BOTH of you to have published your own guide first, including you. It is only "
            "ever about you and one other person — there is no way to ask for a comparison of two "
            "other people, by design. TEAM BUILDER takes the skills a piece of work needs, plus "
            "dates if they matter, and proposes a group: who covers what, who is away, and what "
            "the group's mix of working styles adds up to. It never ranks or scores anybody, and "
            "an honest 'nobody has confirmed that skill' is an answer it will give. You can also "
            "just ask Chat for a team.\n"
            "FIND PEOPLE BY SKILL is the other direction: name what a project needs and see "
            "everyone who told us they do it. Everyday wording works ('gdpr' finds data "
            "privacy), up to six skills at a time. It lists EVERYBODY who said yes, in name "
            "order — it is not a shortlist and there is no 'best match', because this is not a "
            "ranking of people. It shows only what people confirmed about themselves, never a "
            "suggestion nobody has answered, and never what a suggestion was based on. If you "
            "type something the skills list does not know, it says so rather than showing you an "
            "empty result — 'nobody has this' and 'we do not track this' are different answers.\n"
            "SEEING WHERE PEOPLE OVERLAP: three screens draw the picture as well as "
            "describing it. My Guide shows your own shape on each scale, with your actual "
            "numbers, because it is your own profile. Compare puts you and one colleague "
            "side by side and marks each dimension Same, Close or Apart, so you can see "
            "where you will click and where you are most likely to grate — in WORDS, never "
            "numbers, in both directions. Team Builder adds a who-covers-what grid showing "
            "where a team doubles up and where one person carries a skill alone. Four "
            "All three also show a MAP: a circle with the four DISC letters on it and a dot "
            "for each person, on two axes — how much somebody pushes for progress, and how "
            "much they work through people. The dot is placed from BOTH working style and "
            "temperament, because both describe those same two things; what drives you and "
            "your Enneagram type are deliberately left out, because they are not positions on "
            "these axes and your motivations in particular only mean something next to your "
            "own others rather than next to a colleague's. Dots bunched "
            "together mean a group will agree quickly and miss the same things; spread out "
            "means more friction and better coverage. There is no ideal spot on the circle, "
            "nobody is ranked by where they land, and how far out a dot sits carries no "
            "meaning — only the direction. Two people with the same style sit on the same "
            "spot, which is the point. The coloured RING around a dot is what that person "
            "brings to a piece of work — Sense-Making, Momentum, Connection or Stewardship — "
            "and each always sits in the same place on the ring (top, right, bottom, left) so "
            "it can be read without relying on colour; no ring at all means their strengths "
            "section is closed rather than that they cover none of them. "
            "Somebody with no dot is NOT PLACED rather than in the "
            "middle: their answers did not support a working style, so there is no position to "
            "draw, and they are still listed beside the map.\n"
            "things worth knowing: a hatched 'Not measured' cell is NOT agreement, it "
            "means one of you has nothing reportable on that dimension so there was "
            "nothing to compare; the written guide's closing 'What this guide cannot speak "
            "to' list is the same idea in words — the questions Cubie could not answer "
            "because one or both profiles are missing the part behind them, listed so a gap "
            "does not read as 'nothing found here', and it names somebody as having "
            "something still to finish only when they actually do (a section that was "
            "declined, or one derived from another section, leaves nothing to do and names "
            "nobody); there is no compatibility score and there never will be, "
            "the grid counts dimensions and names them rather than adding them into a "
            "verdict about a relationship; 'What drives you' appears only on your own "
            "guide because it is an order rather than a level and only means something "
            "next to your own others; and Compare's grid needs BOTH of you to have "
            "published your own guide, though it does not need anybody to have generated "
            "the written guide first, so you can look before you ask for one.\n"
            "PRIVACY: the interview transcript and the numeric scores behind it are visible "
            "only to you and to the admins who support Smart Teams — every admin unless "
            "your company has handed support to one team's admins in Admin → Feature "
            "Access (Smart Teams support view) — and never to your manager or the Exec "
            "team for being senior. They reach that support view from a 'Support: raw "
            "profile lookup' link on the My Work Style page; it is read-only, every lookup is logged "
            "against the admin who ran it, and it exists for fixing a problem WITH the person it "
            "belongs to. Your guide becomes company-visible only after you have read and approved "
            "it, and you can unpublish it at any time. Colleagues never see a number, a "
            "percentile or a raw score of yours anywhere in Smart Teams. A Compare guide is never "
            "searchable in Chat and has no download, because a saved file would outlive the "
            "moment either of you unpublishes. You can retake the interview whenever you like — a "
            "retake creates a new version and needs approving again rather than overwriting what "
            "is published.\n"
            "ADVISORY ONLY: Smart Teams exists to help people work together. It is not used "
            "for performance reviews, promotion, compensation, or deciding whether someone is "
            "right for a role. It is also self-given only — Cubie does not research colleagues "
            "to build these, and nobody can add a skill, a guide or an answer on your behalf.\n"
            "MISSING A SKILL FROM THE LIST? The admins who support Smart Teams can add one — "
            "there is a 'Curate the list' link on My Skills for them — and it is usable "
            "straight away, without waiting "
            "for a release. Ask them rather than picking the nearest wrong thing: the list is "
            "shared, so a skill stored eleven different ways stops Find People and Team Builder "
            "working. Skills can be retired but never deleted, because somebody may already have "
            "claimed them.\n"
            "LIMITS WORTH KNOWING: the curated skills list is loaded as a setup step, so if it "
            "looks empty it is not in place rather than having no match for you (the page says "
            "which), and that is worth reporting to an admin rather than waiting for it. Skill "
            "suggestions are worked out overnight and whenever you press 'Take a fresh look'. The "
            "nightly pass only re-reads people whose meetings or documents actually "
            "changed, so a quiet week legitimately adds nothing — press the button to have it "
            "looked at now. Only official DISC reports can be read today — "
            "other assessments are recognised and named to you but cannot be merged. And a Team "
            "Builder answer is not saved: it answers the question you just asked, so copy "
            "anything you want to keep."
        ),
    ),
    HelpSection(
        id="scheduled-tasks",
        title="Scheduled tasks",
        requires="scheduled_tasks",
        keywords=(
            "schedule",
            "scheduled",
            "scheduled task",
            "recurring",
            "every morning",
            "every day",
            "every weekday",
            "weekly",
            "daily",
            "automatic",
            "automation",
            "routine",
            "reminder",
            "digest",
            "cron",
            "pause",
            "resume",
            "run now",
        ),
        body=(
            'Ask Cubie in Chat to do something on a repeating schedule and it will — "every weekday at 8am send me my important emails", "each Monday summarize last week\'s meetings", "on the 1st, remind me to review the pipeline". Cubie confirms the schedule in plain English, then runs it for you at that time in your own timezone, with no browser open and nothing to click.\n\n'
            'Each run arrives as its own new conversation in a chat folder called "Scheduled tasks", named with the task and the date, plus a Slack direct message. You can open the conversation and keep asking questions about the result, exactly like any other chat. Ask for email delivery and Cubie will send it there too, where that has been switched on.\n\n'
            'You do not have to go looking for a result. When a run finishes, a lavender dot appears on Chat in the left sidebar and on the conversation itself, and the "Scheduled tasks" folder shows how many results you have not opened yet. Opening the conversation clears its dot. If a task fails often enough that Cubie pauses it, the dot appears on Settings → Scheduled tasks instead, because there is no result to read — only a task to look at. You can turn these off under Settings → General → Notifications.\n\n'
            "What a scheduled run can do: read your email, calendar, meeting transcripts, HubSpot, Slack, the knowledge base and your EOS data — the same sources Chat uses, as you, with your own permissions. What it cannot do: send an email or book a meeting. Nobody is there to approve those, so a scheduled run prepares work and never commits it. It can still leave a draft in your Gmail drafts.\n\n"
            'Write the request so it stands on its own. A run has no memory of the conversation you set it up in, so "the same as yesterday" gives it nothing to work from; Cubie turns what you say into a full instruction and shows it to you.\n\n'
            "Before it saves a new task, or a change to one, Cubie shows you a card with the task's name, the full instruction, when it runs and where results go, and saves it only when you click Yes, go ahead. Read the instruction: it runs later with your access and nobody watching, so say no to anything you did not ask for. Pausing, resuming and running a task now need no OK; deleting one does.\n\n"
            'To see or change what you have scheduled, say "what have I scheduled?" in Chat, or open Settings → Scheduled tasks. There you can pause a task (for a vacation, say), resume it, run it once immediately, or delete it. Pausing keeps everything and stops the runs; deleting is permanent, though the results you already received stay in your chat history. There is a limit on how many tasks one person can keep, because every task is a full Cubie run each time it fires.\n\n'
            "Timing is approximate to within about five minutes: 8:00 means between 8:00 and 8:05. If a task fails several times in a row Cubie pauses it and tells you, rather than failing quietly forever. If a run has nothing to report — a genuinely quiet inbox — it says so instead of inventing something. Unread results also show in the bell in the left sidebar; the chat conversation and the Slack DM are still where you read the full result.\n\n"
            "Scheduled tasks are being rolled out team by team, so the option appears in Chat only once your team has it."
        ),
    ),
    HelpSection(
        id="settings",
        title="Settings",
        keywords=(
            "settings",
            "timezone",
            "working hours",
            "notifications",
            "bell",
            "notification bell",
            "personalization",
            "tone",
            "signature",
            "connect slack",
            "language",
            "verbosity",
            "preferences",
        ),
        body=(
            "If you have Settings in your left rail, open it there — its sections open in "
            "the panel beside the rail, so you move between them from there rather than from "
            "inside the page. It is also still in the account menu at the bottom-left. "
            "Settings comes with the copilot, which everyone has unless an admin has "
            "narrowed it:\n"
            "- General — your timezone (so AI Priorities and the daily briefing match your "
            "actual day), your working hours (Cubie keeps focus blocks and free-slot "
            "suggestions inside them), and which notifications reach the bell in the left "
            "sidebar. If page tours are on for your team, Page tours is here too: untick it "
            "to stop tours from starting by themselves, or click Show every tour again to "
            "see each page's tour the next time you open it. Both take effect at once, "
            "without the Save button. If Cubie for Chrome is on for your team, its card lists "
            "every browser connected to your account, with when it was connected and last "
            "used: Disconnect turns one off at once, and Connect this browser adds the one you "
            "are using.\n"
            "- AI Personalization — tell Cubie who you are and how you like to work, and it "
            "shapes Chat answers, AI Priorities and meeting prep. Set your role and a short "
            "'about you & focus', choose your preferred answer length and reply language, "
            "and tune AI Priorities (a standing focus, how many cards, which sources to draw "
            "from, topics to mute). You can also reopen priorities you dismissed or "
            "snoozed.\n"
            "- Email Preferences — the voice Cubie uses for every email and meeting brief it "
            "drafts (tone, writing style, preferred greeting). 'Preview my style' shows a "
            "sample without saving or sending. Your Google Workspace signature, including "
            "the confidentiality disclaimer, is added automatically to every draft, so you "
            "don't set it here. Your workspace's Sales email model also powers this preview. "
            "If that selected provider cannot answer, the preview shows an error so your "
            "admin can check its connection in Admin → Workspace. If your admin enables "
            "multiple Gmail accounts, this section "
            "also lets you connect or remove your own extra mailbox. Google asks you to "
            "authorize its Gmail access separately from Cubie sign-in. Extra mailboxes "
            "are selected explicitly in Productivity Email and never become your "
            "default work mailbox. They join your own AI Priorities and scheduled "
            "summaries with their mailbox label. The workspace's selected AI provider "
            "processes included mail to create those priorities. If an admin changes "
            "that model, Cubie pauses automatic use of your extra mailbox and sends you "
            "a notification. Review the new provider in Settings → Email to include "
            "that mailbox again. If the selected provider is unavailable, automatic "
            "use stays paused until your admin restores it. You can still open it "
            "manually or disconnect it.\n"
            "- Slack — connect or disconnect your Slack account. Once connected, Cubie "
            "surfaces your Slack @-mentions in AI Priorities and answers Slack questions in "
            "Chat. Cubie connects AS YOU, so it only ever sees the channels and DMs you're "
            "already in; private channels you're not a member of stay hidden, and your Slack "
            "messages are never copied into the shared knowledge base. Connect here before "
            "using Cubie's /todo or /ids Slack commands. If your company's Workspace domain "
            "is removed, the connection stops while access is unavailable and works again "
            "if an admin restores that domain."
        ),
    ),
    HelpSection(
        id="tips",
        title="Tips & feedback",
        keywords=(
            "tips",
            "feedback",
            "thumbs",
            "rating",
            "logout",
            "logged out",
            "login",
            "session",
            "timeout",
            "signed in",
            "sign out",
            "hours",
            "follow-up",
            "support",
            "contact",
            "not working",
        ),
        body=(
            "- Use the 👍 / 😐 / 👎 buttons under Chat answers, on AI Priority cards, and at "
            "the bottom of a Meeting Prep briefing. Ratings guide future answers and help "
            "admins spot quality issues; older answer text and comments are not replayed "
            "into new prompts.\n"
            "- You never upload meeting transcripts; they sync on their own every ~15 "
            "minutes.\n"
            "- Sign in with your company's Google Workspace account. Cubie checks the "
            "verified company domain and opens only that company's workspace. Your company may "
            "have more than one verified sign-in domain. If Cubie "
            "says your company is not set up or is unavailable, use another account or "
            "contact your admin; no workspace data is opened. Gmail, Calendar and Drive "
            "use that account; Slack needs connecting in Settings.\n"
            "- A developer's local preview can show 'Enter local preview' on the sign-in "
            "page. It works only in the development build with local mock authentication "
            "and a provisioned test workspace.\n"
            "- You stay signed in for 5 hours at a time. When the "
            "session ends, Cubie brings you back to the sign-in page; one click on 'Sign in "
            "with Google' and you're back. Admins can change the session length in "
            "Administration → Google Workspace.\n"
            "- Ask follow-up questions naturally — Chat remembers the conversation.\n"
            "- Questions or something not working right? Reach out to your "
            f"{PRODUCT_NAME} admin. Admins can write to {VENDOR_NAME} support at "
            f"{SUPPORT_EMAIL}."
        ),
    ),
    HelpSection(
        id="voice-commands",
        title="Voice commands",
        requires="voice_commands",
        keywords=(
            "voice",
            "voice command",
            "voice commands",
            "speak",
            "say",
            "talk",
            "microphone",
            "mic",
            "shortcut",
            "keyboard shortcut",
            "option command",
            "ctrl alt",
            "hands free",
            "dictate",
            "add a rock by voice",
            "open a page",
        ),
        body=(
            "Run Cubie by speaking: press Voice (the microphone above Notifications in the "
            "sidebar) or the keyboard shortcut, Option+Command on a Mac and Ctrl+Alt on a PC, "
            "and say what you want. It works on every page, including during an L10. Tap the "
            "shortcut, speak, then tap it again or press Enter; or hold it while you talk and "
            "let go. Esc throws it away, and a recording stops by itself after 30 seconds "
            "without doing anything.\n"
            "What you can say: open a page ('Open my work', 'take me to Rocks', 'I want to "
            "write a proposal'), only pages in your own menu; open one record by what it is "
            "about ('open Acme's X-Ray', 'open Thursday's Sales L10', 'prep for my 2pm', "
            "'open the Globex proposal', 'open my chat about pricing', 'open Jordan's work "
            "style guide'), only records you can already open, with a list when several fit "
            "and a question first for an L10, since opening it starts the meeting; ask a "
            "question, which goes to Ask Cubie as your message (so does anything else "
            "Ask Cubie does, like a draft email "
            "or your own My To-Dos); or change EOS: add a Rock, mark a Rock on track, "
            "off track or done, add a To-Do, mark a To-Do done, add an Issue, or record "
            "a Scorecard number.\n"
            "Nothing in EOS changes until you confirm. Cubie shows "
            "a card with exactly what it will do (the Rock, quarter, owner and team; the "
            "To-Do's owner and due date; the number and any number it replaces). Press Enter or the button, or press the "
            "shortcut again and say 'yes'; Esc or 'no' cancels. The change is made exactly as "
            "the page's own button makes it, as you, so you can only change what you could by "
            "clicking. When your words fit more than one record or person Cubie asks which; "
            "when they fit none it says so. Unless you say otherwise a Rock or To-Do is yours, "
            "a To-Do is due a week out, an Issue or To-Do goes to the team the page's own form "
            "picks first (the Exec team, for an exec), and a Rock to its owner's team; say the "
            "team to choose another. Scorecard numbers are weekly ones only.\n"
            "Change the shortcut or turn it off in Settings → General (Change, then press the "
            "keys you want); a Mac and a PC keep their own. The recording is never stored, and "
            "a recording nobody spoke in never reaches the AI. If Cubie takes too long to work "
            "out a command it says so and nothing is done; just say it again."
        ),
    ),
    HelpSection(
        id="whats-new",
        title="What’s new",
        # Off with the WHATS_NEW_ENABLED kill switch as well as off the roster
        # (tools.py gates on both), because the drawer answers 404 then.
        requires="whats_new",
        # Distinctive words only: every word of a keyword also scores on its
        # own, so "slack", "approve", "card", "mark", "tab", "now" or "change"
        # (even inside a phrase) would pull ordinary questions about other
        # features here; the body still carries those phrases. The title
        # uses a curly apostrophe and the keyword is spelled "whats new": a
        # straight "what's" is a token of its own, and it matched every
        # "what's the…" question. "What's new?" still lands here through the
        # body, which carries the phrase.
        keywords=(
            "whats new",
            "new updates",
            "changelog",
            "announcements",
            "spotlight",
            "drawer",
            "tease",
        ),
        body=(
            "What's new is the list of Cubie's release notes: each change to Cubie that "
            "matters to you, explained in a sentence or two. RubikSoft, the team that "
            "makes Cubie, writes each one. Open it from the What's new button at the "
            "bottom of the left-hand menu, just above your name; it opens from the right "
            "on whatever page you are on.\n"
            "THE BUTTON AND THE DRAWER — the button shows how many updates are new since "
            "your last visit. The drawer lists the updates newest first, each with its "
            "date, a title and a short description, and a dot marks the ones you have not "
            "seen. Many end with a 'Try it' link that takes you to the place it "
            "describes. An update about a feature only some teams have says so ('For "
            "Loose Ends users'): you only see news about features you can use, and "
            "nothing about a feature that is switched off here. 'Mark all as seen' "
            "clears the count (the drawer then says 'You're all caught up.'), and so does "
            "closing the drawer after reading it. New means published since your last "
            "visit, within the last two weeks. Cubie remembers what you have seen in this "
            "browser, so on another computer the same updates show as new once more. "
            "When there is nothing to read yet, the drawer says 'No updates yet.'\n"
            "ONCE A WEEK, IT OPENS BY ITSELF — the first time you open Cubie in a week, if "
            "there is something new since your last visit, the drawer slides open by itself "
            "for about five seconds and closes again. It doesn't cover the page, take the "
            "cursor or count as reading anything; point at it or tab into it and it waits, "
            "click anything inside and it stays open. It never does this on a live L10 "
            "meeting page, on a narrow screen, when you arrived from a link to one update, "
            "after you already opened the drawer, while another window is open over the "
            "page, or while an admin is viewing Cubie as you. If Cubie is open in a browser "
            "tab you are not looking at, it waits until you come back to it. An admin can "
            "switch it off for everyone.\n"
            "ON YOUR DAILY BRIEFING — if you have the Daily Briefing, a one-line spotlight "
            "bar under its heading, 'New since your last visit', names the newest update you "
            "have not seen and how many more there are. 'See what's new' opens the drawer; "
            "the x hides the bar until the next update arrives. A What's new button next to "
            "Refresh opens the drawer too.\n"
            "THE MORNING POST IN SLACK — when an admin has turned it on, each weekday "
            "morning Cubie posts one message in the chosen Slack channel listing the "
            "updates published since the last post, one line each: the title, a sentence "
            "or two and a 'See it' link. A morning with nothing new posts nothing. The "
            "link opens Cubie with the drawer on that update, marked 'You came here from "
            "Slack'; if you were signed out you land on it after signing in. If the update "
            "is about a feature your team does not have, the drawer says 'This update is "
            "for another team.' instead.\n"
            "COUNTS OF PEOPLE, NEVER WHO — Cubie counts how many people opened each update "
            "from Slack or with 'Try it', once per person, and keeps a scrambled code for "
            "each instead of your name, only so it can count each person once. No screen "
            "shows who opened what.\n"
            "FOR ADMINS — Admin → What's New, in the Administration section (admins only), "
            "holds the two settings that apply to everyone: 'Post new updates in Slack "
            "each weekday morning' with its channel (#announcements unless changed; "
            "create the channel in Slack and type /invite @Cubie in it, then Save: Save "
            "checks the channel exists, is not archived and has Cubie in it, and says what "
            "to fix if not), and 'Tease the drawer the first time someone opens Cubie each "
            "week'. The tab shows who last changed it and when, every save is in the audit "
            "log, and nothing needs a new release. Nobody here writes or approves the "
            "updates: they come from RubikSoft.\n"
            "Open to everyone by default; an admin can change it under Administration → "
            "Feature Access. An admin viewing Cubie as someone else does not see the "
            "button."
        ),
    ),
    HelpSection(
        id="admins",
        title="For admins & execs",
        keywords=(
            "admin",
            "administration",
            "exec",
            "roles",
            "permissions",
            "team manager",
            "ai cost",
            "who can see",
            "visibility",
            "pilot",
            "access",
            "feature access",
            "onboard",
            "onboarding",
            "department",
            "ingestion council",
            "slack installation",
            "fathom webhook",
        ),
        body=(
            "Some areas appear only for certain roles:\n"
            "- Administration (admins only) — grouped into five sections. Workspace: "
            "Company setup (the company profile, product catalog and proposal templates). "
            "Access: users, roles, Google Workspace, access control, feature access. AI "
            "Settings: AI Priorities tuning, "
            "the chat model, the sales models, AI cost. "
            "Productivity: Drive labels and "
            "department folders. Admin: departments, L10 meetings, the Loose Ends daily "
            "post, What's New, login activity and feedback. Departments "
            "used to be its own menu item and now lives under Admin. Workspace admins "
            "can also enable additional Gmail accounts for members; it starts off, and "
            "members connect their own accounts under Settings. All five groups appear "
            "in the panel beside the rail when you click Admin, so the page itself is just "
            "the section you picked.\n"
            "- Impersonating a person (named admins only) — the impersonation menu at the "
            "bottom of the panel lists the people your deployment names for it, if any, "
            "above the departments. Picking one shows Cubie as that person sees it: their My Day, "
            "sales cockpit, daily rhythm, EOS and call scripts. It is read-only — nothing "
            "can be saved, sent or changed as them — and it leaves out their email, "
            "calendar, Drive, call transcripts, the home feed and the briefing. Ask "
            "Cubie works in a HubSpot-only form: it answers questions about their "
            "deals, pipeline and contacts as them, and saves nothing to their chat "
            "history. A banner says whose view you are in, with a button to stop. Every "
            "impersonation is recorded in the audit log.\n"
            "- Platform support is different from viewing as a person. An authorized "
            "RubikSoft platform administrator can enter a workspace for a short support "
            "session. A persistent banner says 'Acting in <Workspace> as RubikSoft "
            "support' and offers Exit. Each change is recorded in that workspace's "
            "audit log under the support actor before it runs and again with its "
            "result. Support cannot open a teammate's delegated Google mailbox, "
            "calendar, Drive or directory account.\n"
            "- Feature Access (Admin → Feature Access) — who can reach each feature: the "
            "copilot, Customer X-Ray, CRM Sync, the Sales Cockpit views, Sales Call Capture, "
            "Smart Teams and the rest. Everyday features start open to everyone; narrowing one "
            "to some departments, or adding a department or a person to a feature that starts "
            "closed, is a checkbox that takes effect on teammates' "
            "next page load — no code change, no deploy. Every card shows the roster in "
            "force, whether it is the shipped default or a customized one, whether the "
            "feature is switched on in this environment, and exactly who has access today; "
            "editing previews who gains or loses access before saving, and every change is "
            "logged with a note. The workspace's module switch must also be on: "
            "the core app starts on, while EOS and Sales Coach are enabled by the "
            "platform administrator. A roster cannot turn an off module on. "
            "Who each feature starts with is described below — an "
            "admin may have changed it since.\n"
            "- Workspace identity (Admin → Workspace) shows the verified sign-in "
            "domains, primary domain and enabled modules. These are platform-owned "
            "and read-only here; ask your platform administrator to change them. "
            "Workspace admins can still manage feature rosters and session length.\n"
            "- HubSpot settings (Admin → Workspace) names the deal pipelines this "
            "workspace uses. Platform support assigns those pipelines; until then Cubie does "
            "not read or update deals. Workspace admins set won stages, custom fields, "
            "HubSpot links and Scorecard mappings here; a missing mapping leaves "
            "that action off. A company or contact must belong wholly to those pipelines "
            "before Cubie can read or update it. Connecting the HubSpot account "
            "is a separate step.\n"
            "- Workspace operations (Admin → Workspace) sets this "
            "company's business time zone, ingestion health report audience, "
            "Insights exclusions, Deep Prep sender and L10 recap sender. L10 recap "
            "delivery stays off until an admin enters its start date. Person "
            "impersonation grants are managed by platform support.\n"
            "- AI provider credentials (Admin → Workspace) lets "
            "this workspace connect its own Claude, DeepSeek, OpenAI, Gemini API, "
            "Muse or Vertex AI account. API keys are write-only workspace secrets; "
            "client Vertex uses an authorized service identity rather than a "
            "browser-uploaded private key file. Compatible models can run Chat, "
            "Support chat, Sales email assistant, Overnight pipeline, Chat web "
            "research, CRM retrieval, AI Priorities, recaps, X-Ray, summaries, "
            "extraction and scheduled work after a capability test. Chat needs tool "
            "calls and streaming; web research needs Google Search grounding, so "
            "the compatible model list changes with the workload. Each choice "
            "applies only to that workload; a client "
            "provider failure is shown instead of falling back to Cubie's "
            "managed Vertex account. The provider bills its account owner; AI "
            "Cost estimates are separate from the provider invoice. Knowledge "
            "search embeddings must match the vector index. Select a knowledge "
            "collection; Rebuild index verifies a new version before Activate "
            "switches that collection. OpenAI and Gemini embeddings use the "
            "workspace credential. Cloud workspaces keep client-owned vectors "
            "in their own Cloud SQL database and sync document changes. A failed "
            "index or selected key stops search for that collection rather than "
            "using a different provider.\n"
            "- Slack and Fathom connections: each workspace has its own Slack app "
            "installation. Connect while signed in to the matching Slack account; "
            "Cubie verifies the Slack identity before saving the connection. People must "
            "connect in Settings before using slash commands. Signed commands and forms "
            "use that installation's workspace. Removing a verified Workspace domain "
            "stops its connections while access is unavailable. Older connections from "
            "before this update need to be connected again. For Fathom, "
            "configure this workspace's API key and signing secret. Its webhook URL "
            "is the Cubie service URL plus /api/fathom/w/<workspace-slug>/webhook; "
            "the shared URL no longer receives events. A signed completed meeting "
            "starts an import only when its host has Fathom import access.\n"
            "- AI Priorities models (Admin → AI Priorities) — which Gemini model runs "
            "each step of the daily briefing: the readers that go through each source, "
            "and the three steps that write the briefing (the summary, the ranking and "
            "the review). Besides the Flash and Pro tiers, any step can run on Gemini 4 "
            "Argon at high thinking. A change applies to the next briefing, with no "
            "deploy.\n"
            "- Sales Models (Admin → Sales Models) — which Gemini model powers each "
            "sales feature: proposal drafting, call wrap-up and scripts, the five steps "
            "of a Customer X-Ray report, and CRM Sync. The same page also sets the model for the Deep Prep brief, which "
            "every team gets, not only Sales: Gemini 2.5 Pro (the default), Gemini 4 "
            "Argon at high thinking, or Gemini 3.8 Flash, the three tested side by side "
            "on real briefs. Each job is set separately because they are not equally demanding — "
            "the steps that read one source at a time are cheap and run many times per "
            "report, while the steps that write something a client reads are worth a "
            "stronger model. A change applies to the next run, with no deploy, which is "
            "also the fastest way to keep sales working if a model starts rate-limiting: "
            "move the affected job down a tier and save. Only models with reserved "
            "capacity are offered, and web research is limited to the ones that can "
            "search the web. 'Reset to defaults' restores the shipped choices.\n"
            "- Loose Ends daily post (Admin → Loose Ends) — whether the daily Loose Ends "
            "summary is posted to Slack, and to which channel. It ships OFF, pointed at "
            "#sales. The post lists each person's name, how many loose ends of each kind "
            "they have and how many are 7 days or older — never an email subject, a "
            "message or an AI summary. A quiet day (nothing open and everyone checked) "
            "posts nothing; if someone could not be checked, the post goes out to say so. "
            "To turn it on, type /invite @Cubie in the channel first, then switch the post "
            "on and Save: Cubie checks with Slack that it can see the channel and is in "
            "it, and says exactly what to fix if not. 'Preview today's post' shows what it "
            "would say without posting anything. Turning it off, or changing the channel, "
            "takes effect on the next morning's scan with no deploy, and every change is "
            "recorded in the audit log. If Slack refuses a morning post (Cubie was removed "
            "from the channel, or the channel was archived), the card says so and names "
            "the fix: after inviting Cubie back, press 'Save and re-check'; for an "
            "archived channel, pick another one and Save.\n"
            "- What's New (Admin → What's New) — the two settings behind the What's new "
            "drawer that apply to everyone: the weekday morning Slack post of new release "
            "notes, with its channel (#announcements unless changed), and the weekly "
            "slide-open of the drawer. To turn the post on, type /invite @Cubie in the "
            "channel first, then switch 'Post new updates in Slack each weekday morning' "
            "on and Save: Cubie checks with Slack that the channel exists, is not "
            "archived and has Cubie in it, and says exactly what to fix if not. The "
            "release notes themselves come from RubikSoft; nobody here writes or approves "
            "them. The card shows who last changed it and when, every save is recorded in "
            "the audit log, and nothing needs a new release.\n"
            "- AI Cost (Admin → AI Cost) — how much every AI feature is spending. Each "
            "Gemini call is metered and attributed to the feature that triggered it (Chat, "
            "AI Priorities, Meeting Prep (with one-section rewrites on their own row), Deep "
            "Prep, Customer X-Ray, EOS, Slack summaries, email, the overnight pipeline and "
            "more), with token usage and estimated dollar "
            "cost over 7/30/90 days, a daily trend, a per-model breakdown and a CSV export. "
            "Dollar figures use an editable Vertex rate card on the page. Spend that can't "
            "be matched to a feature lands in an 'Unattributed' row you can expand to see "
            "the exact code paths responsible. Costs include the models' thinking: before "
            "it answers, a model like the one behind AI Priorities and Deep Prep works the "
            "problem through in tokens nobody sees, and Google bills those at the same "
            "rate as the answer. The Thinking column shows them for each feature. Ask "
            "Cubie answers and scheduled tasks are counted in full: to answer, Cubie often "
            "looks things up first, and each step is its own model call, counted in the "
            "Chat and Scheduled Tasks rows. Both rows are priced at Google's lower rate for "
            "the part of each request Cubie reuses from its cache, instead of the full "
            "rate.\n"
            "- Org-wide Exec users see every department's content (chat, documents, EOS) but "
            "by design do NOT get the admin panel. Meeting transcripts are the exception: "
            "everyone, execs included, only sees meetings they hosted or attended; only "
            "admins see all transcripts.\n"
            "- Team Managers — an admin can make anyone the manager of one or more teams "
            "(Admin → edit a user → 'Team Manager — departments this person leads') WITHOUT "
            "adding them to the Exec team. A Team Manager runs their team's L10 and can see "
            "and manage that team's Rocks, Issues, To-Dos and Scorecard, but never the "
            "confidential Exec surface (Leadership Scorecard, Exec L10). A leader who runs a "
            "team they don't sit in themselves also appears to that team: they can be "
            "checked into its L10, handed the meeting, and assigned its items. A team's L10 "
            "Check-in list offers everyone on the meeting invite, guests included.\n"
            "- Anything marked Exec team anywhere in EOS — issues, to-dos, measurables, "
            "rocks, the Exec L10 and everything in it — is visible to Exec team members "
            "only, enforced server-side. Only Exec members can file items under the Exec "
            "team, and only they can import the ET L10 (it's recognized by name and scoped "
            "to the Exec team automatically, pulling the Exec team's Rocks, Issues and "
            "Scorecard). A meeting can be moved onto or off the Exec team by its manager "
            "(Meetings → edit → Scope), but only by an Exec member.\n"
            "- Who sees an L10 meeting: a meeting IMPORTED from Google Calendar is visible "
            "to the people on its calendar invite and nobody else — being on the same team "
            "as the organizer is not enough. (The team on the card only tells the meeting "
            "which team's Rocks, Issues, To-Dos and Scorecard to pull in.) A meeting "
            "scheduled by hand in Cubie has no invite list, so it stays visible to its whole "
            "team. To bring someone in, add them to the Google Calendar event and click "
            "'Sync' on the meeting card. No team sees another team's L10, with one "
            "exception: an Org-wide Exec (flagged 'All departments' — in practice the CEO) "
            "sees every team's L10. Being an admin, an exec for one department, or a Team "
            "Manager does NOT open another team's L10.\n"
            "- Who can change who RUNS an L10: its current manager, your team's leader (a "
            "Team Manager of that team, or the holder of a leadership seat on it), or an "
            "admin. Any of them can open Meetings → Edit and pick a new manager.\n"
            "- If the manager is away and never handed the meeting over, the team is not "
            "stuck: click the person icon on the meeting card ('Manager away?'). Cubie "
            "checks whether the manager can still be reached, and if they're out of office, "
            "have left the company, or the meeting has no manager at all, you can take the "
            "meeting over yourself so your team can run its L10. You can only take it over "
            "FOR YOURSELF — handing it to a third party stays with the manager, your team's "
            "leader, or an admin — and every takeover is logged. If the manager is actually "
            "available, Cubie says so and asks you to go through them instead. The Exec L10 "
            "is excluded: only Exec team members can re-point it.\n"
            "- Meetings close themselves. An L10 is over when its facilitator clicks "
            "Conclude, but if nobody does, Cubie ends it for you overnight — once the "
            "meeting is well past its scheduled length AND has had no activity for "
            "several hours. That is why a meeting you left open days ago no longer "
            "shows a green Rejoin, and why starting it next week gives you a clean "
            "agenda from the top instead of picking up mid-meeting where the last one "
            "was abandoned. A meeting still running long is never cut off — both "
            "conditions have to be true. Concluding it yourself is still worth doing: "
            "only that collects everyone's 1-10 rating, so an auto-closed meeting has "
            "no meeting score.\n"
            "- Admins have an L10 Meetings tab (Admin → L10 Meetings) listing every meeting "
            "in the company with its team, schedule, and manager, so an admin can reassign "
            "an abandoned meeting for a team they're not on. It shows structure only — no "
            "meeting notes, diaries, headlines or attendee names — so team privacy is "
            "unchanged.\n"
            "- The EOS Dashboard is limited to execs/admins. The VTO is company-wide — "
            "everyone sees its open lines; only lines the Exec team marks Confidential (and "
            "the upload/edit controls) stay execs/admins-only.\n"
            "- EVERYONE WHO SIGNS IN WITH A COMPANY ACCOUNT GETS THE APP, with or without a "
            "department. The AI copilot (Daily Briefing, Chat, Productivity, Assets, Meeting "
            "Prep), EOS and the everyday features start open to everyone. An admin can narrow "
            "any of them to some departments in Feature Access; a teammate left out of the "
            "copilot keeps EOS and Help. That includes the overnight AI Priorities: it "
            "reaches everyone until an admin narrows it, which is how to cut its "
            "overnight AI cost. Someone left out of it, or out of the copilot, has "
            "nothing read overnight.\n"
            "- SOME FEATURES START CLOSED and open by role: the Insights dashboards and the "
            "cockpit's exec roll-up to the Exec team and admins, the cockpit's team roll-up "
            "and nudge worklist to admins until a team is added. Views of other people's work "
            "(the weekly close-date roll-up, the team versions of the daily rhythm and Loose "
            "Ends, every team's Rocks) and the Fathom import (on sign-in, and as each call "
            "they host ends) start with nobody: an admin adds the people or the team. "
            "Loose Ends itself starts open to everyone, and everyone on it gets a morning "
            "check of their Gmail and Slack: narrow it to the team that works client "
            "threads if that is who it is for. This is "
            "separate from each feature's own on/off switches: those decide whether it runs "
            "at all, Feature Access decides who may reach it once it does.\n"
            "- Ingestion Council — each night (and weekly for EOS), a panel of AI auditors "
            "reviews Cubie's own data-ingestion runs the way a QA team would, checking that "
            "meeting transcripts, AI Priorities and EOS content actually landed, stayed "
            "searchable, and kept to their access rules. It DMs the people set up to receive "
            "it (by default, the workspace's admins) a PASS / WARN / FAIL health verdict."
        ),
    ),
    HelpSection(
        id="my-todos",
        title="My To-Dos",
        requires="todos",
        keywords=(
            "to-do",
            "to-dos",
            "todo",
            "todos",
            "my to-dos",
            "promise",
            "promised",
            "remind me",
            "reminder",
            "waiting on you",
            "kept",
            "snooze",
            "slipping",
            "add to my to-dos",
        ),
        body=(
            "My To-Dos (My Work → My To-Dos) is your own list of what you have said you would "
            "do, for whom, and by when. It is different from a team's EOS To-Dos: those are "
            "team work reviewed in an L10; this is your personal ledger, and only you can "
            "change it. It is not hidden from your manager, though: see Who else sees it below.\n"
            "Adding one: type it in plain words at the top of the page ('send Sam the revised "
            "SOW Thursday') — Cubie reads out the task, who is waiting and the date, and you "
            "confirm. In Chat, say 'remind me to…' or 'add a to-do…'. In Slack, /todo now asks "
            "Where: My list (default) or Team L10. On a Daily Briefing card, open More actions and "
            "choose 'Add to my To-Dos' to turn Cubie's suggestion into a commitment you own.\n"
            "The list is grouped by who is waiting on you, with your own items under 'Just you'. "
            "Each item has a next-step button that does the first part of the work — draft the "
            "email, open the document or deal, prep for the meeting, or block 30 minutes — plus "
            "Kept when it is done and Snooze (tomorrow, 3 days, next week, or a date). The … "
            "menu holds Change the date, Make private, Make it a team To-Do (when EOS is "
            "enabled for your workspace, this files it into your team's EOS list as well) "
            "and Drop.\n"
            "An item whose date has passed is marked slipping so it never sits quietly overdue. "
            "Open items that are overdue, due within the next 7 days, or have no date are also "
            "sent to your Daily Briefing as 'Your To-Do' cards, at most 12 at a time with the "
            "overdue ones first, and Cubie weighs them against everything else in your day. An "
            "item due later than that waits until it is a week out. Ticking the card and marking "
            "the item kept are the same action.\n"
            "Also on the page, when EOS is enabled: your open team To-Dos; account executives "
            "also see their "
            "open Sales Cockpit commitments — read from those systems, not copied, with their own "
            "links. The footer keeps this week's tally: how many you kept and dropped since "
            "Monday, and how many are open now (and how many of those are slipping).\n"
            "Cubie heard you promise…: promises Cubie hears you make in meetings, L10s, sent "
            "email and Slack land in a tray at the top of the page, each with your exact words "
            "and a link to where you said them. Accept one to add it to your list, or reject it "
            "if it wasn't really a promise (it won't be suggested again). Once its date passes, "
            "Cubie checks your sent email, Slack, Drive and EOS To-Dos; if it finds the email or "
            "file that keeps the promise, the item moves to 'Looks done, confirm?' — Cubie never "
            "marks anything kept on its own.\n"
            "Who else sees it: only you can change your list, but it is not hidden from your "
            "manager. A manager can ask Chat about the promises of the people who report to them "
            "and the teams they lead, including what is on My To-Dos and what Cubie heard them "
            "promise; executives who oversee the whole company can ask about every team. To keep "
            "one item out of that, open its … menu and choose Make private. A private item also "
            "stays out of your Meeting Prep briefs, because a brief can be saved to Assets.\n"
            "Separately, Insights → Follow-through counts how many To-Dos each person kept or "
            "dropped, for the people allowed to open Insights. It shows counts only, never what "
            "a To-Do says, and a private item is counted like any other."
        ),
    ),
    HelpSection(
        id="cubie-for-chrome",
        title="Cubie for Chrome",
        requires="chrome_extension",
        keywords=(
            "chrome",
            "cubie for chrome",
            "chrome extension",
            "extension",
            "browser",
            "new tab",
            "toolbar",
            "badge",
            "icon number",
            "address bar",
            "omnibox",
            "right-click",
            "ask cubie about",
            "hide for today",
            "one line",
            "connected browsers",
            "disconnect browser",
            "past due",
        ),
        body=(
            "Cubie for Chrome is a small Chrome extension that shows your day each time you open "
            "a new tab: first the promises you made and have not kept yet, then this morning's "
            "top priorities. Each line opens the right page in Cubie, and you can tick a promise "
            "or a priority off right there. There are no pop-ups and no sounds.\n"
            "What the new tab shows:\n"
            "- Promises past due: your To-Dos from L10 meetings when EOS is enabled for your "
            "workspace, the "
            "items in My To-Dos past their date, and the promises Cubie heard you make on calls "
            "and in email. Each says how many days late it is, and a promise in two of those "
            "places shows once.\n"
            "- Today's priorities: the top of the AI Priorities on your Daily Briefing, with the "
            "one-line headline Cubie wrote for your day. If this morning's list is not ready yet, "
            "the tab says so and links to Cubie.\n"
            "What you see follows your other access: the priorities and the promises Cubie heard "
            "need the Cubie copilot, the My To-Dos promises need My To-Dos, and EOS To-Dos "
            "need the EOS module.\n"
            "Connecting it: once your company installs the extension from the Chrome Web "
            "Store, it arrives on its own. Until then it is added by hand: ask your Cubie "
            "admin for the Cubie for Chrome package, built for your Cubie address, and unzip "
            "it. In Chrome, open chrome://extensions, turn on Developer mode, click Load "
            "unpacked and pick the folder you unzipped. Then open the extension, or go to "
            "Connect Cubie for Chrome in Cubie, and press Connect this browser. Cubie says "
            "what the extension will show and what it can't see before you agree. Connect "
            "each browser you use once.\n"
            "If a browser was connected before workspace-bound keys were introduced, "
            "open Connect Cubie for Chrome and press Connect this browser again. "
            "The earlier browser key no longer works.\n"
            "The number on the Cubie icon in Chrome's toolbar is how many of your promises are "
            "past due (9+ for more than nine). Nothing past due, no number.\n"
            "Keeping it out of your way: right-click the Cubie icon and choose Options to "
            "shrink the new tab to one line under the search box (or show your promises only), turn off "
            "the number on the icon, or blur the details until you point at them, for when you "
            "share your screen. Hide for today, at the bottom of the new tab, hides Cubie until "
            "tomorrow.\n"
            "Ask Cubie from anywhere in Chrome: type cubie, then a space, then your question in "
            "the address bar and press Enter, and Ask Cubie opens and asks it. Or select text on "
            "any page, right-click and choose Ask Cubie about…. The question is sent as a chat "
            "message, exactly as if you had typed it, and kept in your chat history like any "
            "other. These two shortcuts are only in the version loaded by hand; the version your "
            "company installs from the Chrome Web Store leaves them out for now, because Chrome "
            "Web Store rules let a new tab extension add only the new tab and its toolbar "
            "button.\n"
            "What it can't see: it only shows what Cubie sends it. It cannot read your email, "
            "your browsing history or any other website, and a selected-text question goes to "
            "Cubie only when you choose Ask Cubie. It never asks Cubie to make new priorities: "
            "the new tab reads the list Cubie already made this morning, so opening as many tabs "
            "as you like costs nothing.\n"
            "Disconnecting: every connected browser is listed in Settings → General, under Cubie "
            "for Chrome, with when it was connected and last used. Disconnect turns one off at "
            "once. A connection also lapses after 90 days (connect that browser again to keep it "
            "going), and every connection ends when someone leaves your company."
        ),
    ),
    HelpSection(
        id="company-setup",
        title="Company setup",
        keywords=(
            "company setup",
            "company profile",
            "profile",
            "catalog",
            "product catalog",
            "products",
            "services",
            "price list",
            "csv",
            "upload",
            "proposal template",
            "templates",
            "letterhead",
            "industry",
            "workspace",
        ),
        body=(
            "Cubie works for any company, so it starts out knowing nothing about yours. An "
            "admin tells it in Admin → Company setup, under Workspace. The page has three "
            "sections, each saved on its own, and all three start empty.\n"
            "- The company profile: the company's name, legal name, website and address, "
            "and a few sentences about the business: its industry, what it sells, who it "
            "sells to, how it sells, and its main products or services. Chat, call "
            "wrap-ups, CRM Sync, Customer X-Ray, Meeting Prep, Loose Ends replies and "
            "proposals all read it. Until it is filled in they simply say 'the company'; "
            "a blank field is left out, never guessed. The name, legal name, address and "
            "website also make up the letterhead of every proposal. The company's name "
            "also heads the other documents Cubie makes for you: Meeting Prep and deep "
            "prep briefs (the Word download and the emailed PDF), the L10 recap email, "
            "and the Scorecard and VTO Excel templates. Until the profile has a name or "
            "legal name, they carry Cubie's name instead.\n"
            "- The product catalog: the products and services the company sells, uploaded "
            "as a CSV whose first row is name,description,category,price,unit,url. Only the "
            "name is required: leave the price blank for anything priced per job, and use "
            "the unit for a price such as $90 per hour. Each upload replaces the whole "
            "catalog. A file with even one bad row changes nothing, and the page lists "
            "every row to fix, by the row number your spreadsheet shows. Customer X-Ray "
            "suggests cross-sell opportunities from the catalog, Meeting Prep tells sales "
            "what each matched product does, and chat can look products up; with no "
            "catalog none of them invents a product.\n"
            "- Proposal templates: a standard write-up of one service plus the catalog "
            "items that price it. When a call is about that service, the Proposal "
            "Generator suggests the template, prints its write-up word for word after the "
            "scope of work, and adds its catalog prices to the pricing table, where the "
            "account executive can change them before anything is generated. Prices never "
            "come from the AI's own wording. In the write-up, start a line with # for a "
            "heading and - for a bullet. Untick 'Offer it on new proposals' to retire a "
            "template without deleting it: new proposals stop offering it, and proposals "
            "that already use it keep printing its write-up. Without any templates, "
            "proposals are written from the call alone."
        ),
    ),
)


# ── Retrieval ────────────────────────────────────────────────────────────────
# The scorer itself lives in `_retrieval.py`.
# What stays here is only what is specific to Cubie's own Help page.

_INDEX = HelpIndex(HELP_SECTIONS)


def search_help(
    query: str,
    *,
    top_k: int = 3,
    allowed_ids: Collection[str] | None = None,
) -> list[HelpSection]:
    """Return the Help sections most likely to answer ``query``, best first.

    ``allowed_ids`` restricts the result to sections the caller may see (the
    gated features); ``None`` means no restriction. An empty list means nothing
    matched — callers should offer the section directory rather than guess.
    """
    return _INDEX.search(query, top_k=top_k, allowed_ids=allowed_ids)


def section_directory(allowed_ids: Collection[str] | None = None) -> str:
    """A one-line-per-section index, for when nothing matched the query."""
    return render_directory(
        (s for s in HELP_SECTIONS if allowed_ids is None or s.id in allowed_ids),
        help_link,
    )


def render_help_report(sections: list[HelpSection]) -> str:
    """Format sections for the agent: content plus the link to cite."""
    return render_sections(sections, help_link)
