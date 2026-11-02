import React, { useEffect, useRef, useState } from 'react';
import { Quote } from 'lucide-react';
import { PrimaryButton, SecondaryButton } from '../smartTeams/shared';
import { Tooltip } from '../Tooltip';
import { toast } from '../eos/Toast';
import { AgeChip } from './AgeChip';
import type { AgeChipProps } from './AgeChip';

// One worklist row. This component is where anti-tsunami Rule 3 stops being a
// good intention and becomes a type error:
//
//   "every item in a WORKLIST has exactly one primary action, and that action
//    writes something" — Rule 3 (the rules are listed in
//    `rubik_core/cockpit/digest.py`)
//
// `primaryAction` is REQUIRED and singular. A row that genuinely has nothing to
// do does not belong in a worklist, so there is no way to render one here.
//
// USED BY `pages/sales/MyDay.tsx` AND `pages/sales/SupervisorWorklist.tsx`
// ONLY. `ManagerView.tsx` and `ExecSummary.tsx` are aggregate reporting and are
// EXPLICITLY EXEMPT from Rule 3 — a roll-up number has no single
// action and must not pretend to. They use `KpiStrip`/`KpiCard` from
// `pages/eos/_shared.tsx` instead. Without that exemption written down, neither
// aggregate view can be built at all.
//
// The primary action is async because it WRITES. So: the button shows a pending
// state while it runs, failure is surfaced through `toast.error` and the button
// is re-enabled. A row must never sit there looking applied when the write
// failed — that is how a person stops believing the screen.

export interface ActionRowAction {
    label: string;
    onClick: () => Promise<void>;
    /** Renders the button disabled with this sentence as its tooltip. For an
     *  action kind a newer backend introduced that this build cannot perform:
     *  a row that only explains itself beats a row that throws. */
    disabledReason?: string | null;
}

export interface ActionRowProps {
    title: string;
    subtitle?: string | null;
    /** Omit entirely for a row with no clock — see the AgeChip header. */
    age?: AgeChipProps;
    /** The quote behind the row. AE and manager only. The SUPERVISOR worklist
     *  passes nothing here, ever — and the router is what withholds it, so
     *  this staying unset is belt and braces. */
    evidence?: string | null;
    /** Row-level metadata that is not the title — an `OwnerChip`, a kind badge. */
    meta?: React.ReactNode;
    /** A quiet line under the row: "Nudged 10:04 AM · task created, Slack skipped". */
    footnote?: React.ReactNode;
    /** Required, singular. Rule 3. */
    primaryAction: ActionRowAction;
    secondaryAction?: ActionRowAction;
    /** A quiet text button after the other two — today only "Defer" on a deal
     *  row. Not a competing primary: it sets the row aside, it does not do the
     *  row's work, so it is styled as a link and never as a button. */
    tertiaryAction?: ActionRowAction;
}

type Pending = 'primary' | 'secondary' | 'tertiary' | null;

const ActionButton: React.FC<{
    action: ActionRowAction;
    variant: 'primary' | 'secondary';
    busy: boolean;
    disabled: boolean;
    onRun: () => void;
}> = ({ action, variant, busy, disabled, onRun }) => {
    const blocked = !!action.disabledReason;
    const button =
        variant === 'primary' ? (
            <PrimaryButton onClick={onRun} busy={busy} disabled={disabled || blocked}>
                {action.label}
            </PrimaryButton>
        ) : (
            <SecondaryButton onClick={onRun} busy={busy} disabled={disabled || blocked}>
                {action.label}
            </SecondaryButton>
        );
    // A disabled button swallows hover events, so the explanation goes on a
    // wrapper (Tooltip is focusable, so the reason is reachable by keyboard).
    return blocked ? <Tooltip content={action.disabledReason}>{button}</Tooltip> : button;
};

export const ActionRow: React.FC<ActionRowProps> = ({
    title,
    subtitle,
    age,
    evidence,
    meta,
    footnote,
    primaryAction,
    secondaryAction,
    tertiaryAction,
}) => {
    const [pending, setPending] = useState<Pending>(null);
    // The usual outcome of a successful write is that the parent refetches and
    // this row disappears. Keep a liveness flag so the finally-clause is a
    // no-op then instead of poking a dead component.
    const alive = useRef(true);
    useEffect(
        () => () => {
            alive.current = false;
        },
        [],
    );

    const run = (which: Exclude<Pending, null>, action: ActionRowAction) => () => {
        if (pending) return;
        setPending(which);
        void (async () => {
            try {
                await action.onClick();
            } catch (err) {
                // Backstop only: the pages toast their own, specific messages.
                // This exists so an UNEXPECTED throw still says something and
                // still re-enables the button.
                toast.error(err instanceof Error ? err.message : 'That action could not be completed.');
            } finally {
                if (alive.current) setPending(null);
            }
        })();
    };

    return (
        <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 flex-1">
                <p className="text-base font-semibold text-gray-900">{title}</p>
                {subtitle && <p className="mt-0.5 text-sm text-gray-600">{subtitle}</p>}
                {evidence && (
                    <p className="mt-2 flex items-start gap-1.5 text-xs italic leading-relaxed text-gray-500">
                        <Quote size={15} className="mt-0.5 flex-shrink-0 text-gray-400" aria-hidden="true" />
                        <span>{evidence}</span>
                    </p>
                )}
                {meta && <div className="mt-2 flex flex-wrap items-center gap-2">{meta}</div>}
                {footnote && <div className="mt-2 text-xs text-gray-500">{footnote}</div>}
            </div>

            <div className="flex flex-shrink-0 flex-wrap items-center gap-2 sm:justify-end">
                {age && <AgeChip {...age} />}
                {tertiaryAction && (
                    <button
                        type="button"
                        onClick={run('tertiary', tertiaryAction)}
                        disabled={pending !== null || !!tertiaryAction.disabledReason}
                        title={tertiaryAction.disabledReason ?? undefined}
                        className="px-2 py-1 text-sm font-medium text-gray-500 underline-offset-2 hover:text-gray-800 hover:underline disabled:opacity-50"
                    >
                        {tertiaryAction.label}
                    </button>
                )}
                {secondaryAction && (
                    <ActionButton
                        action={secondaryAction}
                        variant="secondary"
                        busy={pending === 'secondary'}
                        disabled={pending !== null}
                        onRun={run('secondary', secondaryAction)}
                    />
                )}
                <ActionButton
                    action={primaryAction}
                    variant="primary"
                    busy={pending === 'primary'}
                    disabled={pending !== null}
                    onRun={run('primary', primaryAction)}
                />
            </div>
        </div>
    );
};
