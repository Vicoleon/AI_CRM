// The review step for a parsed official report: what Cubie thinks the numbers
// are, and the person's yes or no.
//
// THIS IS THE ONLY PLACE IN THE SMART TEAMS UI THAT RENDERS A RAW SCORE
// --------------------------------------------------------------------
// `UploadRecord.parsed.disc` is four 0-100 numbers. They are shown here because
// this screen exists so the subject can check them against the paper report in
// front of them — a review step that hides the values under review is theatre.
// It is safe because the route is own-uploads-only (the ownership test lives
// inside `uploads.get_upload`'s SQL predicate) plus the support read.
//
// The rules that follow from that, and they are absolute:
//   * this component is never rendered on a colleague-facing surface;
//   * `parsed` is never passed into a shared component or lifted into page
//     state that a colleague view also reads;
//   * nothing derived from these numbers (a band, a rank, a "high D") is
//     computed here — the merge produces a profile version and the guide is
//     where any of it turns into words.
//
// CONFIRM IS GATED ON ACTUAL REVIEW
// ---------------------------------
// Confirming replaces a layer of the person's profile and takes their published
// guide out of company view, so the button is disabled until the checkbox is
// ticked, and the checkbox says what it is affirming. Nothing is pre-ticked.
// `framework` is sent back as an echo of what was on screen: if a re-parse
// changed the proposal underneath them, the server refuses the confirm rather
// than merging something they never saw.
import React, { useState } from 'react';
import { AlertTriangle, Check, FileCheck2, X } from 'lucide-react';

import type { ConfirmUploadResponse, ParsedUploadPayload, UploadRecord } from '../../../api/smartTeams';
import { confirmUpload, rejectUpload, smartTeamsError } from '../../../api/smartTeams';
import { PrimaryButton, SecondaryButton } from '../shared';
import { DISC_NAMES, DISC_ORDER, frameworkName, scaleLabel } from './uploadState';

const ScoreTable: React.FC<{ parsed: ParsedUploadPayload }> = ({ parsed }) => {
    const scores = parsed.disc ?? {};
    const keys = [
        ...DISC_ORDER.filter(key => key in scores),
        ...Object.keys(scores).filter(key => !DISC_ORDER.includes(key as (typeof DISC_ORDER)[number])),
    ];

    if (keys.length === 0) {
        return (
            <p className="text-sm text-gray-600">
                Cubie recognised the report but there are no numbers in what it read back, so
                there is nothing here to check. Reject it and tell an admin — confirming would
                merge nothing.
            </p>
        );
    }

    return (
        <div className="overflow-x-auto">
            <table className="w-full text-sm">
                <caption className="sr-only">
                    The scores Cubie read out of your report, {scaleLabel(parsed.scale)}
                </caption>
                <thead>
                    <tr className="border-b border-gray-200 text-left text-xs uppercase tracking-wide text-gray-500">
                        <th scope="col" className="py-2 pr-4 font-medium">
                            From your report
                        </th>
                        <th scope="col" className="py-2 font-medium">
                            Value
                        </th>
                    </tr>
                </thead>
                <tbody>
                    {keys.map(key => (
                        <tr key={key} className="border-b border-gray-100 last:border-0">
                            <th scope="row" className="py-2 pr-4 text-left font-normal text-gray-700">
                                <span className="font-medium text-gray-900">{key}</span>
                                {DISC_NAMES[key] ? ` — ${DISC_NAMES[key]}` : ''}
                            </th>
                            <td className="py-2 font-medium tabular-nums text-gray-900">
                                {scores[key]}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
            <p className="mt-2 text-xs text-gray-500">
                Read {scaleLabel(parsed.scale)}. These are the numbers from your own report —
                only you and a support admin can see this screen.
            </p>
        </div>
    );
};

export const UploadReview: React.FC<{
    /** The DETAILED record from `GET /uploads/{id}` — `merge_notice` only rides
     *  that read, and only while the row is mergeable and still pending. */
    record: UploadRecord;
    onConfirmed: (response: ConfirmUploadResponse) => void;
    onRejected: (record: UploadRecord) => void;
}> = ({ record, onConfirmed, onRejected }) => {
    const [checked, setChecked] = useState(false);
    const [busy, setBusy] = useState<'confirm' | 'reject' | null>(null);
    const [error, setError] = useState<string | null>(null);

    const parsed = record.parsed;

    const confirm = async (): Promise<void> => {
        setBusy('confirm');
        setError(null);
        try {
            const response = await confirmUpload(record.id, parsed?.framework ?? null);
            onConfirmed(response);
        } catch (e) {
            setError(
                smartTeamsError(e, 'Cubie could not use this report. Nothing has changed.')
                    .message,
            );
        } finally {
            setBusy(null);
        }
    };

    const reject = async (): Promise<void> => {
        setBusy('reject');
        setError(null);
        try {
            const updated = await rejectUpload(record.id);
            onRejected(updated);
        } catch (e) {
            setError(smartTeamsError(e, 'Cubie could not record that. Nothing has changed.').message);
        } finally {
            setBusy(null);
        }
    };

    return (
        <div className="rounded-lg border border-gray-200 bg-white p-4">
            <h4 className="flex items-center gap-2 text-sm font-semibold text-gray-900">
                <FileCheck2 size={16} className="text-brand" />
                Cubie read this as a {frameworkName(parsed?.framework ?? record.framework)}{' '}
                report
            </h4>
            <p className="mt-1 text-xs leading-relaxed text-gray-600">
                Check these against the report itself before you decide. Nothing about your
                profile changes until you confirm.
            </p>

            <div className="mt-3">
                {parsed ? (
                    <ScoreTable parsed={parsed} />
                ) : (
                    <p className="text-sm text-gray-600">
                        There is no proposal on this upload to review.
                    </p>
                )}
            </div>

            {/* Verbatim from the router (`MERGE_NOTICE`). It states the guide
                consequence, and it belongs on the screen where the decision is
                made rather than in a toast afterwards. */}
            {record.merge_notice && (
                <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs leading-relaxed text-amber-900">
                    {record.merge_notice}
                </p>
            )}

            <label className="mt-3 flex cursor-pointer items-start gap-2.5 rounded-md border border-gray-200 bg-gray-50 p-3">
                <input
                    type="checkbox"
                    checked={checked}
                    onChange={event => setChecked(event.target.checked)}
                    className="mt-0.5 h-4 w-4 flex-shrink-0 rounded border-gray-300 text-brand focus:ring-brand"
                />
                <span className="text-sm leading-relaxed text-gray-800">
                    I have checked these numbers against my report and they are right.
                </span>
            </label>

            {error && (
                <div
                    role="alert"
                    className="mt-3 flex items-start gap-2.5 rounded-md border border-red-200 bg-red-50 p-3"
                >
                    <AlertTriangle className="mt-0.5 flex-shrink-0 text-red-500" size={16} />
                    <p className="text-xs leading-relaxed text-red-800">{error}</p>
                </div>
            )}

            <div className="mt-4 flex flex-wrap items-center gap-2">
                <PrimaryButton
                    onClick={() => void confirm()}
                    disabled={!checked || !parsed}
                    busy={busy === 'confirm'}
                >
                    <Check size={15} />
                    Use these in my profile
                </PrimaryButton>
                <SecondaryButton onClick={() => void reject()} busy={busy === 'reject'}>
                    <X size={15} />
                    No, leave my profile alone
                </SecondaryButton>
            </div>
            {!checked && (
                <p className="mt-2 text-xs text-gray-500">
                    Tick the box once you have compared the numbers.
                </p>
            )}
        </div>
    );
};

export default UploadReview;
