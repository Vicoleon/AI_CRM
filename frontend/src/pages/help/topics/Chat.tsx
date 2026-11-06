import React from 'react';
import { AskLink, Callout, SubHeading, TopicLink } from '../kit';

// Help → Chat. Served at /help/chat; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const Chat: React.FC = () => (
    <>
        <p>
            Chat is your conversational assistant. Ask in plain language and it picks the right
            source automatically. Your conversations are saved in the list on the left; start a
            new one any time.
        </p>
        <SubHeading id="workspace-ai-provider">Your workspace AI provider</SubHeading>
        <p>
            Your workspace admin can choose a compatible AI provider for Chat in Admin → Workspace.
            Chat sends your prompt and the context it needs to answer to that provider. Chat's
            Gmail tools use your main work mailbox; connected extra mailboxes join your own AI
            Priorities and scheduled summaries instead.
            Chat web research has a separate model choice because it needs Google Search grounding.
            Pictures and built-in Google research require a compatible Google model.
        </p>
        <p>
            If the selected provider cannot answer, Chat shows a provider error. Ask your workspace
            admin to check the model and credentials in Admin → Workspace, then retry.
        </p>
        <SubHeading id="starter-prompts">Starter prompts</SubHeading>
        <p>
            An empty chat offers <span className="font-medium">one-click starter prompts</span> —
            click one and Cubie asks it straight away. They are a random handful drawn from a much
            wider set, one from each part of Cubie: your day, email, calendar, meetings, CRM, the
            knowledge base, your company&apos;s products, Slack, who&apos;s out of office, the web, and Cubie
            itself — plus scheduled tasks and Smart Teams where your team has them.
        </p>
        <p>
            You get a fresh set every time you start a new conversation, or click{' '}
            <span className="font-medium">Show me other things to ask</span> to re-draw them on the
            spot, so you keep meeting things you haven&apos;t tried. Prompts for features your team
            doesn&apos;t have never appear.
        </p>
        <SubHeading id="folders">Folders</SubHeading>
        <p>
            Keep the sidebar tidy with <span className="font-medium">folders</span>: create one
            with the folder-plus icon at the top of the list, then use the folder icon on any chat
            to file it (or make a new folder on the spot). Click a folder to collapse or expand it;
            deleting a folder just moves its chats back to <span className="font-medium">Ungrouped</span> — it never deletes them.
        </p>
        <SubHeading id="what-chat-can-answer-about">What Chat can answer about</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Company documents &amp; knowledge base</span> — policies, products, procedures that your team can access; answers show the sources they came from, and it can open a document by name. If your team&apos;s knowledge collection is unavailable, Cubie says so instead of searching a broader shared collection. To look one entry up inside a long document, name the document in your question (&ldquo;what&rsquo;s this client&rsquo;s acronym in the Client Acronym List&rdquo;) and Cubie opens that file and reads the whole thing, not just the first pages. If it can only read part of a very long file, it tells you so instead of reporting the entry as missing</li>
            <li><span className="font-medium">Products</span> — offerings, pricing, fulfillment</li>
            <li><span className="font-medium">CRM / HubSpot</span> — deals, pipeline, sales activity (see the <TopicLink id="crm">CRM page</TopicLink>)</li>
            <li><span className="font-medium">Your Gmail</span> — search, summarize unread, read a thread, and <span className="font-medium">draft</span> replies or new emails in your writing style</li>
            <li><span className="font-medium">Meeting transcripts</span> — search and summarize past Google Meet / Fathom meetings; ask by name (&ldquo;summarize the Weekly Pipeline Review&rdquo;), for your most recent one, or by topic. Ask for the <span className="font-medium">full</span> or <span className="font-medium">complete</span> notes (&ldquo;give me all the notes from today&rsquo;s Sales L10&rdquo;) and it returns the whole meeting — action items and all — instead of a short summary</li>
            <li><span className="font-medium">Your calendar</span> — list events, check availability, create events</li>
            <li><span className="font-medium">Who&apos;s out of office</span> — ask &ldquo;is Jordan out this week?&rdquo; or &ldquo;when is Maya back?&rdquo; and Cubie tells you their PTO window and return date. When you schedule a meeting, it flags an attendee who&apos;s away</li>
            <li><span className="font-medium">The web</span> and your <span className="font-medium">company directory</span></li>
            <li><span className="font-medium">Slack</span> — recent messages, channel catch-ups, your @-mentions (once Slack is connected in Settings)</li>
            <li><span className="font-medium">What you&apos;re waiting on</span> — ask <AskLink q="What am I waiting on?" /> (no need to say email or Slack) and Cubie sweeps your recent Gmail threads and your Slack mentions and DMs together, follows each one to its latest reply, and comes back with what <span className="font-medium">needs your action</span> (by priority, with who and when), what you&apos;re <span className="font-medium">waiting on from others</span>, and what it <span className="font-medium">skipped</span> because the thread was already closed or someone else has it. The footer says exactly what was checked; if Slack isn&apos;t connected it says so instead of showing an empty section</li>
            <li><span className="font-medium">Cubie itself</span> — how-to questions about the app, answered from these Help pages. Ask <AskLink q="How do I add a document to the knowledge base?" /> or “where do I connect Slack?” and the answer comes back with a link to the Help page it used, so you can read the rest</li>
        </ul>
        <SubHeading id="approving-actions">Approving actions</SubHeading>
        <p>
            When Chat is about to <span className="font-medium">do</span> something rather than
            just answer (book a calendar event; save, change or delete a scheduled task) it
            stops and shows a card with exactly what will happen, taken from the action
            itself: the title and time, every person invited, the full text of a
            description, a task&apos;s instruction and schedule. Click <span className="font-medium">Yes, go ahead</span> or{' '}
            <span className="font-medium">No, cancel</span>; typing yes or no works too.
        </p>
        <p>
            A yellow warning flags what you might not expect, such as an invitation going to
            people <span className="font-medium">outside your company</span>. Cubie reads text
            other people wrote, like emails and documents, and that text can try to steer it,
            so read the card rather than Cubie&apos;s earlier message: the card is what will
            run. If anything on it is not what you asked for, say no.
        </p>
        <SubHeading id="sources-panel">Sources panel</SubHeading>
        <p>
            When an answer comes from your documents, a <span className="font-medium">Sources</span>{' '}
            panel sits underneath it — one card per document, naming the document, the folder it
            lives in, and a preview of the passage Cubie read. Open a card to see the exact wording
            it quoted; if several passages came from the same document they are grouped on one card.
            Long lists show the first few, with <span className="font-medium">Show all</span> for the rest.
        </p>
        <SubHeading id="mentions-feedback-and-the-timer">Mentions, feedback and the timer</SubHeading>
        <p>
            Type <span className="font-mono text-brand">@</span> to mention a person or{' '}
            <span className="font-mono text-brand">#</span> to reference a Slack channel. Use
            the thumbs buttons under any answer to give feedback.
        </p>
        <p>
            While Cubie is working, a timer shows how long the current answer has been running.
            Use the <span className="font-medium">sound icon</span> next to the Send button to
            toggle a chime that plays when a longer answer finishes — handy if you switch to
            another tab while it works.
        </p>
        <SubHeading id="voice-input">Voice input</SubHeading>
        <p>
            Where your team has it switched on, a <span className="font-medium">microphone</span>{' '}
            sits next to the Send button. Click it and say your question. To finish, click it again
            (it turns into a stop square while it listens) or press{' '}
            <span className="font-medium">Enter</span>:{' '}
            <span className="font-medium">Cubie writes down what you said and sends it straight
            away</span>, punctuated, with the um&apos;s and uh&apos;s left out. Anything you had
            already typed in the box goes out with it, in front of the spoken words. Press{' '}
            <span className="font-medium">Esc</span> while it listens, or while it is still writing
            it down, to throw the recording away and send nothing.
        </p>
        <p>
            While the mic is on, it is the send button: the Send button and the starter prompts
            wait until it is done. Your words stay in the box instead of being sent, with a note
            saying why, when the recording reaches its two-minute limit, when you move to another
            conversation or change what is in the box while Cubie is writing it down, or when
            Cubie is still answering your previous question. Press Send when you are ready. If
            what it heard isn&apos;t what you meant, just ask again: your question and the answer
            are both in the conversation. Shift+Enter still starts a new line, and Enter in an
            @mention list still picks the person.
        </p>
        <p>
            It spells colleagues&apos; names and your company&apos;s own terms (its name and
            products, L10, HubSpot) the way the company writes them, and it writes down whatever language you
            speak without translating it. A recording can run up to two minutes. If nobody spoke,
            Cubie says it didn&apos;t hear anything instead of guessing. The recording itself is
            never saved. The first time, your browser asks for permission to use the microphone;
            if you said no, allow it again in the browser&apos;s site settings.
        </p>
        <SubHeading id="files-and-pictures">Files and pictures</SubHeading>
        <p>
            Where your team has it switched on, a <span className="font-medium">paperclip</span> sits
            beside the microphone. Click it to attach a Word document (.docx), a PDF, an Excel or CSV
            file, a PowerPoint deck (.pptx), a text file or a picture (PNG, JPG, WEBP, GIF, HEIC). You
            can also drop files on the chat box, or paste a screenshot straight into it. Up to five
            files per message, 20 MB each.
        </p>
        <p>
            Cubie reads each file as soon as you add it: a spinner shows while it works, and the file
            then shows what it found (pages, sheets, slides). It reads the words in a picture and
            what the picture shows, so you can ask about a chart, a screenshot or a photo of a page,
            and it reads a scanned PDF page by page. Then ask your question and press Send, or send
            the file on its own and Cubie tells you what is in it.
        </p>
        <p>
            <span className="font-medium">Your files are private to you.</span> Nobody else can see
            them: not your team, not your manager, not an admin. A file marked{' '}
            <span className="font-medium">Private library</span> is kept in your own library, so you
            can ask about it again in any later conversation (&quot;what did that contract I sent
            say about renewal?&quot;). A file marked <span className="font-medium">This chat only</span>{' '}
            could not be added to your library at the time; Cubie still uses it in the conversation
            you attached it to. Attaching the same file again reuses what Cubie already read.
        </p>
        <p>
            To see, download or delete your files, open{' '}
            <TopicLink id="assets">Assets → My private files</TopicLink>. Deleting a file takes it out
            of your library, so no other conversation finds it; a conversation you attached it to keeps
            what Cubie read there until you delete that conversation. Removing a file from the chat box
            before you send also deletes it, unless you had attached it before. You can attach up to 100
            new files a day. Older Office files (.doc, .xls, .ppt) need to be saved as .docx, .xlsx or
            .pptx first.
        </p>
        <p>
            To share one with your team, open <TopicLink id="assets">Assets → My private files</TopicLink>{' '}
            and click <span className="font-medium">Publish</span> (the share icon) on the file. Pick a
            topic and a department, or company-wide, the same choices as adding a Drive file. A copy goes
            into Assets for that department (or everyone) and Cubie can answer their questions from it;
            your private file stays private, and deleting it later does not remove the copy. A Word file,
            PDF, PowerPoint deck or text file goes in as itself. A picture, a scanned PDF or a spreadsheet
            goes in as a Word document with the picture or pages and what Cubie read from it, because the
            knowledge base can&apos;t search those directly. Publishing needs the same permission as
            uploading a document to Assets.
        </p>
        <SubHeading id="meeting-skills">Meeting write-ups, templates, skills and routines</SubHeading>
        <p>
            Where your team has it switched on, ask for any meeting in the shape you want:{' '}
            <span className="italic">&quot;my 10am as a BANT note&quot;</span>,{' '}
            <span className="italic">&quot;the Acme call as pains, budget and next steps&quot;</span>,{' '}
            <span className="italic">&quot;my last meeting like a SOAP note&quot;</span>. Cubie writes the
            outline from what you asked and fills it from the meeting&apos;s transcript: the Google Meet or
            Gemini transcript in your Drive, a Call Capture call or Fathom. It only uses meetings you attended.
        </p>
        <p>
            <span className="font-medium">Every point is checked.</span> Each one ends in a number like [1];
            the <span className="font-medium">Sources</span> panel shows who said it, roughly when and their
            exact words. Google marks the time every few minutes, so a time with a ~ is approximate. Cubie
            checks every quote, owner, date and figure against the transcript and leaves out anything it
            cannot find there, and the write-up says how many points it left out. Action items are grouped by
            owner, with a due date only if someone said one; an owner nobody named goes under{' '}
            <span className="font-medium">Owner not clear</span>.
        </p>
        <ul className="list-disc list-inside space-y-1">
            <li>
                <span className="font-medium">A template</span> is just the outline. Say &quot;save that as my
                BANT template&quot;, or attach a form you already use and say &quot;use this as a template&quot;;
                Cubie keeps its headings and their order.
            </li>
            <li>
                <span className="font-medium">A skill</span> is a whole job you run again: what to look for,
                the outline, the tone, how much detail, one meeting or every meeting from the last few days,
                and whether to also leave a Gmail draft for you or for the other attendees (Cubie never sends
                it). Say &quot;save that as a skill&quot;, then type <span className="font-mono">/</span> in the
                chat box to pick it: <span className="font-mono">/client-debrief the Acme call</span>.
            </li>
            <li>
                <span className="font-medium">A routine</span> runs a skill on its own. &quot;After every client
                call, run /client-debrief&quot; fires when the transcript is ready, for meetings from then on.
                &quot;Every Monday at 9, run /weekly-digest&quot; is a{' '}
                <TopicLink id="scheduled-tasks">scheduled task</TopicLink>. Results arrive as conversations in
                your <span className="font-medium">Scheduled tasks</span> chat folder and as a Slack DM.
            </li>
        </ul>
        <p>
            Saving, changing or deleting a template, skill or routine asks for your OK first, and saving
            under a name you already have makes a new version. Ask &quot;what skills do I have?&quot; to list
            them, or &quot;pause my debrief routine&quot;. They are private to you. A routine that fails several
            times in a row pauses itself.
        </p>
        <SubHeading id="stalled-or-failed-answers">Stalled or failed answers</SubHeading>
        <p>
            If Google&apos;s AI service stalls while Cubie is answering, Cubie switches to a second
            AI model once by itself, so a brief outage usually costs a few extra seconds instead of an
            error.
        </p>
        <p>
            If an answer still fails, the message says why.{' '}
            <span className="font-medium">Trouble reaching the AI service</span> means Google&apos;s
            AI was briefly unavailable: ask the same question again in a minute, there is no need to
            simplify it. <span className="font-medium">Took too long</span> means the question itself
            needed more work than one answer allows, so splitting it up helps.
        </p>
        <Callout tone="info">
            Email is <span className="font-medium">draft-only</span> — Cubie never sends on your
            behalf. Anything it writes lands in your Gmail <span className="font-medium">Drafts</span>{' '}
            for you to review and send.
        </Callout>
    </>
);
