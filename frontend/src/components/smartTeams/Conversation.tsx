// The conversational segments — the strengths stories, and the motivators
// follow-up.
//
// Rendered as a conversation, not a grid of textareas, and that is not a
// cosmetic choice. The strengths layer scores from a TRANSCRIPT of attributed
// turns (`strengths.Turn`), because the one thing a person can argue with in
// their own guide is a quote, and a quote has to be a sentence they actually
// said. A form of labelled boxes invites summaries; a conversation invites
// stories, and the extraction rubric is built to read stories. It also makes the
// speaker attribution visible: what Cubie asked is on the left, what you said is
// on the right, and only the right-hand side is ever quotable.
//
// THE RUNTIME OWNS THE SCRIPT
// ---------------------------
// One turn at a time, through `POST .../turn`, and the SERVER decides whether
// the segment is finished — whether a follow-up gets offered depends on a
// planner call taken inside that request, so it is the one thing this component
// cannot know. It renders `conversation.finished` / `awaiting_answer` and never
// guesses. `pending_question` is the last Cubie turn, so it is already in the
// transcript and is not drawn twice.
//
// A turn is NOT idempotent — a blind retry is a second answer to the next
// question. So a failed send keeps the text in the box and the parent re-reads
// the section rather than re-posting.
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { AlertTriangle, ArrowRight, Loader2, Send, Sparkles } from 'lucide-react';

import type { ConversationState, SectionMeta } from '../../api/smartTeams';
import { PrimaryButton } from './shared';

interface ConversationProps {
    section: SectionMeta;
    conversation: ConversationState;
    /** Sends one turn. Resolves true when it landed — the draft is cleared only
     *  then, so a failed send never eats what somebody typed. */
    onSend: (text: string) => Promise<boolean>;
    sending: boolean;
    /** A send that failed, in the server's words. Rendered next to the box with
     *  the text still in it. */
    error: string | null;
    /** Move on once the runtime says the segment is done. The section is already
     *  recorded as complete server-side by then; this is navigation. */
    onContinue: () => void;
    continuing: boolean;
}

export const Conversation: React.FC<ConversationProps> = ({
    section,
    conversation,
    onSend,
    sending,
    error,
    onContinue,
    continuing,
}) => {
    const [draft, setDraft] = useState('');
    const boxRef = useRef<HTMLTextAreaElement>(null);
    const endRef = useRef<HTMLDivElement>(null);

    // Keep the newest turn in view as the conversation grows. useLayoutEffect so
    // it happens before paint and the view does not visibly jump.
    useLayoutEffect(() => {
        endRef.current?.scrollIntoView({ block: 'nearest' });
    }, [conversation.transcript.length]);

    // Focus the box when Cubie asks something new, so the whole segment can be
    // typed without touching the mouse.
    useEffect(() => {
        if (!sending && conversation.awaiting_answer) boxRef.current?.focus();
    }, [conversation.pending_question, conversation.awaiting_answer, sending]);

    const submit = async (text: string) => {
        if (sending) return;
        const ok = await onSend(text);
        if (ok) setDraft('');
    };

    const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
        // Enter sends, Shift+Enter is a new line — the convention this app's Chat
        // page already uses, so the muscle memory transfers.
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            void submit(draft.trim());
        }
    };

    return (
        <div>
            <div className="mb-4 rounded-lg border border-gray-200 bg-white p-4">
                <p className="text-sm leading-relaxed text-gray-600">{section.blurb}</p>
                <p className="mt-2 text-xs text-gray-500">
                    {conversation.finished
                        ? `All ${conversation.questions_total} questions answered.`
                        : `Question ${Math.min(conversation.question_index + 1, conversation.questions_total)} of ${conversation.questions_total}.`}{' '}
                    Specific beats short — a couple of sentences with a real example is worth
                    more here than a paragraph of summary.
                </p>
            </div>

            {/* The transcript. `aria-live` on the log so a new question from Cubie
                is announced rather than silently appearing above the box. */}
            <div
                className="space-y-3"
                role="log"
                aria-live="polite"
                aria-label={`${section.title} conversation`}
            >
                {conversation.transcript.map((turn, index) => (
                    <div
                        key={`${index}-${turn.speaker}`}
                        className={`flex ${turn.speaker === 'interviewee' ? 'justify-end' : 'justify-start'}`}
                    >
                        <div
                            className={`max-w-[85%] rounded-lg px-4 py-3 text-sm leading-relaxed ${
                                turn.speaker === 'interviewee'
                                    ? 'bg-brand text-white'
                                    : 'border border-gray-200 bg-white text-gray-800'
                            }`}
                        >
                            <p
                                className={`mb-1 text-[11px] font-medium uppercase tracking-wide ${
                                    turn.speaker === 'interviewee'
                                        ? 'text-white/70'
                                        : 'text-brand'
                                }`}
                            >
                                {turn.speaker === 'interviewee' ? 'You' : 'Cubie'}
                            </p>
                            <p className="whitespace-pre-wrap">{turn.text}</p>
                        </div>
                    </div>
                ))}
                {sending && (
                    <div className="flex justify-start">
                        <div className="inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-4 py-3 text-sm text-gray-500">
                            <Loader2 className="animate-spin" size={14} />
                            Cubie is reading that…
                        </div>
                    </div>
                )}
                <div ref={endRef} />
            </div>

            {error && (
                <div
                    role="alert"
                    className="mt-4 flex items-start gap-2.5 rounded-lg border border-red-200 bg-red-50 p-3"
                >
                    <AlertTriangle className="mt-0.5 flex-shrink-0 text-red-500" size={16} />
                    <p className="text-xs leading-relaxed text-red-800">
                        {error} Your answer is still in the box below — press Send to try again.
                    </p>
                </div>
            )}

            {conversation.finished ? (
                <div className="mt-5 flex flex-col gap-3 rounded-lg border border-green-200 bg-green-50 p-4 sm:flex-row sm:items-center sm:justify-between">
                    <p className="flex items-start gap-2 text-sm leading-relaxed text-green-900">
                        <Sparkles className="mt-0.5 flex-shrink-0" size={16} />
                        {/* The sign-off is the segment's own line when it has one.
                            Nothing here judges the answers: whether there is
                            enough to score is a diagnostic for support, not a
                            verdict to hand the person who just answered. */}
                        {conversation.closing_line ??
                            'That is this conversation done — your answers are saved.'}
                    </p>
                    <PrimaryButton onClick={onContinue} busy={continuing}>
                        Continue
                        <ArrowRight size={15} />
                    </PrimaryButton>
                </div>
            ) : (
                <div className="mt-4">
                    <label htmlFor="st-conversation-input" className="sr-only">
                        Your answer
                    </label>
                    <div className="rounded-lg border border-gray-300 bg-white p-2 focus-within:border-brand focus-within:ring-1 focus-within:ring-brand">
                        <textarea
                            id="st-conversation-input"
                            ref={boxRef}
                            value={draft}
                            onChange={event => setDraft(event.target.value)}
                            onKeyDown={onKeyDown}
                            rows={3}
                            placeholder="In your own words…"
                            className="block w-full resize-y border-0 px-2 py-1.5 text-sm outline-none placeholder:text-gray-400"
                        />
                        <div className="flex flex-wrap items-center justify-between gap-2 px-2 pb-1 pt-1">
                            <span className="text-[11px] text-gray-400">
                                Enter to send · Shift+Enter for a new line
                            </span>
                            <div className="flex items-center gap-2">
                                {/* An empty answer is a first-class one: the
                                    server records no interviewee turn for a
                                    question somebody had nothing to say to,
                                    rather than writing down a sentence nobody
                                    said. So "nothing to say" is a button, not a
                                    dead end. */}
                                <button
                                    type="button"
                                    onClick={() => void submit('')}
                                    disabled={sending}
                                    className="rounded-md px-2 py-1.5 text-xs font-medium text-gray-500 underline-offset-2 hover:text-gray-800 hover:underline disabled:opacity-50"
                                >
                                    Nothing to say to this one
                                </button>
                                <PrimaryButton
                                    onClick={() => void submit(draft.trim())}
                                    busy={sending}
                                    disabled={!draft.trim()}
                                >
                                    <Send size={14} />
                                    Send
                                </PrimaryButton>
                            </div>
                        </div>
                    </div>
                    <p className="mt-2 text-xs text-gray-500">
                        Answers are saved as you send them. You can leave and come back to this
                        conversation at any point.
                    </p>
                </div>
            )}
        </div>
    );
};
