import React, { useEffect, useId, useRef, useState } from 'react';
import { ArrowRightLeft, CalendarDays, Quote } from 'lucide-react';
import { PrimaryButton, SecondaryButton } from '../smartTeams/shared';
import { inputCls } from '../eos/Modal';
import { toast } from '../eos/Toast';

// The one-click forecast repair (My Day's pipeline-hygiene block):
//
//   Initech — stage says SOW Sent, evidence says Legal Review
//   "…we're just waiting on their counsel to clear the redlines."  Call · 12 Aug
//   [ Advance to Legal Review ]  [ Not right ]
//
// "Not right" IS A LABELLED SIGNAL, NOT A DISMISS. It is worded that way and it
// is not styled as an ✕, because the rejection rate per signal type is the only
// honest measure of whether the detector helps — and a detector that cries wolf
// teaches the AE to dismiss without reading. So rejecting asks for a reason
// (optional; asking is what makes it tuning data instead of a shrug) and the
// reason field opens INLINE on the card, which is deliberate and is NOT the
// inline expansion Rule 2 forbids — that rule is about the "+N more" overflow
// cap on a list, not about a form on a single card.

export interface StageLagApplyAction {
    /** Server-rendered — "Advance to Legal Review". Never assembled here: the
     *  stage name has to be HubSpot's, not our guess at it. */
    label: string;
    onClick: () => Promise<void>;
}

export interface StageLagRejectAction {
    /** Server-rendered, expected to read "Not right". */
    label: string;
    onClick: (reason: string) => Promise<void>;
}

export interface StageLagCardProps {
    /** The company / deal the exception is about. */
    title: string;
    /** "stage says SOW Sent, evidence says Legal Review" — the disagreement,
     *  as the server phrased it. */
    stageSummary?: string | null;
    /** The quote the detector fired on (`CockpitItem.evidence_text`). */
    quote?: string | null;
    /** When the quote was said, as the server rendered it. Printed verbatim —
     *  no client-side date arithmetic anywhere in this feature. */
    callDate?: string | null;
    applyAction: StageLagApplyAction;
    rejectAction: StageLagRejectAction;
    /** "Defer" — sets the deal aside until a follow-up date. A quiet text
     *  button, like `ActionRow`'s tertiary slot; the parent opens the dialog. */
    deferAction?: { label: string; onClick: () => void };
}

const CARD = 'bg-white rounded-lg border border-gray-200 p-6 shadow-sm';

type Pending = 'apply' | 'reject' | null;

export const StageLagCard: React.FC<StageLagCardProps> = ({
    title,
    stageSummary,
    quote,
    callDate,
    applyAction,
    rejectAction,
    deferAction,
}) => {
    const [pending, setPending] = useState<Pending>(null);
    const [rejecting, setRejecting] = useState(false);
    const [reason, setReason] = useState('');
    const reasonId = useId();
    const alive = useRef(true);
    useEffect(
        () => () => {
            alive.current = false;
        },
        [],
    );

    // Both writes go through here so a failure always re-enables the buttons and
    // always says something. A card left looking applied when the HubSpot write
    // failed is worse than no card.
    const run = (which: Exclude<Pending, null>, fn: () => Promise<void>) => {
        if (pending) return;
        setPending(which);
        void (async () => {
            try {
                await fn();
            } catch (err) {
                toast.error(err instanceof Error ? err.message : 'That action could not be completed.');
            } finally {
                if (alive.current) setPending(null);
            }
        })();
    };

    return (
        <div className={CARD}>
            <div className="flex items-start gap-3">
                <div className="mt-0.5 flex-shrink-0 rounded-md bg-brand-tint p-2 text-brand">
                    <ArrowRightLeft size={16} aria-hidden="true" />
                </div>
                <div className="min-w-0 flex-1">
                    <p className="text-base font-semibold text-gray-900">{title}</p>
                    {stageSummary && <p className="mt-0.5 text-sm text-gray-600">{stageSummary}</p>}

                    {quote && (
                        <p className="mt-3 flex items-start gap-1.5 text-sm italic leading-relaxed text-gray-600">
                            <Quote size={15} className="mt-0.5 flex-shrink-0 text-gray-400" aria-hidden="true" />
                            <span>{quote}</span>
                        </p>
                    )}
                    {callDate && (
                        <p className="mt-1.5 flex items-center gap-1.5 text-xs text-gray-500">
                            <CalendarDays size={15} aria-hidden="true" />
                            {callDate}
                        </p>
                    )}
                </div>
            </div>

            {!rejecting && (
                <div className="mt-4 flex flex-wrap items-center gap-2">
                    <PrimaryButton
                        onClick={() => run('apply', applyAction.onClick)}
                        busy={pending === 'apply'}
                        disabled={pending !== null}
                    >
                        {applyAction.label}
                    </PrimaryButton>
                    <SecondaryButton onClick={() => setRejecting(true)} disabled={pending !== null}>
                        {rejectAction.label}
                    </SecondaryButton>
                    {deferAction && (
                        <button
                            type="button"
                            onClick={deferAction.onClick}
                            disabled={pending !== null}
                            className="px-2 py-1 text-sm font-medium text-gray-500 underline-offset-2 hover:text-gray-800 hover:underline disabled:opacity-50"
                        >
                            {deferAction.label}
                        </button>
                    )}
                </div>
            )}

            {rejecting && (
                <div className="mt-4 rounded-md border border-gray-200 bg-gray-50 p-4">
                    <label className="block text-sm font-medium text-gray-700" htmlFor={reasonId}>
                        What did we get wrong?
                    </label>
                    <p className="mt-0.5 text-xs text-gray-500">
                        Optional, and worth a line: this is the only thing that tunes the detector.
                    </p>
                    <textarea
                        id={reasonId}
                        className={`${inputCls} mt-2`}
                        rows={2}
                        value={reason}
                        onChange={e => setReason(e.target.value)}
                        placeholder="The stage is right — the quote was about a different deal."
                    />
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                        <PrimaryButton
                            onClick={() => run('reject', () => rejectAction.onClick(reason.trim()))}
                            busy={pending === 'reject'}
                            disabled={pending !== null}
                        >
                            Send signal
                        </PrimaryButton>
                        <SecondaryButton
                            onClick={() => {
                                setRejecting(false);
                                setReason('');
                            }}
                            disabled={pending !== null}
                        >
                            Cancel
                        </SecondaryButton>
                    </div>
                </div>
            )}
        </div>
    );
};
