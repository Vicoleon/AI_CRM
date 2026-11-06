// ST-5 — "I already have a DISC report from a previous job." The whole feature,
// as one quiet section of the guide page.
//
// THE DEFAULT PATH IS THAT NOBODY HAS ONE, AND THE UI HAS TO ACT LIKE IT
// ---------------------------------------------------------------------
// The router says it plainly: almost nobody has one, your profile is complete
// without it, and nothing here is waiting on you. So this section is:
//
//   * collapsed by default, and its collapsed form is one grey line — no count
//     badge, no amber, no chevron pointing at an unfinished step;
//   * never phrased as a step, a gap, or a percentage of anything. There is no
//     "add an official report to improve your profile", because that sentence
//     would imply an interview-only profile is the lesser one, and it is not;
//   * silent when it has nothing to say. An empty list is the complete answer
//     and `optional_notice` — the server's own sentence — is what says so;
//   * quiet when it FAILS, too. A section nobody needs must not raise a red
//     banner on a page about somebody's guide.
//
// The one time it opens itself is when the person has an upload of their own
// still awaiting review. That is not a nag: they started it, the decision is
// theirs, and burying it would be the unhelpful choice.
//
// WHY THIS COMPONENT OWNS THE READS
// ---------------------------------
// `merge_notice` rides `GET /uploads/{id}` and not the list, so an open row needs
// both calls. Doing that here — rather than in each card — keeps one owner of
// what the server said, and means the bounded parse poll is written once instead
// of once per row.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, FileUp, Loader2, Upload } from 'lucide-react';

import type { ConfirmUploadResponse, UploadRecord } from '../../../api/smartTeams';
import {
    getOwnUpload,
    listOwnUploads,
    smartTeamsError,
    uploadOfficialReport,
} from '../../../api/smartTeams';
import { PrimaryButton } from '../shared';
import { UploadCard } from './UploadCard';
import { frameworkName, isParsing } from './uploadState';

/** 5s, the interval every polled surface in this app uses. */
const POLL_MS = 5000;
/** ~2.5 minutes. Reading four numbers out of a PDF does not take longer than
 *  that, and a parse job that died leaves the row `pending` for ever — so the
 *  ceiling is what turns an endless spinner into something a person can act on. */
const MAX_POLLS = 30;

/** List the caller's uploads, then top up the OPEN ones with their detail read.
 *  The detail is where `merge_notice` lives, and that sentence has to be on
 *  screen before Confirm can be pressed. A detail read that fails leaves the
 *  list row in place rather than dropping the upload from the page. */
async function readUploads(): Promise<{
    uploads: UploadRecord[];
    optionalNotice: string;
    mergeableFrameworks: string[];
}> {
    const response = await listOwnUploads();
    const open = response.uploads.filter(row => row.review_status === 'pending');
    let rows = response.uploads;
    if (open.length > 0) {
        const details = await Promise.all(
            open.map(row => getOwnUpload(row.id).catch(() => null)),
        );
        const byId = new Map<string, UploadRecord>();
        for (const detail of details) {
            if (detail) byId.set(detail.id, detail);
        }
        rows = rows.map(row => byId.get(row.id) ?? row);
    }
    return {
        uploads: rows,
        optionalNotice: response.optional_notice,
        mergeableFrameworks: response.mergeable_frameworks,
    };
}

export const OfficialReportSection: React.FC<{
    /** Called after a merge. A merge writes a new profile version and can take a
     *  published guide out of company view, so the page has to re-read it. */
    onProfileChanged: () => void;
}> = ({ onProfileChanged }) => {
    const [uploads, setUploads] = useState<UploadRecord[] | null>(null);
    const [optionalNotice, setOptionalNotice] = useState<string | null>(null);
    const [mergeableFrameworks, setMergeableFrameworks] = useState<string[]>([]);
    const [loadFailed, setLoadFailed] = useState(false);
    const [open, setOpen] = useState(false);
    const [pollGaveUp, setPollGaveUp] = useState(false);
    const [checking, setChecking] = useState(false);

    // Opened for the person at most once, on a load that shows an open review.
    // A ref, not state, so it can never fight them closing it again.
    const autoOpened = useRef(false);

    const [file, setFile] = useState<File | null>(null);
    const [uploading, setUploading] = useState(false);
    const [uploadError, setUploadError] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement | null>(null);

    const pollRef = useRef<number | null>(null);
    const pollsRef = useRef(0);

    const load = useCallback(async (): Promise<void> => {
        try {
            const result = await readUploads();
            setUploads(result.uploads);
            setOptionalNotice(result.optionalNotice);
            setMergeableFrameworks(result.mergeableFrameworks);
            setLoadFailed(false);
            if (
                !autoOpened.current &&
                result.uploads.some(row => row.review_status === 'pending')
            ) {
                autoOpened.current = true;
                setOpen(true);
            }
        } catch {
            // Deliberately quiet. This section is optional; a failed read of it
            // must not turn the guide page into an error page. It says what
            // happened only to somebody who opens it and looks.
            setLoadFailed(true);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const stopPolling = useCallback(() => {
        if (pollRef.current) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
        }
    }, []);

    // A string key rather than the array, so the effect re-runs when the set of
    // parsing rows changes and not on every render.
    const parsingKey = (uploads ?? [])
        .filter(isParsing)
        .map(row => row.id)
        .join(',');

    useEffect(() => {
        if (!parsingKey) {
            stopPolling();
            return;
        }
        if (pollRef.current) return;
        pollsRef.current = 0;
        pollRef.current = window.setInterval(() => {
            pollsRef.current += 1;
            void load();
            if (pollsRef.current >= MAX_POLLS) {
                // Bounded on purpose. `pollGaveUp` is never un-set: the automatic
                // checking really has stopped, and it stays stopped until the
                // person presses Check now.
                stopPolling();
                setPollGaveUp(true);
            }
        }, POLL_MS);
        return stopPolling;
    }, [load, parsingKey, stopPolling]);

    useEffect(() => stopPolling, [stopPolling]);

    const checkNow = async (): Promise<void> => {
        setChecking(true);
        await load();
        setChecking(false);
    };

    const submit = async (): Promise<void> => {
        if (!file) return;
        setUploading(true);
        setUploadError(null);
        try {
            const created = await uploadOfficialReport(file);
            setUploads(current => [created, ...(current ?? []).filter(u => u.id !== created.id)]);
            setOptionalNotice(created.optional_notice);
            setPollGaveUp(false);
            setFile(null);
            if (fileInputRef.current) fileInputRef.current.value = '';
        } catch (e) {
            // The router's 400s are written to be shown: wrong extension, empty
            // file, over 10 MB. Verbatim beats a paraphrase that drifts from the
            // real bound.
            setUploadError(
                smartTeamsError(e, 'Cubie could not take that file. Nothing was stored.')
                    .message,
            );
        } finally {
            setUploading(false);
        }
    };

    const replaceRow = (row: UploadRecord): void => {
        setUploads(current => (current ?? []).map(u => (u.id === row.id ? row : u)));
    };

    const count = uploads?.length ?? 0;
    const mergeableList = mergeableFrameworks.map(frameworkName).join(', ');

    return (
        <section className="rounded-lg border border-gray-200 bg-white">
            <h2>
                <button
                    type="button"
                    onClick={() => setOpen(value => !value)}
                    aria-expanded={open}
                    aria-controls="st-official-report-body"
                    className="flex w-full items-center gap-2 rounded-lg px-5 py-4 text-left hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                >
                    {open ? (
                        <ChevronDown size={16} className="flex-shrink-0 text-gray-400" />
                    ) : (
                        <ChevronRight size={16} className="flex-shrink-0 text-gray-400" />
                    )}
                    <span className="flex-1">
                        <span className="text-sm font-medium text-gray-800">
                            Official assessment report
                        </span>
                        <span className="ml-2 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">
                            Optional
                        </span>
                        {/* One grey line, and only when there is something true to
                            put in it. Never a count that reads as a task list. */}
                        <span className="mt-0.5 block text-xs text-gray-500">
                            {count > 0
                                ? count === 1
                                    ? 'One report uploaded.'
                                    : `${count} reports uploaded.`
                                : 'Almost nobody has one. Your profile is complete without it.'}
                        </span>
                    </span>
                </button>
            </h2>

            {open && (
                <div id="st-official-report-body" className="border-t border-gray-100 px-5 py-4">
                    {/* Verbatim from the router (`OPTIONAL_NOTICE`). It is the
                        sentence that makes an empty list read as "fine". */}
                    {optionalNotice && (
                        <p className="text-sm leading-relaxed text-gray-700">{optionalNotice}</p>
                    )}

                    {/* ── The uploader ─────────────────────────────────────── */}
                    <div className="mt-4 rounded-lg border border-dashed border-gray-300 p-4">
                        <label
                            htmlFor="st-official-report-file"
                            className="flex items-center gap-2 text-sm font-medium text-gray-800"
                        >
                            <FileUp size={15} className="text-gray-400" />
                            Choose a report to upload
                        </label>
                        <p className="mt-1 text-xs leading-relaxed text-gray-500">
                            A PDF of the report itself, up to 10 MB. Cubie reads the scores out
                            of it and shows you what it found — nothing goes into your profile
                            until you say so.
                            {mergeableList && (
                                <>
                                    {' '}
                                    Today Cubie can only fold in a {mergeableList} report; it
                                    recognises the others and will tell you it cannot use them.
                                </>
                            )}
                        </p>
                        <input
                            id="st-official-report-file"
                            ref={fileInputRef}
                            type="file"
                            accept="application/pdf,.pdf"
                            onChange={event => {
                                setUploadError(null);
                                setFile(event.target.files?.[0] ?? null);
                            }}
                            className="mt-3 block w-full text-sm text-gray-600 file:mr-3 file:rounded-md file:border file:border-gray-300 file:bg-white file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-gray-700 hover:file:bg-gray-50"
                        />
                        {uploadError && (
                            <p role="alert" className="mt-2 text-xs leading-relaxed text-red-700">
                                {uploadError}
                            </p>
                        )}
                        <div className="mt-3">
                            <PrimaryButton
                                onClick={() => void submit()}
                                disabled={!file}
                                busy={uploading}
                            >
                                <Upload size={15} />
                                Upload
                            </PrimaryButton>
                        </div>
                    </div>

                    {/* ── What is already here ─────────────────────────────── */}
                    {uploads === null && !loadFailed && (
                        <p className="mt-4 flex items-center gap-2 text-xs text-gray-500">
                            <Loader2 className="animate-spin" size={13} />
                            Checking whether you have uploaded one before…
                        </p>
                    )}

                    {loadFailed && (
                        <p className="mt-4 text-xs leading-relaxed text-gray-600">
                            Cubie could not check whether you have uploaded a report before.
                            Nothing is wrong with your profile — this section is optional.{' '}
                            <button
                                type="button"
                                onClick={() => void load()}
                                className="font-medium text-brand hover:underline"
                            >
                                Try again
                            </button>
                        </p>
                    )}

                    {uploads !== null && uploads.length > 0 && (
                        <ul className="mt-4 space-y-3">
                            {uploads.map(record => (
                                <UploadCard
                                    key={record.id}
                                    record={record}
                                    pollGaveUp={pollGaveUp}
                                    onCheckNow={() => void checkNow()}
                                    checking={checking}
                                    onConfirmed={(response: ConfirmUploadResponse) => {
                                        replaceRow(response);
                                        onProfileChanged();
                                    }}
                                    onRejected={replaceRow}
                                />
                            ))}
                        </ul>
                    )}
                </div>
            )}
        </section>
    );
};

export default OfficialReportSection;
