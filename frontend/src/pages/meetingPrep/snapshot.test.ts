// Tests for the Account Snapshot / "Built from" helpers and the error reader.
//
//   cd <repo>/frontend && node --test "src/pages/meetingPrep/**/*.test.ts"
//
// Date assertions hold in any timezone the runner sits in: a CRM calendar date
// is compared against the LOCAL-midnight rendering of the same day.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    formatMoney,
    formatShortDate,
    signalChipText,
    snapshotCompanies,
    visibleSignals,
} from './snapshot.ts';
import {
    STILL_WORKING_MESSAGE,
    UNSAVED_MESSAGE,
    isStaleVersion,
    newerVersion,
    readPrepError,
    writeOutcome,
} from './errors.ts';

describe('snapshotCompanies', () => {
    it('is empty for a missing snapshot and skips unnamed companies', () => {
        assert.deepEqual(snapshotCompanies(null), []);
        assert.deepEqual(snapshotCompanies({ companies: null }), []);
        const named = { name: 'Acme Bank' };
        assert.deepEqual(snapshotCompanies({ companies: [named, { name: '  ' }] }), [named]);
    });
});

describe('formatMoney', () => {
    it('whole US dollars', () => {
        assert.equal(formatMoney(12500), '$12,500');
        assert.equal(formatMoney(0), '$0');
        assert.equal(formatMoney(1234.56), '$1,235');
    });

    it('null when there is no amount', () => {
        assert.equal(formatMoney(null), null);
        assert.equal(formatMoney(undefined), null);
        assert.equal(formatMoney(Number.NaN), null);
    });
});

describe('formatShortDate', () => {
    const local = (y: number, m: number, d: number) =>
        new Date(y, m - 1, d).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });

    it('keeps a calendar date on its own day (never slides west of UTC)', () => {
        assert.equal(formatShortDate('2026-10-15'), local(2026, 10, 15));
    });

    it('takes the date part of a full timestamp', () => {
        assert.equal(formatShortDate('2026-10-15T00:00:00'), local(2026, 10, 15));
    });

    it('null for empty or unparseable input', () => {
        assert.equal(formatShortDate(null), null);
        assert.equal(formatShortDate(''), null);
        assert.equal(formatShortDate('soon'), null);
    });
});

describe('signals', () => {
    it('chip text is "Label · detail", or just the label', () => {
        assert.equal(signalChipText({ key: 'gmail', label: 'Gmail', detail: '6 threads' }), 'Gmail · 6 threads');
        assert.equal(signalChipText({ key: 'calendar', label: 'Calendar', detail: '' }), 'Calendar');
        assert.equal(signalChipText({ key: 'hubspot', label: '', detail: null }), 'hubspot');
    });

    it('drops empty entries', () => {
        assert.deepEqual(
            visibleSignals([
                { key: 'gmail', label: 'Gmail', detail: '6 threads' },
                { key: '', label: ' ' },
            ]).map(s => s.key),
            ['gmail'],
        );
        assert.deepEqual(visibleSignals(undefined), []);
    });
});

describe('readPrepError', () => {
    const axiosError = (status: number, data: unknown, extra: Record<string, unknown> = {}) => ({
        isAxiosError: true,
        response: { status, data },
        ...extra,
    });

    it('reads the 409 extras from the top level of the body', () => {
        const err = readPrepError(
            axiosError(409, {
                detail: 'A newer version (v6) exists — open it to edit.',
                latest_version: 6,
                latest_run_id: 'run-6',
            }),
            'fallback',
        );
        assert.deepEqual(err, {
            status: 409,
            message: 'A newer version (v6) exists — open it to edit.',
            latestVersion: 6,
            latestRunId: 'run-6',
            timedOut: false,
        });
        assert.ok(isStaleVersion(err));
    });

    it('reads them from inside `detail` too (HTTPException(detail={...}))', () => {
        const err = readPrepError(
            axiosError(409, {
                detail: { detail: 'A newer version (v3) exists — open it to edit.', latest_version: 3, latest_run_id: 'r3' },
            }),
            'fallback',
        );
        assert.equal(err.message, 'A newer version (v3) exists — open it to edit.');
        assert.equal(err.latestVersion, 3);
        assert.equal(err.latestRunId, 'r3');
    });

    it('passes a 502 detail through verbatim', () => {
        const detail = "Cubie couldn't build this briefing — the model timed out. Your current version is unchanged.";
        const err = readPrepError(axiosError(502, { detail }), 'fallback');
        assert.equal(err.status, 502);
        assert.equal(err.message, detail);
        assert.ok(!isStaleVersion(err));
    });

    it('says so on a client timeout — without claiming the version is unchanged', () => {
        // The browser gave up; the server may still save a new version.
        for (const code of ['ECONNABORTED', 'ETIMEDOUT']) {
            const err = readPrepError({ code, message: 'timeout of 180000ms exceeded' }, 'fallback');
            assert.equal(err.status, null);
            assert.equal(err.timedOut, true);
            assert.match(err.message, /took too long/);
            assert.doesNotMatch(err.message, /unchanged/);
        }
        assert.doesNotMatch(STILL_WORKING_MESSAGE, /unchanged/);
        assert.match(STILL_WORKING_MESSAGE, /may still be finishing/);
    });

    it('a server answer is never a client timeout', () => {
        assert.equal(readPrepError(axiosError(502, { detail: 'x' }, { code: 'ECONNABORTED' }), 'f').timedOut, false);
        assert.equal(readPrepError(new Error('Network Error'), 'f').timedOut, false);
    });

    it('falls back when there is nothing to read', () => {
        assert.equal(readPrepError(new Error('Network Error'), 'Could not save.').message, 'Could not save.');
        assert.equal(readPrepError(undefined, 'x').message, 'x');
        assert.equal(readPrepError(axiosError(500, '<html>'), 'Server error.').message, 'Server error.');
    });
});

describe('writeOutcome', () => {
    it('a saved version has a run id and a version number', () => {
        assert.equal(writeOutcome({ run_id: 'r5', version: 5 }), 'saved');
    });

    it('a brief with no run id or version was never stored — not shown, never "saved as vnull"', () => {
        // `_persist_meeting_prep_run` swallowed the error: run_id and version null.
        assert.equal(writeOutcome({ run_id: null, version: null }), 'unsaved');
        assert.equal(writeOutcome({ run_id: 'r5', version: null }), 'unsaved');
        assert.equal(writeOutcome({ run_id: '', version: 5 }), 'unsaved');
        assert.equal(writeOutcome({}), 'unsaved');
        assert.doesNotMatch(UNSAVED_MESSAGE, /v(null|undefined)/);
    });

    it('a failed generation is a failure first', () => {
        assert.equal(writeOutcome({ generation_failed: true, run_id: 'r5', version: 5 }), 'failed');
        assert.equal(writeOutcome(null), 'failed');
    });
});

describe('newerVersion', () => {
    const list = (...vs: number[]) => ({ versions: vs.map(v => ({ run_id: `r${v}`, version: v })) });

    it('finds a version that landed after the write the browser gave up on', () => {
        assert.deepEqual(newerVersion(list(5, 4, 3), 4), { run_id: 'r5', version: 5 });
        // Order does not matter.
        assert.deepEqual(newerVersion(list(3, 6, 4), 4), { run_id: 'r6', version: 6 });
    });

    it('is null when nothing newer has landed (yet)', () => {
        assert.equal(newerVersion(list(4, 3), 4), null);
        assert.equal(newerVersion({ versions: [] }, 4), null);
        assert.equal(newerVersion(null, 4), null);
    });

    it('anything saved is newer than an unsaved brief', () => {
        assert.deepEqual(newerVersion(list(1), null), { run_id: 'r1', version: 1 });
    });
});
