// Reading a failed Meeting Prep call — and a write that "succeeded" without
// saving anything.
//
// Three answers carry more than a sentence:
//
// * 409 — the person acted on a version that is no longer the latest (another
//   tab saved, or they kept an old page open). The server names the newer
//   version so the page can load it instead of guessing.
// * 502 — Cubie could not build the briefing (or rewrite the section). The
//   detail already says the current version is unchanged; the page shows it
//   as is and keeps what is on screen.
// * A client timeout — the BROWSER stopped waiting, not the server. A rewrite
//   or a regenerate can still finish and save a new version after the page
//   has given up, so a timeout must never be worded as "nothing changed": the
//   page says Cubie may still be finishing and looks for a newer version.
//
// The 409 extras are read from the top level of the body and, defensively,
// from inside `detail` too (the shape `HTTPException(detail={...})` produces),
// so the page does not depend on which of the two the router chose.
//
// Import-free apart from `errors.ts` (with its extension) so it runs under
// Node's test runner.

import { errorDetail } from '../../utils/errors.ts';

export interface PrepError {
    status: number | null;
    message: string;
    /** On a 409: the newest version number for this meeting. */
    latestVersion: number | null;
    /** On a 409: that version's run id, to load it directly. */
    latestRunId: string | null;
    /** The browser gave up waiting; the server may still be working. */
    timedOut: boolean;
}

/** What a client timeout says when the page has no better answer. Says
 *  nothing about the version, because the page cannot know. */
export const TIMEOUT_MESSAGE = 'Cubie took too long to answer — please try again.';

/** A write the browser stopped waiting for, when no newer version has landed
 *  (yet). */
export const STILL_WORKING_MESSAGE =
    'Cubie is taking longer than usual and may still be finishing. Reload in a minute to see whether it saved a new version.';

/** A write answered without a saved version (no run id or version number). */
export const UNSAVED_MESSAGE =
    "Cubie built a new version but couldn't save it. Your saved version is unchanged — please try again.";

type Loose = Record<string, unknown> | null | undefined;

const asObject = (v: unknown): Loose =>
    v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : null);

/** axios's codes for its own timeout ('ETIMEDOUT' under
 *  `transitional.clarifyTimeoutError`). */
const TIMEOUT_CODES = new Set(['ECONNABORTED', 'ETIMEDOUT']);

export function readPrepError(e: unknown, fallback: string): PrepError {
    const err = asObject(e);
    const response = asObject(err?.response);
    const status = num(response?.status);
    const body = asObject(response?.data);
    const nested = asObject(body?.detail);
    const timedOut = !response && typeof err?.code === 'string' && TIMEOUT_CODES.has(err.code);

    let message = errorDetail(e) ?? str(nested?.detail) ?? str(nested?.message) ?? str(body?.message);
    if (!message && timedOut) message = TIMEOUT_MESSAGE;

    return {
        status,
        message: message ?? fallback,
        latestVersion: num(body?.latest_version) ?? num(nested?.latest_version),
        latestRunId: str(body?.latest_run_id) ?? str(nested?.latest_run_id),
        timedOut,
    };
}

/** The version acted on is no longer the latest. */
export const isStaleVersion = (err: PrepError): boolean => err.status === 409;

// ─── After a write ─────────────────────────────────────────────────────────

/** What a write's 200 answer holds:
 *
 *  * `failed` — Cubie could not build it (`generation_failed`).
 *  * `unsaved` — a brief with no run id or version number: built, but never
 *    stored. Putting it on screen would hide the saved version (no version
 *    menu, no Word, nothing to edit) and toast "saved as vnull".
 *  * `saved` — a new (or unchanged) stored version, safe to show. */
export type WriteOutcome = 'failed' | 'unsaved' | 'saved';

export function writeOutcome(
    next: { generation_failed?: boolean | null; run_id?: string | null; version?: number | null } | null | undefined,
): WriteOutcome {
    if (!next || next.generation_failed) return 'failed';
    if (!str(next.run_id) || !num(next.version)) return 'unsaved';
    return 'saved';
}

/** After a write the browser stopped waiting for: the newest version in the
 *  list if it is newer than the one the write started from, else null. */
export function newerVersion<V extends { run_id: string; version: number }>(
    list: { versions?: readonly V[] | null } | null | undefined,
    fromVersion: number | null | undefined,
): V | null {
    let newest: V | null = null;
    for (const v of list?.versions ?? []) {
        if (v?.run_id && typeof v.version === 'number' && (!newest || v.version > newest.version)) newest = v;
    }
    return newest && newest.version > (fromVersion ?? 0) ? newest : null;
}
