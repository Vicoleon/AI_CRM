// What the support screen must never get wrong about somebody's profile.
//
// Runs on Node's built-in test runner against Node's built-in TypeScript
// stripping, same as `interviewFlow.test.ts` — see that file's header for why
// the repo has no component test runner:
//
//   cd <repo>/frontend && node --test "src/components/smartTeams/**/*.test.ts"
//
// The cases pinned here are the ones where a wrong answer sends a support admin
// somewhere useless: reporting a hung job as a person's fault, reporting a
// person's unmade decision as an outage, or letting a failed retry masquerade as
// a broken published guide.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import type { GuideDocument, UploadRecord } from '../../../api/smartTeams';
import {
    agoLabel,
    currentGuide,
    daysSince,
    diagnose,
    headline,
    type SupportInterview,
} from './diagnosis.ts';

const NOW = new Date('2026-08-25T12:00:00Z');
const daysAgo = (n: number): string =>
    new Date(NOW.getTime() - n * 86_400_000).toISOString();

/** A completed, scored interview — the healthy baseline every case deviates from. */
function interview(over: Record<string, unknown> = {}): SupportInterview {
    return {
        status: 'completed',
        subject_email: 'someone@acme.example',
        interview_id: 'iv-1',
        sections_completed: [],
        progress: {
            sections_total: 9,
            sections_done: 9,
            steps_total: 155,
            steps_done: 155,
            seconds_total: 1764,
            seconds_done: 1764,
            percent: 100,
            minutes_remaining: 0,
            current_section_key: null,
            is_complete: true,
        },
        current_section: null,
        section_cursor: null,
        resume_section_key: null,
        section_answers: {},
        first_unanswered_item: null,
        started_at: daysAgo(10),
        last_activity_at: daysAgo(9),
        completed_at: daysAgo(9),
        profile_id: 'pr-1',
        sections: [],
        profile: {
            id: 'pr-1',
            version: 2,
            status: 'current',
            origin: 'interview',
            scored_at: daysAgo(9),
            provenance: {},
        },
        ...over,
    } as SupportInterview;
}

function guide(over: Partial<GuideDocument> = {}): GuideDocument {
    return {
        id: 'g-1',
        kind: 'personal_guide',
        subject_email: 'someone@acme.example',
        display_name: 'Someone',
        title: 'How to work with Someone',
        status: 'ready',
        error: null,
        profile_version: 2,
        current_profile_version: 2,
        stale: false,
        published: true,
        approved_at: daysAgo(5),
        approved_by: 'someone@acme.example',
        unpublished_at: null,
        rag_ingested: true,
        created_at: daysAgo(6),
        updated_at: daysAgo(5),
        advisory_notice: 'advisory',
        ...over,
    };
}

function upload(over: Partial<UploadRecord> = {}): UploadRecord {
    return {
        id: 'u-1',
        original_filename: 'disc.pdf',
        framework: 'disc',
        parsed: null,
        parse_error: null,
        review_status: 'merged',
        mergeable: true,
        merged_profile_id: 'pr-1',
        uploaded_at: daysAgo(3),
        parsed_at: daysAgo(3),
        reviewed_at: daysAgo(2),
        advisory_notice: 'advisory',
        ...over,
    };
}

const run = (over: Partial<Parameters<typeof diagnose>[0]> = {}) =>
    diagnose({
        interview: interview(),
        guides: [guide()],
        uploads: [],
        pendingSuggestions: 0,
        now: NOW,
        ...over,
    });

describe('daysSince / agoLabel', () => {
    it('returns null for a missing or unparseable timestamp', () => {
        // Not 0: "today" and "we cannot tell" mean opposite things when you are
        // deciding whether a job is hung.
        assert.equal(daysSince(null, NOW), null);
        assert.equal(daysSince('not a date', NOW), null);
        assert.equal(agoLabel(null), 'at an unknown time');
    });

    it('counts whole days', () => {
        assert.equal(daysSince(daysAgo(3), NOW), 3);
        assert.equal(agoLabel(0), 'today');
        assert.equal(agoLabel(1), 'yesterday');
        assert.equal(agoLabel(4), '4 days ago');
    });
});

describe('currentGuide', () => {
    it('prefers the published row over a newer failed retry', () => {
        // The regression this exists for: a failed regeneration is guides[0],
        // but the company is still reading the published one. Reporting the
        // retry as "their guide" says a live guide is broken when it is not.
        const rows = [guide({ id: 'retry', status: 'failed', published: false }), guide()];
        assert.equal(currentGuide(rows)?.id, 'g-1');
    });

    it('falls back to the newest row when nothing is published', () => {
        const rows = [guide({ id: 'newest', published: false })];
        assert.equal(currentGuide(rows)?.id, 'newest');
    });

    it('is null for no rows at all', () => {
        assert.equal(currentGuide([]), null);
    });
});

describe('the interview', () => {
    it('reports a completed-but-unscored interview as broken, not as waiting on them', () => {
        // The headline case for the whole screen. There is no failure column on
        // the interview row, so nothing else in the app will ever say this.
        const findings = run({ interview: interview({ profile: null }), guides: [] });
        const first = findings[0];
        assert.equal(first.id, 'interview-unscored');
        assert.equal(first.tone, 'blocked');
        assert.equal(first.owner, 'system');
    });

    it('does not invent a guide problem for somebody who never started', () => {
        // "No guide generated yet" under "no interview on file" is noise, and
        // enough noise pushes the real finding off the top of the page.
        const findings = run({
            interview: { status: 'not_started', subject_email: 'x@acme.example' },
            guides: [],
        });
        assert.deepEqual(findings.map(f => f.id), ['interview-none']);
    });

    it('separates a stalled interview from an active one by age alone', () => {
        const active = run({
            interview: interview({ status: 'in_progress', last_activity_at: daysAgo(2) }),
            guides: [],
        });
        assert.equal(active[0].tone, 'waiting');

        const stalled = run({
            interview: interview({ status: 'in_progress', last_activity_at: daysAgo(30) }),
            guides: [],
        });
        assert.equal(stalled[0].tone, 'attention');
    });
});

describe('the guide', () => {
    it('calls a long-pending generation broken, and a fresh one merely slow', () => {
        const fresh = run({ guides: [guide({ status: 'pending', published: false, updated_at: daysAgo(0) })] });
        assert.equal(fresh.find(f => f.id === 'guide-pending')?.tone, 'waiting');

        const hung = run({ guides: [guide({ status: 'pending', published: false, updated_at: daysAgo(4) })] });
        assert.equal(hung.find(f => f.id === 'guide-pending')?.tone, 'blocked');
    });

    it('puts an unapproved-but-ready guide on the person, never on the system', () => {
        const findings = run({ guides: [guide({ published: false, rag_ingested: false })] });
        const f = findings.find(x => x.id === 'guide-unapproved');
        assert.equal(f?.tone, 'waiting');
        assert.equal(f?.owner, 'them');
    });

    it('flags an unpublished guide that is still in the Chat corpus', () => {
        // The live privacy gap: consent was retracted, the vectors were not
        // purged, and this is the only screen where anyone would see it.
        const findings = run({ guides: [guide({ published: false, rag_ingested: true })] });
        const f = findings.find(x => x.id === 'guide-ingested-unpublished');
        assert.equal(f?.tone, 'blocked');
        assert.equal(f?.owner, 'support');
    });

    it('reports staleness against the server flag rather than comparing versions', () => {
        const findings = run({ guides: [guide({ stale: true, profile_version: 1 })] });
        assert.ok(findings.some(f => f.id === 'guide-stale'));
    });
});

describe('uploads', () => {
    it('reports a parse failure as a system problem', () => {
        const findings = run({ uploads: [upload({ parse_error: 'No DISC scores found.' })] });
        const f = findings.find(x => x.id.startsWith('upload-parse-'));
        assert.equal(f?.tone, 'attention');
        assert.equal(f?.owner, 'system');
    });

    it('says an unconfirmed merge is theirs, because a merge is consent', () => {
        const findings = run({ uploads: [upload({ review_status: 'pending', merged_profile_id: null })] });
        const f = findings.find(x => x.id.startsWith('upload-pending-'));
        assert.equal(f?.owner, 'them');
    });

    it('says nothing about an upload that merged cleanly', () => {
        const findings = run({ uploads: [upload()] });
        assert.equal(findings.filter(f => f.id.startsWith('upload-')).length, 0);
    });
});

describe('ordering and headline', () => {
    it('sorts blocked above waiting, and keeps rule order within a tone', () => {
        const findings = run({
            interview: interview({ profile: null }),
            guides: [guide({ published: false, rag_ingested: false })],
            uploads: [upload({ review_status: 'pending', merged_profile_id: null })],
            pendingSuggestions: 3,
        });
        const tones = findings.map(f => f.tone);
        assert.equal(tones[0], 'blocked');
        assert.deepEqual([...tones].sort((a, b) =>
            ['blocked', 'attention', 'waiting', 'ok'].indexOf(a) -
            ['blocked', 'attention', 'waiting', 'ok'].indexOf(b)), tones);
    });

    it('does not call a healthy profile broken just because a person owes a decision', () => {
        const h = headline(run({ guides: [guide({ published: false, rag_ingested: false })] }));
        assert.equal(h.tone, 'waiting');
    });

    it('is plainly ok when everything landed', () => {
        assert.equal(headline(run()).tone, 'ok');
    });
});
