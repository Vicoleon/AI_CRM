// My Day → "Daily rhythm": the sales checklist's three routines, one tab each.
//
// Sits BELOW the AE's five blocks, never in place of them — My Day's order is
// load-bearing and this section is additive. It opens on the routine for the
// reader's clock (before 11:00 morning, before 15:00
// mid-day, then end of day) and loads a tab only when it is shown.
//
// The cockpit's worklist rules hold here too: at most three rows a block, the
// rest as a "+N more" LINK (to the Ask Cubie question that lists them all —
// never an inline expander), and exactly one primary action per row. Every
// count is shown against its denominator ("2 of 14 open deals").
//
// The checklist items that are not HubSpot checks (inbox triage, promises,
// meeting prep, the end-of-day recap) appear as one-click Ask Cubie links, so
// the whole routine is reachable from this one place.
//
// A deal row carries "Defer" for people on the `deal_defer` roster. A deferred
// deal is left out of every check by the server until its follow-up date, and
// the block's note says how many were left out.
import React, { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ChevronRight, MessageSquare, Sparkles } from 'lucide-react';
import { cockpitError, isForbidden, isNotEnabled } from '../../api/cockpit';
import type { CockpitAction, CockpitError } from '../../api/cockpit';
import {
    askCubieHref,
    fetchRhythm,
    isRhythmActionKind,
    rhythmUnavailableCopy,
} from '../../api/salesRhythm';
import type { RhythmBlock, RhythmResponse } from '../../api/salesRhythm';
import type { DeferTarget } from '../../api/dealDefer';
import { ActionRow, DeferDealModal } from '../../components/sales';
import type { ActionRowAction } from '../../components/sales';
import { useAuth } from '../../context/AuthContext';
import { PrimaryButton } from '../../components/smartTeams/shared';
import { ErrorState, LoadingState } from '../eos/_shared';
import {
    RHYTHM_PARTS,
    RHYTHM_PART_GOALS,
    RHYTHM_PART_LABELS,
    SALES_RHYTHM_PROMPTS,
    rhythmPartFor,
} from '../chatSuggestions';
import type { RhythmPart } from '../chatSuggestions';

const LIST_CARD = 'bg-white rounded-lg border border-gray-200 shadow-sm divide-y divide-gray-100';
const NOTE_CARD = 'rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900';

// Full literal class strings — the Tailwind JIT purges what it cannot see whole.
const TAB: Record<'on' | 'off', string> = {
    on: 'bg-brand text-white',
    off: 'text-gray-600 hover:bg-gray-50',
};

export const DailyRhythm: React.FC<{
    /** After a deal was deferred from one of these rows, so My Day can reload
     *  its blocks and its Deferred deals list. */
    onDeferred?: () => void;
    /** Bumped by My Day after a defer anywhere on the page, so the open tab is
     *  read again without that deal. */
    reloadKey?: number;
}> = ({ onDeferred, reloadKey: outerKey = 0 }) => {
    const navigate = useNavigate();
    const { hasFeature } = useAuth();
    const canDefer = hasFeature('deal_defer');
    const [part, setPart] = useState<RhythmPart>(() => rhythmPartFor(new Date()));
    const [data, setData] = useState<Partial<Record<RhythmPart, RhythmResponse>>>({});
    const [errors, setErrors] = useState<Partial<Record<RhythmPart, CockpitError>>>({});
    const [deferTarget, setDeferTarget] = useState<DeferTarget | null>(null);
    // Bumped after a defer so the open tab is read again without the deal.
    const [reloadKey, setReloadKey] = useState(0);

    // Every setState sits inside the async callback (`react-hooks/set-state-in-
    // effect` is an error in this repo), and the spinner is derived below. A
    // tab is re-read each time it is opened; the server caches each check for
    // five minutes, so switching back and forth costs no HubSpot calls.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const fresh = await fetchRhythm(part);
                if (cancelled) return;
                setData(prev => ({ ...prev, [part]: fresh }));
                setErrors(prev => ({ ...prev, [part]: undefined }));
            } catch (err) {
                if (!cancelled) setErrors(prev => ({ ...prev, [part]: cockpitError(err) }));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [part, reloadKey, outerKey]);

    const current = data[part];
    const error = errors[part];

    // Switched off, or not on the roster: the section is simply not there. My
    // Day above it is unaffected, and an error card for a feature the reader
    // was never given would be noise.
    if (error && (isNotEnabled(error) || isForbidden(error))) return null;

    const toRowAction = (action: CockpitAction): ActionRowAction => {
        const label = action.label;
        const href = action.href ?? null;
        if (!isRhythmActionKind(action.kind) || !href) {
            return {
                label,
                onClick: async () => {},
                disabledReason: href
                    ? `Cubie does not know how to "${label}" yet.`
                    : 'This link needs the HubSpot portal id, which is not set here. Open the record in HubSpot directly.',
            };
        }
        switch (action.kind) {
            // A HubSpot record: booking the step or filling the field happens there.
            case 'open_hubspot':
                return {
                    label,
                    onClick: async () => {
                        window.open(href, '_blank', 'noopener,noreferrer');
                    },
                };
            // Ask Cubie, in-app, with the draft request ready to run.
            case 'ask_cubie':
                return { label, onClick: async () => navigate(href) };
            default:
                return { label, onClick: async () => {}, disabledReason: `Unknown action "${label}".` };
        }
    };

    const renderBlock = (block: RhythmBlock): React.ReactNode => {
        const unavailable = rhythmUnavailableCopy(block.unavailable_reason);
        const qualifying = block.items.length + block.suppressed_count;
        return (
            <section key={block.key} className="mb-7">
                <div className="mb-2">
                    <h3 className="text-base font-semibold text-gray-900">
                        {block.label}
                        {!unavailable && (
                            <span className="ml-2 text-sm font-normal text-gray-500">
                                {qualifying} of {block.scanned} {block.scanned_label}
                            </span>
                        )}
                    </h3>
                    <p className="mt-0.5 text-xs text-gray-500">{block.hint}</p>
                </div>

                {unavailable && (
                    <p className={`mb-3 ${NOTE_CARD}`} role="status">
                        {unavailable}
                    </p>
                )}

                {block.items.length === 0 ? (
                    unavailable ? null : (
                        <p className="px-1 text-sm text-gray-500">
                            {block.scanned === 0
                                ? `You have no ${block.scanned_label} right now.`
                                : block.empty_line}
                        </p>
                    )
                ) : (
                    <div className={LIST_CARD}>
                        {block.items.map(item => {
                            const target = item.defer;
                            return (
                                <ActionRow
                                    key={item.id}
                                    title={item.title}
                                    subtitle={item.subtitle}
                                    primaryAction={toRowAction(item.primary_action)}
                                    secondaryAction={
                                        item.secondary_action ? toRowAction(item.secondary_action) : undefined
                                    }
                                    tertiaryAction={
                                        canDefer && target?.deal_id
                                            ? { label: 'Defer', onClick: async () => setDeferTarget(target) }
                                            : undefined
                                    }
                                />
                            );
                        })}
                    </div>
                )}

                {block.suppressed_count > 0 && (
                    <div className="mt-2 px-1">
                        <Link
                            to={askCubieHref(block.more_prompt || block.ask_prompt)}
                            className="inline-flex items-center gap-1 text-sm font-medium text-brand hover:underline"
                        >
                            +{block.suppressed_count} more — see them all in Ask Cubie
                            <ChevronRight size={15} aria-hidden="true" />
                        </Link>
                    </div>
                )}
                {block.note && <p className="mt-2 px-1 text-xs text-gray-500">{block.note}</p>}
            </section>
        );
    };

    // The checklist prompts this tab does not already show as a block — inbox
    // triage, promises and meeting prep in the morning — as Ask Cubie links.
    const shownAsBlocks = new Set([
        ...(current?.blocks ?? []).map(b => b.ask_prompt),
        current?.recap_prompt ?? '',
    ]);
    const alsoAsk = current
        ? SALES_RHYTHM_PROMPTS.rep[part].filter(p => !shownAsBlocks.has(p))
        : [];

    let body: React.ReactNode;
    if (!current && !error) {
        body = <LoadingState label={`Checking your ${RHYTHM_PART_LABELS[part].toLowerCase()} list…`} />;
    } else if (!current && error) {
        body = <ErrorState message={error.message} />;
    } else if (current) {
        body = (
            <>
                {current.crm_available === false && (
                    <p className={`mb-5 ${NOTE_CARD}`} role="status">
                        HubSpot could not be reached just now, so these checks are unavailable
                        rather than empty. Your worklist above is unaffected.
                    </p>
                )}
                {alsoAsk.length > 0 && (
                    <div className="mb-6">
                        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                            Also part of your {RHYTHM_PART_LABELS[part].toLowerCase()}
                        </p>
                        <div className="flex flex-wrap gap-2">
                            {alsoAsk.map(prompt => (
                                <Link
                                    key={prompt}
                                    to={askCubieHref(prompt)}
                                    className="inline-flex max-w-full items-start gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2 text-left text-sm text-gray-700 hover:border-brand"
                                >
                                    <MessageSquare size={15} className="mt-0.5 flex-shrink-0 text-brand" aria-hidden="true" />
                                    <span>{prompt}</span>
                                </Link>
                            ))}
                        </div>
                    </div>
                )}
                {current.blocks.map(renderBlock)}
                {current.recap_prompt && (
                    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
                        <div className="flex items-start gap-3">
                            <span className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-brand/10 text-brand">
                                <Sparkles size={16} aria-hidden="true" />
                            </span>
                            <div className="min-w-0 flex-1">
                                <h3 className="text-base font-semibold text-gray-900">Send the day’s recap</h3>
                                <p className="mt-0.5 text-sm text-gray-600">
                                    Cubie gathers today&apos;s calls, emails, meetings and stage changes —
                                    what you logged in HubSpot, plus the emails you sent and the meetings on
                                    your calendar — and drafts a bulleted update for your manager. You edit
                                    it and send it; nothing goes out on its own.
                                </p>
                                <div className="mt-3">
                                    <PrimaryButton onClick={() => navigate(askCubieHref(current.recap_prompt ?? ''))}>
                                        Draft my end-of-day update
                                    </PrimaryButton>
                                </div>
                            </div>
                        </div>
                    </div>
                )}
            </>
        );
    }

    return (
        <section className="mt-10 border-t border-gray-200 pt-8" aria-labelledby="daily-rhythm-heading">
            <div className="mb-5 flex flex-wrap items-end justify-between gap-3" data-tour="my-day-daily-rhythm">
                <div>
                    <h2 id="daily-rhythm-heading" className="text-lg font-semibold text-gray-900">
                        Daily rhythm
                    </h2>
                    <p className="mt-0.5 text-xs text-gray-500">{RHYTHM_PART_GOALS[part]}</p>
                </div>
                <div
                    role="tablist"
                    aria-label="Part of the day"
                    className="inline-flex rounded-lg border border-gray-200 bg-white p-0.5"
                >
                    {RHYTHM_PARTS.map(p => (
                        <button
                            key={p}
                            type="button"
                            role="tab"
                            aria-selected={p === part}
                            onClick={() => setPart(p)}
                            className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${p === part ? TAB.on : TAB.off}`}
                        >
                            {RHYTHM_PART_LABELS[p]}
                        </button>
                    ))}
                </div>
            </div>
            {body}
            <DeferDealModal
                key={deferTarget?.deal_id ?? 'closed'}
                target={deferTarget}
                onClose={() => setDeferTarget(null)}
                onDeferred={() => {
                    setReloadKey(k => k + 1);
                    onDeferred?.();
                }}
            />
        </section>
    );
};
