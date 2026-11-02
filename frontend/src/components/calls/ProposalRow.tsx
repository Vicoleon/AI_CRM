// One proposed HubSpot write in the wrap-up screen: `current → proposed`, the
// quote it came from, and the AE's decision.
//
// Fully controlled. Every piece of decision state (ticked, confirmed, edited)
// lives in WrapUp.tsx so the screen can build one `decisions` array and one
// elapsed-time measurement without reaching into children.
//
// FOUR RULES IN HERE ARE GROUND RULES, NOT POLISH:
//
// 1. `is_overwrite` → amber "change" badge AND an explicit per-row confirm
//    before "Approve all" may include it. HubSpot already holds a different
//    value. The AI proposing "Globex" over an existing "Initech" is exactly
//    how a confident model destroys good data, and it is the failure that
//    ends adoption in week one. Never blind-overwrite. The confirm this row
//    collects is what becomes `confirm_overwrite: true` on that one decision —
//    the server refuses the write with `needs_confirmation` without it, and
//    there is no bulk way to supply it.
// 2. `needs_new_option` → the heard value is not one of the property's options,
//    so writing it would MINT a new one. Those rows are shown, clearly marked,
//    and cannot be approved into HubSpot. That is the whole point of the
//    constrained-vocabulary fix ("Acme" vs "Acmee"); a silently created enum
//    option is a data-quality bug nobody notices until a report is wrong.
// 3. Lead rows carry the inline hot|warm|cold select bound to `edited_value`.
//    The AEs' own ask, and the one thing they said they actively wanted.
// 4. Confidence is shown ONLY when it is low. A number on every row is noise,
//    and it trains the AE to ignore the one row where it mattered.
import React, { useState } from 'react';
import { AlertTriangle, ArrowRight, Ban, Pencil } from 'lucide-react';
import {
    isUnresolvedResult,
    LEAD_TEMPERATURES,
    leadTemperature,
    type ApproveResult,
    type ApproveResultStatus,
    type WrapUpItem,
} from '../../api/calls';
import { StatusBadge } from '../../pages/eos/_shared';
import { EvidenceQuote } from './EvidenceQuote';

export interface ProposalRowProps {
    item: WrapUpItem;
    /** Ticked = will be sent as `accept: true`. Everything defaults to ticked;
     *  rejecting is the deliberate act. */
    checked: boolean;
    onChange: (checked: boolean) => void;
    /** `is_overwrite` rows only — the AE has explicitly confirmed the change.
     *  Meaningless (and ignored) on any other row. */
    confirmed: boolean;
    onConfirm: (confirmed: boolean) => void;
    /** The AE's correction, or null to send the proposal as-is. */
    edited: string | null;
    onEdit: (value: string | null) => void;
    /** Present after an approve round-trip. A `write_failed` row MUST stay
     *  legible so the AE sees why nothing landed. */
    result?: ApproveResult | null;
    /** In flight, or the wrap-up is already complete — the row becomes a
     *  read-only record of what was decided. */
    locked?: boolean;
}

// Full literal class strings. A ternary between two whole literals is visible
// to the JIT; an interpolated fragment is not.
const ROW_NEEDS_CONFIRM = 'bg-amber-50/60 ring-1 ring-inset ring-amber-300';
const ROW_UNRESOLVED = 'bg-amber-50/40 ring-1 ring-inset ring-amber-200';
const ROW_TRIAGED = 'bg-gray-50';
const ROW_PLAIN = 'bg-white';

// All ten of the router's `DECISION_RESULTS`, typed as a full Record so a new
// status on the server is a compile error here rather than a bare wire string
// rendered raw in a badge. Four of them (`needs_confirmation`, `invalid`,
// `drift`, `write_failed`) leave the proposal claimable, so they read as work
// still owed rather than as an outcome.
type ResultTone = 'green' | 'amber' | 'blue' | 'red' | 'gray';

const RESULT_TONE: Record<ApproveResultStatus, ResultTone> = {
    written: 'green',
    already_written: 'green',
    recorded: 'green',
    triage: 'amber',
    rejected: 'gray',
    skipped: 'gray',
    needs_confirmation: 'amber',
    invalid: 'red',
    drift: 'amber',
    write_failed: 'red',
};

const RESULT_LABEL: Record<ApproveResultStatus, string> = {
    written: 'Written to HubSpot',
    already_written: 'Already in HubSpot',
    recorded: 'Recorded',
    triage: 'Sent to triage',
    rejected: 'Not filed',
    skipped: 'Already handled',
    needs_confirmation: 'Needs your confirmation',
    invalid: 'Value not accepted',
    drift: 'Changed in HubSpot',
    write_failed: 'Write failed',
};

// Message colour by tone, whole literals again.
const MESSAGE_CLS: Record<ResultTone, string> = {
    green: 'text-xs text-green-800',
    amber: 'text-xs text-amber-800',
    blue: 'text-xs text-blue-800',
    red: 'text-xs text-red-700',
    gray: 'text-xs text-gray-600',
};

export const ProposalRow: React.FC<ProposalRowProps> = ({
    item,
    checked,
    onChange,
    confirmed,
    onConfirm,
    edited,
    onEdit,
    result,
    locked = false,
}) => {
    const [editing, setEditing] = useState(false);

    const isLead = item.kind === 'lead';
    // Not approvable, ever: writing this value would create a new enum option
    // on the HubSpot property. It still renders — the AE needs to see what was
    // heard, and triage needs the row to exist.
    const triaged = Boolean(item.needs_new_option);
    const needsConfirm = item.is_overwrite && !confirmed;

    const proposed = edited ?? item.proposed_value ?? '';
    const wasEdited = edited !== null && edited !== (item.proposed_value ?? '');
    const current = item.current_value ?? '—';

    // The server came back saying this row is still the AE's to finish —
    // confirm the overwrite, fix the value, look at what changed, or retry.
    const unresolved = result ? isUnresolvedResult(result.status) : false;
    const resultMessage = result
        ? result.message ||
          (result.status === 'write_failed' ? 'HubSpot rejected the write.' : '')
        : '';
    const rowTone = triaged
        ? ROW_TRIAGED
        : needsConfirm && checked
          ? ROW_NEEDS_CONFIRM
          : unresolved
            ? ROW_UNRESOLVED
            : ROW_PLAIN;

    return (
        <div className={`px-4 py-3 ${rowTone}`}>
            <div className="flex items-start gap-3">
                <input
                    type="checkbox"
                    checked={checked && !triaged}
                    disabled={locked || triaged}
                    onChange={e => onChange(e.target.checked)}
                    aria-label={`Include: ${item.label}`}
                    title={
                        triaged
                            ? 'Cannot be written — the value is not one of this field’s options'
                            : undefined
                    }
                    className="mt-1 h-4 w-4 flex-shrink-0 rounded border-gray-300 text-brand focus:ring-brand disabled:opacity-40"
                />

                <div className="flex-1 min-w-0">
                    {/* label + current → proposed */}
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                        <span
                            className={`text-sm font-medium ${
                                checked && !triaged ? 'text-gray-900' : 'text-gray-400 line-through'
                            }`}
                        >
                            {item.label}
                        </span>

                        {proposed && (
                            <span className="inline-flex items-center gap-1.5 text-sm text-gray-500">
                                <span className="max-w-[16rem] truncate" title={current}>
                                    {current}
                                </span>
                                <ArrowRight size={15} className="flex-shrink-0 text-gray-400" />
                                {editing && !locked && !isLead ? (
                                    <input
                                        type="text"
                                        autoFocus
                                        value={proposed}
                                        onChange={e => onEdit(e.target.value)}
                                        onBlur={() => setEditing(false)}
                                        onKeyDown={e => {
                                            if (e.key === 'Enter' || e.key === 'Escape') {
                                                setEditing(false);
                                            }
                                        }}
                                        aria-label={`Correct the proposed value for ${item.label}`}
                                        className="w-48 rounded border border-gray-300 px-2 py-0.5 text-sm font-semibold text-gray-900 focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                                    />
                                ) : (
                                    <span className="font-semibold text-gray-900">{proposed}</span>
                                )}
                                {!editing && !locked && !isLead && !triaged && (
                                    <button
                                        type="button"
                                        onClick={() => setEditing(true)}
                                        aria-label={`Correct the proposed value for ${item.label}`}
                                        title="Correct this value"
                                        className="flex-shrink-0 text-gray-400 hover:text-brand focus:outline-none focus:ring-2 focus:ring-brand rounded"
                                    >
                                        <Pencil size={14} />
                                    </button>
                                )}
                                {wasEdited && <StatusBadge value="edited" tone="blue" />}
                            </span>
                        )}

                        {item.is_overwrite && (
                            <span className="inline-flex items-center gap-1">
                                <AlertTriangle size={15} className="text-amber-500" />
                                <StatusBadge value="change" tone="amber" />
                            </span>
                        )}

                        {/* The server's own verdict against
                            settings.CCS_MIN_CONFIDENCE_SHOW — never a threshold
                            hard-coded here, so tuning it is a config change. */}
                        {item.low_confidence && (
                            <span
                                className="text-xs text-gray-500"
                                title="The model hedged on this one — read the quote before approving."
                            >
                                less certain
                            </span>
                        )}
                    </div>

                    {/* lead temperature — the AEs' ask, bound to edited_value */}
                    {isLead && (
                        <div className="mt-2 flex items-center gap-2">
                            <label
                                className="text-xs font-medium text-gray-600"
                                htmlFor={`temp-${item.id}`}
                            >
                                Temperature
                            </label>
                            <select
                                id={`temp-${item.id}`}
                                value={edited ?? leadTemperature(item) ?? ''}
                                disabled={locked || triaged}
                                onChange={e => onEdit(e.target.value || null)}
                                className="rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-700 focus:border-brand focus:ring-1 focus:ring-brand outline-none disabled:opacity-50"
                            >
                                <option value="">— pick one —</option>
                                {LEAD_TEMPERATURES.map(t => (
                                    <option key={t} value={t}>
                                        {t}
                                    </option>
                                ))}
                            </select>
                        </div>
                    )}

                    {/* the quote — the thing that makes approving fast */}
                    <EvidenceQuote
                        text={item.evidence_text}
                        context={item.evidence_context}
                        label={item.label}
                    />

                    {/* not a valid option → triage, never a write */}
                    {triaged && (
                        <p className="mt-2 flex items-start gap-1.5 text-xs text-gray-700">
                            <Ban size={14} className="mt-0.5 flex-shrink-0 text-gray-400" />
                            <span>
                                <span className="font-semibold">
                                    &ldquo;{item.needs_new_option}&rdquo;
                                </span>{' '}
                                is not one of this field&rsquo;s options in HubSpot, so it
                                can&rsquo;t be written from here —{' '}
                                <span className="font-medium">sent to triage.</span> Someone adds
                                the option (or corrects it) in HubSpot first.
                            </span>
                        </p>
                    )}

                    {/* the overwrite gate */}
                    {item.is_overwrite && !triaged && (
                        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                            {confirmed ? (
                                <>
                                    <span className="font-medium text-amber-800">
                                        Change confirmed — {current} will be replaced.
                                    </span>
                                    {!locked && (
                                        <button
                                            type="button"
                                            onClick={() => onConfirm(false)}
                                            className="font-medium text-gray-500 underline hover:text-gray-800"
                                        >
                                            undo
                                        </button>
                                    )}
                                </>
                            ) : (
                                <>
                                    <span className="text-amber-800">
                                        HubSpot already holds{' '}
                                        <span className="font-semibold">{current}</span>. Confirm
                                        before this can be approved.
                                    </span>
                                    <button
                                        type="button"
                                        onClick={() => onConfirm(true)}
                                        disabled={locked}
                                        className="rounded-md border border-amber-400 bg-white px-2.5 py-1 font-medium text-amber-800 transition-colors hover:bg-amber-100 focus:outline-none focus:ring-2 focus:ring-amber-400 disabled:opacity-50"
                                    >
                                        Confirm change
                                    </button>
                                </>
                            )}
                        </div>
                    )}

                    {/* outcome of an approve round-trip. The server sends a
                        message on every status that needs explaining — the
                        HubSpot error, the value being replaced, what the field
                        now reads, which options do exist — so it is printed
                        whenever there is one rather than only on a failure. */}
                    {result && (
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                            <StatusBadge
                                value={RESULT_LABEL[result.status] ?? result.status}
                                tone={RESULT_TONE[result.status] ?? 'gray'}
                            />
                            {resultMessage && (
                                <span className={MESSAGE_CLS[RESULT_TONE[result.status] ?? 'gray']}>
                                    {resultMessage}
                                </span>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};

export default ProposalRow;
