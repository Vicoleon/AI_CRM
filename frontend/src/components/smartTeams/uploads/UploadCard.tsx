// One uploaded official report, in whichever state it is actually in.
//
// Presentational on purpose: the section above it owns the reads, the poll and
// the list, so there is exactly one place that knows what the server said about
// these rows. The only state here is the merge outcome, which arrives from an
// event (the person pressing Confirm) rather than from a fetch.
//
// THE STATES, AND WHY EACH IS ITS OWN SCREEN
// ------------------------------------------
//   parsing              `review_status: pending`, no `parsed`, no `parse_error`.
//                        The parse runs on cubie-worker, so this is real and it
//                        is polled — boundedly, because a job that died leaves
//                        the row exactly like this for ever.
//   could not read it    `parse_error` set. The sentence is OURS
//                        (`uploads.PARSE_REASONS`) — never a model string, never
//                        an excerpt of the document — so it is rendered
//                        verbatim, with the fact that matters beside it: nothing
//                        about the profile changed.
//   recognised, unusable `framework` set with `mergeable: false`. A DIFFERENT
//                        screen from "could not tell what this is": Cubie knows
//                        what the report is and has no adapter for it. Calling
//                        that "unrecognised" would misstate our own limits.
//   ready to review      `parsed` + `mergeable` + still pending → the review step
//                        (`UploadReview`), which is where the numbers appear.
//   merged               the numbers are in the profile, on a new version.
//   confirmed, not merged  the confirm was recorded and the merge did not finish.
//                        Rare, and it must not be dressed up as success.
//   rejected             the person said no. Kept, not deleted, and stated as
//                        their decision rather than as a failure.
//
// Nothing in here is a to-do. No badge, no count, no "action required": the whole
// feature is optional and almost nobody has an official report at all.
import React, { useState } from 'react';
import { AlertTriangle, CheckCircle2, FileText, Info, Loader2, RefreshCw, XCircle } from 'lucide-react';

import type { ConfirmUploadResponse, UploadRecord } from '../../../api/smartTeams';
import { SecondaryButton } from '../shared';
import { shortDate } from '../guide/guideState';
import { UploadReview } from './UploadReview';
import { frameworkName, isParsing } from './uploadState';

const Line: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <p className="mt-1 text-xs leading-relaxed text-gray-600">{children}</p>
);

export const UploadCard: React.FC<{
    record: UploadRecord;
    /** True once the section's bounded poll has stopped asking. */
    pollGaveUp: boolean;
    /** A manual re-read, for when the poll has given up. */
    onCheckNow: () => void;
    checking: boolean;
    /** Handed up so the section can refresh the list and the page can re-read
     *  the guide — a merge can take a published guide out of company view. */
    onConfirmed: (response: ConfirmUploadResponse) => void;
    onRejected: (record: UploadRecord) => void;
}> = ({ record, pollGaveUp, onCheckNow, checking, onConfirmed, onRejected }) => {
    // The rich merge outcome (which layer, which version, what happened to the
    // guide). Set from the Confirm event, not from a fetch — the list read that
    // follows carries the row but not the fallout.
    const [merge, setMerge] = useState<ConfirmUploadResponse | null>(null);

    const uploaded = shortDate(record.uploaded_at);
    const status = record.review_status;
    const parsing = isParsing(record) && !merge;
    const readyToReview = status === 'pending' && record.parsed && record.mergeable && !merge;

    return (
        <li className="rounded-lg border border-gray-200 bg-gray-50 p-4">
            <div className="flex items-start gap-2.5">
                <FileText className="mt-0.5 flex-shrink-0 text-gray-400" size={16} />
                <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-gray-900">
                        {record.original_filename}
                    </p>
                    <p className="text-xs text-gray-500">
                        {uploaded ? `Uploaded ${uploaded}` : 'Uploaded'}
                    </p>
                </div>
            </div>

            <div className="mt-3">
                {/* ── Still being read ─────────────────────────────────────── */}
                {parsing && (
                    <div>
                        <p className="flex items-center gap-2 text-sm text-gray-700">
                            <Loader2 className="animate-spin text-gray-400" size={14} />
                            Cubie is reading this report.
                        </p>
                        <Line>
                            It runs in the background and takes a moment. Nothing about your
                            profile changes until you have seen what it found and said yes.
                        </Line>
                        {pollGaveUp && (
                            <>
                                <Line>
                                    Cubie has stopped checking automatically — this is taking
                                    longer than reading a report should. Press Check now, and
                                    tell an admin if it stays like this.
                                </Line>
                                <div className="mt-2">
                                    <SecondaryButton onClick={onCheckNow} busy={checking}>
                                        <RefreshCw size={14} />
                                        Check now
                                    </SecondaryButton>
                                </div>
                            </>
                        )}
                    </div>
                )}

                {/* ── Cubie could not use it ───────────────────────────────── */}
                {!parsing && record.parse_error && status === 'pending' && (
                    <div className="rounded-md border border-gray-200 bg-white p-3">
                        <p className="flex items-start gap-2 text-sm text-gray-800">
                            <Info className="mt-0.5 flex-shrink-0 text-gray-400" size={15} />
                            {/* Verbatim from `uploads.PARSE_REASONS`. Ours, not a
                                model's, and not an excerpt of the document. */}
                            <span>{record.parse_error}</span>
                        </p>
                        {record.framework && (
                            <Line>
                                Cubie did work out that this is a{' '}
                                {frameworkName(record.framework)} report — it just has no way
                                to fold that kind into a profile yet.
                            </Line>
                        )}
                        <Line>
                            Nothing about your profile changed, and your profile is complete
                            without this. You can leave it as it is.
                        </Line>
                    </div>
                )}

                {/* ── Ready for the person to check ────────────────────────── */}
                {readyToReview && (
                    <UploadReview
                        record={record}
                        onConfirmed={response => {
                            setMerge(response);
                            onConfirmed(response);
                        }}
                        onRejected={onRejected}
                    />
                )}

                {/* ── Merged ───────────────────────────────────────────────── */}
                {(status === 'merged' || merge) && (
                    <div className="rounded-md border border-green-200 bg-green-50 p-3">
                        <p className="flex items-center gap-2 text-sm font-medium text-green-900">
                            <CheckCircle2 size={15} />
                            The numbers from this report are in your profile.
                        </p>
                        {merge ? (
                            <>
                                <Line>
                                    Your {frameworkName(merge.merged.framework)} layer now comes
                                    from this report. Everything else was left exactly as it
                                    was
                                    {merge.merged.untouched_frameworks.length > 0
                                        ? ` (${merge.merged.untouched_frameworks.length} other ${
                                              merge.merged.untouched_frameworks.length === 1
                                                  ? 'layer'
                                                  : 'layers'
                                          })`
                                        : ''}
                                    . Your profile is now version{' '}
                                    {merge.merged.profile_version}.
                                </Line>
                                {/* The guide fallout, with the two latencies kept
                                    apart: out of company view is immediate, out of
                                    Chat is a background job. */}
                                {merge.guide.message && (
                                    <p className="mt-2 rounded-md border border-amber-200 bg-amber-50 p-2.5 text-xs leading-relaxed text-amber-900">
                                        {merge.guide.message}
                                    </p>
                                )}
                                {merge.guide.still_ingested_document_ids.length > 0 && (
                                    <p className="mt-2 flex items-start gap-1.5 text-xs leading-relaxed text-amber-800">
                                        <AlertTriangle className="mt-0.5 flex-shrink-0" size={12} />
                                        Cubie has not finished removing the Chat copy of{' '}
                                        {merge.guide.still_ingested_document_ids.length === 1
                                            ? 'that guide'
                                            : 'those guides'}
                                        , so for a short while Chat may still quote the older
                                        wording.
                                    </p>
                                )}
                            </>
                        ) : (
                            <Line>
                                Merged
                                {shortDate(record.reviewed_at)
                                    ? ` on ${shortDate(record.reviewed_at)}`
                                    : ''}
                                .
                            </Line>
                        )}
                    </div>
                )}

                {/* ── Confirmed, but the merge did not finish ──────────────── */}
                {status === 'confirmed' && !record.merged_profile_id && !merge && (
                    <div className="rounded-md border border-amber-200 bg-amber-50 p-3">
                        <p className="flex items-center gap-2 text-sm font-medium text-amber-900">
                            <AlertTriangle size={15} />
                            Cubie recorded your confirmation but did not finish the merge.
                        </p>
                        <Line>
                            Your profile has not changed. Tell an admin and mention this file —
                            you do not need to upload it again.
                        </Line>
                    </div>
                )}

                {/* ── The person said no ───────────────────────────────────── */}
                {status === 'rejected' && (
                    <div className="rounded-md border border-gray-200 bg-white p-3">
                        <p className="flex items-center gap-2 text-sm text-gray-800">
                            <XCircle size={15} className="text-gray-400" />
                            You said no to this one.
                        </p>
                        <Line>
                            Nothing about your profile changed. Cubie keeps the file so an admin
                            can look at it if you ask them to.
                        </Line>
                    </div>
                )}
            </div>
        </li>
    );
};

export default UploadCard;
