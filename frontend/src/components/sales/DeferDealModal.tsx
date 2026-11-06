import React, { useState } from 'react';
import { Field, Modal, SubmitButton, inputCls } from '../eos/Modal';
import { toast } from '../eos/Toast';
import { cockpitError } from '../../api/cockpit';
import {
    MAX_DEFER_DAYS,
    NOTE_MAX_CHARS,
    deferDeal,
    deferOutcomeMessage,
    localDay,
} from '../../api/dealDefer';
import type { DeferResponse, DeferTarget } from '../../api/dealDefer';

// "Defer this deal": a follow-up date and a note, both required.
//
// A long-term deal with no real close date keeps showing up as noise.
// Deferring takes it off the rep's own lists until the follow-up date, when it
// comes back by itself, and puts a follow-up task on the deal in HubSpot for that day. The
// manager views still list it, with the note, so the note is written for the
// person who reviews the pipeline as much as for the rep.
//
// One dialog for every surface (My Day's blocks, the Daily rhythm, the Daily
// Briefing's deal cards), so the rules and the copy cannot drift between them.
// The parent owns `target`: null is closed.

export interface DeferDealModalProps {
    target: DeferTarget | null;
    onClose: () => void;
    /** After a defer landed in Cubie (the HubSpot task may still have failed —
     *  the toast says so). The parent refetches the list the row was on. */
    onDeferred: (res: DeferResponse) => void;
    /** Prefill, for "Retry the HubSpot task" from the Deferred list: the same
     *  date and note re-send the same defer, which retries only the task. */
    initialDate?: string;
    initialNote?: string;
}

export const DeferDealModal: React.FC<DeferDealModalProps> = ({
    target,
    onClose,
    onDeferred,
    initialDate,
    initialNote,
}) => {
    // Remounted per target (the parent keys it), so plain initial state is the
    // reset — no effect needed to clear the last deal's note.
    const [date, setDate] = useState(initialDate ?? '');
    const [note, setNote] = useState(initialNote ?? '');
    const [submitting, setSubmitting] = useState(false);

    const earliest = localDay(1);
    const latest = localDay(MAX_DEFER_DAYS);
    const dateOk = date >= earliest && date <= latest;
    const noteOk = note.trim().length > 0 && note.trim().length <= NOTE_MAX_CHARS;
    const canSubmit = !!target && dateOk && noteOk && !submitting;

    const onSubmit = async (e: React.FormEvent): Promise<void> => {
        e.preventDefault();
        if (!target || !canSubmit) return;
        setSubmitting(true);
        try {
            const res = await deferDeal(target.deal_id, {
                follow_up_on: date,
                note: note.trim(),
                deal_name: target.deal_name ?? null,
            });
            const msg = deferOutcomeMessage(res);
            if (msg.ok) toast.success(msg.text);
            else toast.error(msg.text);
            onDeferred(res);
            onClose();
        } catch (err) {
            // Stays open: the rep's note is not lost to a failed request.
            toast.error(cockpitError(err, 'The deal could not be deferred.').message);
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <Modal isOpen={target !== null} onClose={onClose} title="Defer this deal" maxWidth="max-w-lg">
            <form onSubmit={onSubmit} className="space-y-4">
                <p className="text-sm text-gray-600">{target?.deal_name || `Deal ${target?.deal_id ?? ''}`}</p>
                <Field label="Follow up on">
                    <input
                        required
                        type="date"
                        className={inputCls}
                        value={date}
                        min={earliest}
                        max={latest}
                        onChange={e => setDate(e.target.value)}
                    />
                </Field>
                {date && !dateOk && (
                    <p className="text-xs text-red-700" role="alert">
                        Pick a date after today and no more than {MAX_DEFER_DAYS} days out.
                    </p>
                )}
                <Field label="Note">
                    <textarea
                        required
                        rows={3}
                        maxLength={NOTE_MAX_CHARS}
                        className={inputCls}
                        value={note}
                        onChange={e => setNote(e.target.value)}
                        placeholder="Their budget opens in Q1; the CFO asked us to check back after the January board meeting."
                    />
                </Field>
                <p className="text-xs text-gray-500">
                    The deal leaves your My Day, Daily rhythm and Daily Briefing until that date, then
                    comes back by itself. Cubie adds a follow-up task for that day on the deal in
                    HubSpot, with your note as the task. The stage and close date are not changed, and
                    your manager still sees the deal with this note.
                </p>
                <SubmitButton disabled={!canSubmit} onCancel={onClose}>
                    {submitting ? 'Deferring…' : 'Defer'}
                </SubmitButton>
            </form>
        </Modal>
    );
};
