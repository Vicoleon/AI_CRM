import React from 'react';
import { Callout, SubHeading } from '../kit';

// Help → Assets & uploading. Served at /help/assets; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const Assets: React.FC = () => (
    <>
        <p>
            <span className="font-medium">Assets</span> is your document and file library — the same
            knowledge base Chat draws on when it answers questions. It has these tabs:
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Documents</span> — uploaded knowledge documents (not meeting transcripts): a searchable, sortable list; filter by name, owner, topic, tag or description</li>
            <li><span className="font-medium">Transcripts</span> — meetings you hosted or attended, with status and participant counts. Meeting transcripts live here, not under Documents.</li>
            <li><span className="font-medium">My private files</span> — files you attached in Ask Cubie, where your team has that switched on. Only you can see them (not your team, your manager or an admin), and they never join the shared knowledge base. Each shows what Cubie read, whether it is in your private library or for one chat only, and lets you download or delete it, or publish a copy to Assets for your department or the whole company (the share icon; it needs the same permission as uploading a document). The private file stays private, and the copy then behaves like any other document here.</li>
        </ul>
        <p>
            Documents load a page at a time. Use <span className="font-medium">Load More Assets</span>{' '}
            to continue through the library. When access to some saved Customer X-Ray briefs
            needs checking, the count may say more files are available until the next page is
            checked; it never includes a brief Cubie cannot confirm belongs to your workspace.
        </p>
        <SubHeading id="generated-briefs-and-sharing">Generated briefs and sharing</SubHeading>
        <p>
            Documents Cubie generated for you (Meeting Prep and Customer X-Ray briefs saved via
            <span className="font-medium"> Save to Assets</span>) appear under Documents with a
            <span className="font-medium"> cubie-generated</span> tag. Tick the checkbox next to any
            row (or several) and an action bar appears up top:
            <span className="font-medium"> download</span>, <span className="font-medium">share</span>{' '}
            with a teammate (they see it on their Assets page with a “Shared with you” badge,
            plus a notification), or <span className="font-medium">draft a Gmail</span> with the file
            attached — the draft opens in your own Gmail for you to review and send. These generated
            briefs are kept out of Chat's knowledge base on purpose, so Cubie never cites its own
            write-ups as evidence.
        </p>
        <p>
            A saved Customer X-Ray brief appears only while Cubie can confirm that the
            customer&apos;s HubSpot deals belong to your workspace. Older briefs without a
            confirmed customer link may be unavailable until a new brief is saved.
            Saved Meeting Prep briefs follow the same rule for the client records used to
            build them; a brief becomes unavailable if that access changes.
        </p>
        <SubHeading id="adding-a-document">Adding a document</SubHeading>
        <p>
            To add a file, use <span className="font-medium">Upload a document</span>. Pick a PDF,
            Word, text or Markdown file, set its <span className="font-medium">Topic</span> and{' '}
            <span className="font-medium">Department</span> (both required), and optionally add a
            description and tags. Tick <span className="font-medium">Company-Wide</span> if everyone
            should be able to find it — otherwise the Department controls who can.
        </p>
        <p>
            You can also bring in files straight from Google Drive without downloading and
            re-uploading: on <span className="font-medium">Productivity → Documents</span>, use the{' '}
            <span className="font-medium">+ Knowledge</span> button on any file. Either way the same
            rule applies — <span className="font-medium">Company-Wide</span> makes it findable by
            everyone; otherwise the <span className="font-medium">Department</span> you choose
            controls who can.
        </p>
        <SubHeading id="updating-a-document-you-already-uploaded">Updating a document you already uploaded</SubHeading>
        <p>
            When a policy, price sheet or deck gets a new edition, don&apos;t upload it as a second document —
            tick the row and click <span className="font-medium">Versions</span> in the action bar, then
            upload the new file as a <span className="font-medium">new version</span>. The document keeps
            its place in the list, its shares and everyone&apos;s bookmarks; only the file changes, and Chat
            switches to the new edition as soon as it&apos;s indexed (it keeps answering from the previous
            one until then). Every earlier version stays in the history: <span className="font-medium">download</span>{' '}
            any of them, or <span className="font-medium">Restore</span> one to make it current again — a
            restore is recorded as a new version, so nothing is ever overwritten.
        </p>
        <p>
            For each new version Cubie reads the old and new files and writes a short{' '}
            <span className="font-medium">“What changed”</span>{' '}
            note — sections added or removed, numbers, dates and names that moved — so the history answers
            &ldquo;what&apos;s different in this edition?&rdquo; without opening both files. You can add your own
            one-line note too. Documents with more than one version show a small <span className="font-medium">v2</span>,{' '}
            <span className="font-medium">v3</span>… badge next to their name; click it to open the history.
        </p>
        <p>
            The document&apos;s owner, an admin, or an editor for its department can add and restore versions;
            anyone who can read the document can see and download its history. Archived documents,
            meeting transcripts and Cubie-generated briefs don&apos;t take versions.
        </p>
        <Callout tone="ok">
            Meeting transcripts ingest <span className="font-medium">automatically</span> (about every
            15 minutes) — you don&apos;t upload them yourself.
        </Callout>
        <Callout tone="info">
            Two places share the name <span className="font-medium">“Documents.”</span> The{' '}
            <span className="font-medium">Productivity → Documents</span> tab is a live, private view
            of your own Google Drive; the <span className="font-medium">Assets → Documents</span> tab
            is the shared knowledge base Chat answers from. A file only moves from the first to the
            second when you add it with <span className="font-medium">+ Knowledge</span>.
        </Callout>
    </>
);
