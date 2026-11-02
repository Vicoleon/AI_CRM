import React from 'react';
import { AlertTriangle, ShieldCheck } from 'lucide-react';

// The approval card for an action Cubie paused on: a calendar invite, a
// scheduled task. Everything on it comes from the server, which
// renders it from the exact arguments the action will run with
// (rubik_core/orchestration/confirmation_render.py), never from the model's
// own description of the action, so what the person approves is what happens.
// Values are plain text on purpose: they may hold words an outsider wrote.

export interface ConfirmationAction {
    tool: string;
    title: string;
    fields: { label: string; value: string }[];
    warnings: string[];
}

export interface ConfirmationPayload {
    actions: ConfirmationAction[];
}

export const hasConfirmation = (c: ConfirmationPayload | undefined | null): c is ConfirmationPayload =>
    !!c && Array.isArray(c.actions) && c.actions.length > 0;

interface Props {
    confirmation: ConfirmationPayload;
    /** Present only while this is the conversation's open question. */
    onAnswer?: (answer: 'yes' | 'no') => void;
    disabled?: boolean;
}

export const ConfirmationCard: React.FC<Props> = ({ confirmation, onAnswer, disabled }) => {
    const many = confirmation.actions.length > 1;
    return (
        <div className="space-y-4">
            <p className="flex items-center gap-2 font-semibold text-gray-900">
                <ShieldCheck size={18} className="text-brand shrink-0" />
                {many
                    ? `Cubie needs your OK before it does these ${confirmation.actions.length} things`
                    : 'Cubie needs your OK before it does this'}
            </p>
            {confirmation.actions.map((action, i) => (
                <div key={`${action.tool}-${i}`} className="rounded-xl border border-gray-200 bg-gray-50 p-3 space-y-3">
                    <p className="font-medium text-gray-900">{action.title}</p>
                    {action.warnings.map((w, j) => (
                        <p
                            key={j}
                            className="flex gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
                        >
                            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
                            <span className="break-words">{w}</span>
                        </p>
                    ))}
                    <dl className="grid grid-cols-1 sm:grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
                        {action.fields.map((f, j) => (
                            <React.Fragment key={j}>
                                <dt className="font-medium text-gray-500">{f.label}</dt>
                                <dd className="whitespace-pre-wrap break-words text-gray-900 mb-1 sm:mb-0">{f.value}</dd>
                            </React.Fragment>
                        ))}
                    </dl>
                </div>
            ))}
            {onAnswer ? (
                <div className="flex flex-wrap gap-2">
                    <button
                        type="button"
                        disabled={disabled}
                        onClick={() => onAnswer('yes')}
                        className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50"
                    >
                        Yes, go ahead
                    </button>
                    <button
                        type="button"
                        disabled={disabled}
                        onClick={() => onAnswer('no')}
                        className="rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                    >
                        No, cancel
                    </button>
                </div>
            ) : null}
        </div>
    );
};
