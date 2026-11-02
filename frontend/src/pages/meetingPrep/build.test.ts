// Reading a background build of a Meeting Prep brief.
//
//   cd <repo>/frontend && node --test "src/pages/meetingPrep/**/*.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    BUILD_POLL_MAX_MS,
    BUILD_POLL_MS,
    FIRST_BUILD_FAILED,
    buildStateLabel,
    formatElapsed,
    isSettled,
    nextPollDelay,
    readAcceptedBuild,
    readBuildStatus,
    readPendingBuild,
    readPrepAnswer,
    readyAction,
    staleBuilds,
    waitingBarFraction,
} from './build.ts';

describe('readPendingBuild', () => {
    it('reads a queued or running build', () => {
        for (const status of ['queued', 'running'] as const) {
            assert.deepEqual(
                readPendingBuild({
                    run_id: 'r1',
                    status,
                    kind: 'regenerate',
                    started_at: '2026-10-02T15:00:00',
                    event_id: 'evt-1',
                    title: 'Quarterly review',
                }),
                {
                    run_id: 'r1',
                    status,
                    kind: 'regenerate',
                    started_at: '2026-10-02T15:00:00',
                    event_id: 'evt-1',
                    title: 'Quarterly review',
                },
            );
        }
    });

    it('a settled build is not pending', () => {
        assert.equal(readPendingBuild({ run_id: 'r1', status: 'ready' }), null);
        assert.equal(readPendingBuild({ run_id: 'r1', status: 'failed' }), null);
    });

    it('nothing usable is null', () => {
        for (const bad of [null, undefined, 'x', 3, [], {}, { status: 'queued' }, { run_id: '  ', status: 'queued' }]) {
            assert.equal(readPendingBuild(bad), null, JSON.stringify(bad));
        }
    });

    it('fills the optional fields defensively', () => {
        const b = readPendingBuild({ run_id: 'r1', status: 'running', kind: 'weird', title: '' });
        assert.equal(b?.kind, 'first');
        assert.equal(b?.title, null);
        assert.equal(b?.started_at, null);
    });
});

describe('readAcceptedBuild', () => {
    it('a build that settled before the answer was written is still followed', () => {
        for (const status of ['ready', 'failed', undefined]) {
            const b = readAcceptedBuild({ run_id: 'r1', status, kind: 'regenerate' });
            assert.equal(b?.run_id, 'r1');
            assert.equal(b?.status, 'running', String(status));
        }
        assert.equal(readAcceptedBuild({ run_id: 'r1', status: 'queued' })?.status, 'queued');
    });

    it('no run id is no build', () => {
        assert.equal(readAcceptedBuild({ status: 'queued' }), null);
        assert.equal(readAcceptedBuild(null), null);
    });
});

describe('readPrepAnswer', () => {
    it('200 is the latest version', () => {
        const data = { title: 'x', run_id: 'r1', version: 2 };
        assert.deepEqual(readPrepAnswer(200, data), { kind: 'ready', data });
    });

    it('202 is the build making the first version', () => {
        const answer = readPrepAnswer(202, { run_id: 'r1', status: 'queued', kind: 'first', title: null });
        assert.equal(answer.kind, 'building');
        assert.equal(answer.kind === 'building' && answer.build.run_id, 'r1');
    });

    it('a 202 without a build handle is an error, never an empty brief', () => {
        assert.throws(() => readPrepAnswer(202, { detail: 'x' }), new RegExp(FIRST_BUILD_FAILED.slice(0, 20)));
    });
});

describe('readBuildStatus', () => {
    it('reads a ready build', () => {
        const s = readBuildStatus(
            {
                run_id: 'r1',
                event_id: 'evt-1',
                status: 'ready',
                kind: 'regenerate',
                version: 4,
                detail: null,
                started_at: '2026-10-02T15:00:00',
                finished_at: '2026-10-02T15:01:30',
            },
            'r1',
        );
        assert.equal(s.status, 'ready');
        assert.equal(s.version, 4);
        assert.equal(s.kind, 'regenerate');
        assert.equal(s.finished_at, '2026-10-02T15:01:30');
        assert.ok(isSettled(s));
    });

    it('reads a failure with its reader-safe detail', () => {
        const s = readBuildStatus(
            { run_id: 'r1', status: 'failed', detail: 'Cubie took too long to build this briefing — try again.' },
            'r1',
        );
        assert.equal(s.status, 'failed');
        assert.equal(s.version, null);
        assert.match(s.detail ?? '', /took too long/);
        assert.ok(isSettled(s));
    });

    it('an unknown status keeps waiting (the server settles every build)', () => {
        const s = readBuildStatus({ status: 'mystery' }, 'r1');
        assert.equal(s.status, 'running');
        assert.equal(s.run_id, 'r1');
        assert.ok(!isSettled(s));
    });

    it('survives a body that is not an object', () => {
        const s = readBuildStatus(null, 'r9');
        assert.equal(s.run_id, 'r9');
        assert.equal(s.status, 'running');
    });
});

describe('staleBuilds', () => {
    it('a build of this meeting the server no longer reports is dropped (A → B → A)', () => {
        // Followed on the first visit to A; it settled as v3 while the reader
        // was on B. The brief that opens is v3 with nothing in flight.
        assert.deepEqual(staleBuilds([['rA', 'evt-A']], 'evt-A', null), ['rA']);
        assert.deepEqual(staleBuilds([['rA', 'evt-A']], 'evt-A', undefined), ['rA']);
    });

    it('the build the server still reports is kept', () => {
        assert.deepEqual(staleBuilds([['rA', 'evt-A']], 'evt-A', { run_id: 'rA' }), []);
    });

    it('a different build in flight (another tab) replaces it', () => {
        assert.deepEqual(staleBuilds([['rA', 'evt-A']], 'evt-A', { run_id: 'rX' }), ['rA']);
    });

    it("another meeting's build is not this answer's to judge", () => {
        assert.deepEqual(staleBuilds([['rB', 'evt-B'], ['rA', 'evt-A']], 'evt-A', null), ['rA']);
    });

    it('a build started after the request went out is never dropped', () => {
        // Not in the snapshot the page took when it asked.
        assert.deepEqual(staleBuilds([], 'evt-A', null), []);
    });
});

describe('readyAction', () => {
    it('a newer version replaces the one on screen', () => {
        assert.equal(readyAction(4, 3, false), 'show');
    });

    it('never switches back to a version that is not newer than the one on screen', () => {
        // The build made v3, but v4 was saved meanwhile (another tab) and is
        // on screen — or v3 itself already is.
        assert.equal(readyAction(3, 4, false), 'quiet');
        assert.equal(readyAction(3, 3, false), 'quiet');
        assert.equal(readyAction(3, 4, true), 'quiet');
    });

    it('offers it while the reader is holding the screen', () => {
        assert.equal(readyAction(4, 3, true), 'offer');
    });

    it('an unknown number is offered, never switched to', () => {
        assert.equal(readyAction(null, 3, false), 'offer');
    });

    it('nothing numbered on screen: a numbered version shows', () => {
        assert.equal(readyAction(1, null, false), 'show');
    });
});

describe('nextPollDelay', () => {
    it('is steady while the status call answers', () => {
        assert.equal(nextPollDelay(0), BUILD_POLL_MS);
    });

    it('backs off while it fails, up to a ceiling', () => {
        assert.equal(nextPollDelay(1), BUILD_POLL_MS * 2);
        assert.equal(nextPollDelay(2), BUILD_POLL_MS * 4);
        assert.equal(nextPollDelay(10), BUILD_POLL_MAX_MS);
    });
});

describe('formatElapsed', () => {
    it('reads as a clock', () => {
        assert.equal(formatElapsed(0), '0:00');
        assert.equal(formatElapsed(7_400), '0:07');
        assert.equal(formatElapsed(102_000), '1:42');
        assert.equal(formatElapsed(3_723_000), '1:02:03');
    });

    it('never goes negative (a server clock ahead of the browser)', () => {
        assert.equal(formatElapsed(-5_000), '0:00');
        assert.equal(formatElapsed(null), '0:00');
    });
});

describe('waitingBarFraction', () => {
    it('starts empty, grows, and never claims done', () => {
        assert.equal(waitingBarFraction(0), 0);
        const oneMin = waitingBarFraction(60_000);
        const twoMin = waitingBarFraction(120_000);
        assert.ok(oneMin > 0.5 && oneMin < twoMin, `${oneMin} ${twoMin}`);
        assert.ok(waitingBarFraction(60 * 60_000) <= 0.95);
        assert.equal(waitingBarFraction(-1), 0);
    });
});

describe('buildStateLabel', () => {
    it('names every state', () => {
        assert.equal(buildStateLabel('queued'), 'Queued');
        assert.equal(buildStateLabel('running'), 'Building');
        assert.equal(buildStateLabel('ready'), 'Ready');
        assert.equal(buildStateLabel('failed'), 'Failed');
    });
});
