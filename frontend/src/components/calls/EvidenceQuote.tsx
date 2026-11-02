// The verbatim quote behind one wrap-up proposal, with a [context] expander.
//
// This is the component that makes the 90-second interaction budget possible.
// The AE is certifying a claim about a call they had yesterday; the quote is
// what lets them do that in two seconds instead of re-reading the transcript.
// So it is the most readable thing on the row — not a footnote, not a tooltip,
// not truncated.
//
// THREE THINGS HERE ARE DELIBERATE:
//
// 1. There is NO audio player, and there must never be one. Recording defaults
//    to OFF, so no audio artifact exists to seek into. A play
//    button would be a control that cannot work.
// 2. The text is rendered FAITHFULLY. A garbled quote is supposed to look
//    garbled — that is how the AE knows to reject the row, and it is the design
//    working rather than failing. Nothing here trims, capitalises or "cleans".
// 3. An empty `context` array hides the expander instead of disabling it.
//    Fathom-sourced items have no per-utterance identifiers, so the citation
//    is the text alone. "No context to expand" is a normal state, not a broken
//    one.
import React, { useState } from 'react';
import type { EvidenceLine } from '../../api/calls';

export interface EvidenceQuoteProps {
    /** The cited utterance. The column is NOT NULL, so the AE always has one —
     *  but the router redacts it to NULL for a reader the transcript-text
     *  policy excludes (the supervisor, an exec on a team call).
     *  Null or empty renders nothing rather than an empty quote box; the screen
     *  says once, at the top, that it is showing metadata only. */
    text: string | null;
    /** ±2 surrounding transcript lines. Empty/absent → no expander. */
    context?: EvidenceLine[] | null;
    /** Accessible name for the expander, so a screen reader hears which row's
     *  context it is opening rather than five identical "context" buttons. */
    label?: string;
}

// Full literal class strings — the JIT purges anything it cannot see whole.
const CITED_LINE = 'bg-brand-tint text-gray-900 font-medium';
const PLAIN_LINE = 'text-gray-600';

export const EvidenceQuote: React.FC<EvidenceQuoteProps> = ({ text, context, label }) => {
    const [open, setOpen] = useState(false);
    const lines = context ?? [];
    const hasContext = lines.length > 0;
    const quote = text ?? '';

    if (!quote.trim()) return null;

    return (
        <div className="mt-1.5">
            <div className="flex items-start gap-2">
                <blockquote className="flex-1 min-w-0 border-l-2 border-brand/40 pl-3 text-[15px] leading-relaxed text-gray-900">
                    &ldquo;{quote}&rdquo;
                </blockquote>
                {hasContext && (
                    <button
                        type="button"
                        onClick={() => setOpen(o => !o)}
                        aria-expanded={open}
                        aria-label={
                            label
                                ? `${open ? 'Hide' : 'Show'} transcript context for ${label}`
                                : undefined
                        }
                        className="flex-shrink-0 text-xs font-medium text-brand hover:underline focus:outline-none focus:ring-2 focus:ring-brand rounded px-1 -mx-1"
                    >
                        {open ? '[hide]' : '[context]'}
                    </button>
                )}
            </div>

            {open && hasContext && (
                <ol className="mt-2 ml-3 rounded-md border border-gray-200 bg-gray-50 divide-y divide-gray-200 overflow-hidden">
                    {lines.map(line => (
                        <li
                            key={line.sequence}
                            className={`px-3 py-2 text-sm leading-snug ${
                                line.is_cited ? CITED_LINE : PLAIN_LINE
                            }`}
                        >
                            <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mr-2">
                                {line.speaker_name || 'Unknown speaker'}
                            </span>
                            {line.text}
                            {line.is_cited && (
                                <span className="ml-2 text-[10px] font-bold uppercase tracking-wider text-brand">
                                    cited
                                </span>
                            )}
                        </li>
                    ))}
                </ol>
            )}
        </div>
    );
};

export default EvidenceQuote;
