// Contract tests for transcript coverage on the Sales Call Capture client.
//
//   cd <repo>/frontend && node --test "src/api/**/*.test.ts"
//
// (The bare directory form works on Node 22 but not on Node 24, which tries to
// load the path as a module — pass the glob. `npx tsc -b` covers this file too,
// through tsconfig.test.json, so it cannot rot silently.)
//
// WHY COVERAGE NEEDS TESTS OF THIS SHAPE
// ──────────────────────────────────────
// `artifactConfig.autoTranscriptionGeneration: ON` is a DEFAULT, not a lock —
// a host can stop transcription mid-call from the Meet UI, and the Meet API has
// no method that would let Cubie prevent or observe it. What comes back is
// INDISTINGUISHABLE from a complete transcript: the conference reports ENDED,
// the sweep ingests it correctly, and the AE gets a wrap-up that looks entirely
// normal built from part of the call. The band this file guards is the only
// thing that tells them otherwise.
//
// So the failures worth catching are the silent ones, and all three are the
// shape a unit test over this client alone would pass:
//
//   * The `reason` vocabulary drifting from the router's. `reason` is a bare
//     `str` on the wire; a code this build has never heard of must degrade to a
//     neutral sentence, and a code the client names but the router never sends
//     is copy that can never appear. The band's cause line is keyed on one of
//     these strings, and an early draft of it keyed on `transcription_stopped`
//     — a plausible name the router does not emit — which would have rendered
//     the fallback sentence on every truncated call forever.
//   * The band moving BELOW the rows. It exists to be read before the AE starts
//     ticking, not after they have approved.
//   * Coverage being gated on `can_read_transcript`. It is metadata —
//     durations and a closed code — and a supervisor is entitled to know a call
//     was half-captured even though they may never read a word of it.
//
// Each is therefore re-derived FROM the other side (the router source, the
// screen's own source) rather than restated as a constant this file could get
// equally wrong.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

// `api/calls.ts` imports `./client`, extensionless, the way every module in this
// app does — a specifier Vite resolves and bare Node does not. Teach the runner
// Vite's rule, and only for our own files: an unscoped hook also rewrites the
// CJS `require`s inside node_modules and breaks axios's dependency tree.
const resolveExtensionless: ResolveHookSync = (specifier, context, nextResolve) => {
    const parent = context.parentURL ?? '';
    const relative = /^\.{1,2}\//.test(specifier);
    if (parent.includes('/frontend/src/') && relative && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
};

registerHooks({ resolve: resolveExtensionless });

const { COVERAGE_REASONS, coverageMinutes, isCoverageReason, isTruncatedCoverage } =
    await import('./calls.ts');
type TranscriptCoverage = import('./calls.ts').TranscriptCoverage;

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string =>
    fs.readFileSync(path.join(REPO, relative), 'utf8');

const CALLS_PY = read('backend/routers/calls.py');
const INGEST_PY = read('rubik_core/call_capture/transcript_ingest.py');
const CALLS_TS = read('frontend/src/api/calls.ts');
const WRAPUP_TSX = read('frontend/src/pages/calls/WrapUp.tsx');

/** The body of a `{ … }` block that starts at `opener`, up to the first line
 *  that closes it at `closer`'s indentation. Crude, and enough for a python dict
 *  literal or a TS declaration — the closer is passed explicitly because an
 *  indented python `return {` closes at four spaces and a top-level TS
 *  declaration closes at column zero. */
const blockAfter = (source: string, opener: string, closer = '\n}'): string => {
    const start = source.indexOf(opener);
    assert.notEqual(start, -1, `could not find ${JSON.stringify(opener)}`);
    const rest = source.slice(start + opener.length);
    const end = rest.indexOf(closer);
    assert.notEqual(end, -1, `${JSON.stringify(opener)} block is unterminated`);
    return rest.slice(0, end);
};

const coverage = (over: Partial<TranscriptCoverage> = {}): TranscriptCoverage => ({
    covered_minutes: 20.4,
    call_minutes: 51.2,
    gap_minutes: 30.8,
    truncated: true,
    reason: 'truncated',
    ...over,
});

// ─── the reason vocabulary ───────────────────────────────────────────────────

describe('coverage reasons', () => {
    // The router's closed set, which it spells as references to the ingest
    // module's own constants (`_ingest.COVERAGE_TRUNCATED`), plus the literal it
    // substitutes for anything outside the set. Both reach this client as wire
    // strings, so both are resolved back to the string a browser will see —
    // reading the router alone would only pin the CONSTANT names, which are not
    // what travels.
    const declared = /COVERAGE_REASONS = frozenset\(\s*\{([\s\S]*?)\}\s*\)/.exec(CALLS_PY);
    const fallback = /_COVERAGE_REASON_FALLBACK = "([a-z_]+)"/.exec(CALLS_PY);
    const body = declared?.[1] ?? '';

    const named = [...body.matchAll(/_ingest\.(COVERAGE_[A-Z_]+)/g)].map(m => m[1]);
    const resolved = named.map(name => ({
        name,
        value: (new RegExp(`^${name} = "([a-z_]+)"`, 'm').exec(INGEST_PY) ?? [])[1] ?? null,
    }));
    const emitted = [
        ...new Set([
            ...[...body.matchAll(/"([a-z_]+)"/g)].map(m => m[1]),
            ...resolved.map(r => r.value).filter((v): v is string => Boolean(v)),
            ...(fallback ? [fallback[1]] : []),
        ]),
    ].sort();

    it('finds the router vocabulary at all', () => {
        // A guard on the extraction: a regex that silently matched nothing would
        // make every assertion below vacuously true.
        assert.ok(declared, 'COVERAGE_REASONS not found in backend/routers/calls.py');
        assert.ok(fallback, '_COVERAGE_REASON_FALLBACK not found');
        assert.ok(emitted.length >= 6, `only found ${emitted.length} reason codes`);
    });

    it('names only ingest constants that exist', () => {
        // Not a frontend concern on its face, and it is here because the two
        // sides are separate files edited by separate hands: a router that
        // imports `_ingest.COVERAGE_GAP_BELOW_FLOOR` after ingest renamed it
        // raises at MODULE LOAD, which takes out every /api/calls endpoint —
        // not just coverage — and no type checker on either side sees it.
        assert.deepEqual(
            resolved.filter(r => !r.value).map(r => r.name),
            [],
        );
    });

    it('declares every code the router can send', () => {
        // Subset, not equality: this client may keep a retired code declared so
        // an older backend still degrades to an honest sentence (the
        // `crm_read_failed` rule in cockpit.ts). What it may never do is meet a
        // code it has never heard of.
        assert.deepEqual(
            emitted.filter(reason => !isCoverageReason(reason)),
            [],
        );
    });

    it('declares nothing that could never arrive', () => {
        // The other direction, as a warning rather than a fence: every declared
        // code should be one the router can actually send, or copy keyed on it
        // is copy that never renders. `unknown` is the router's own fallback and
        // belongs on both lists.
        assert.deepEqual(
            [...COVERAGE_REASONS].filter(reason => !emitted.includes(reason)),
            [],
        );
    });

    it('degrades a code this build has never heard of instead of throwing', () => {
        // `reason` is a bare `str` server-side, so an ingest build ahead of the
        // router (or a router ahead of this file) is a runtime event, not a
        // compile error. The band still renders — `truncated` is the gate — with
        // the neutral sentence.
        assert.equal(isCoverageReason('truncated'), true);
        assert.equal(isCoverageReason('some_future_code'), false);
        assert.equal(isCoverageReason(undefined), false);
        assert.equal(isCoverageReason(null), false);
    });
});

// ─── the render gate ─────────────────────────────────────────────────────────

describe('isTruncatedCoverage', () => {
    it('says nothing about a call nobody measured', () => {
        // Null is "no information", NOT "fully covered". Every session that
        // predates the measurement is in that state, and a line about it on all
        // of them is worse than silence.
        assert.equal(isTruncatedCoverage(null), false);
        assert.equal(isTruncatedCoverage(undefined), false);
    });

    it('says nothing about a fully captured call', () => {
        // No reassuring badge on the normal case: it is noise on every call and
        // it dilutes the one that matters.
        assert.equal(
            isTruncatedCoverage(coverage({ truncated: false, reason: 'covered' })),
            false,
        );
    });

    it('trusts the server flag rather than re-deriving it', () => {
        // A big gap that the server did NOT call truncated stays quiet. The rule
        // is two settings (`CCS_COVERAGE_MIN_RATIO`, `CCS_COVERAGE_MIN_GAP_SECONDS`)
        // evaluated at ingest; a second opinion here is how the two drift, and
        // the drift direction that costs adoption is the false alarm.
        assert.equal(
            isTruncatedCoverage(
                coverage({
                    truncated: false,
                    reason: 'within_ratio',
                    covered_minutes: 4,
                    call_minutes: 51,
                    gap_minutes: 47,
                }),
            ),
            false,
        );
        assert.equal(isTruncatedCoverage(coverage()), true);
    });
});

// ─── the "X of Y minutes" line ───────────────────────────────────────────────

describe('coverageMinutes', () => {
    it("rounds the router's tenths to whole minutes", () => {
        assert.deepEqual(coverageMinutes(coverage()), { covered: 20, call: 51 });
    });

    it('reports a wholly uncaptured call as zero rather than as nothing', () => {
        // "0 of 51 minutes" is the single most important thing this line can
        // say, so zero must not be folded away as falsy.
        assert.deepEqual(coverageMinutes(coverage({ covered_minutes: 0 })), {
            covered: 0,
            call: 51,
        });
    });

    it('withholds the numbers when they would read as nonsense', () => {
        // No denominator, a broken clock, or a pair that rounds to the same
        // number ("covers 51 of 51 minutes" under a warning band). The band
        // still has a true sentence to fall back on.
        assert.equal(coverageMinutes(coverage({ call_minutes: null })), null);
        assert.equal(coverageMinutes(coverage({ call_minutes: 0 })), null);
        assert.equal(
            coverageMinutes(coverage({ covered_minutes: 50.6, call_minutes: 51 })),
            null,
        );
        assert.equal(coverageMinutes(null), null);
        assert.equal(coverageMinutes(undefined), null);
    });
});

// ─── the wire shape ──────────────────────────────────────────────────────────

describe('the payload the router actually sends', () => {
    const returned = blockAfter(
        CALLS_PY.slice(CALLS_PY.indexOf('def _coverage_out')),
        '    return {',
        '\n    }',
    );
    const serverKeys = [...returned.matchAll(/"([a-z_]+)":/g)].map(m => m[1]).sort();

    const interfaceBody = blockAfter(CALLS_TS, 'export interface TranscriptCoverage {');
    const clientKeys = [...interfaceBody.matchAll(/^ {4}(\w+):/gm)].map(m => m[1]).sort();

    it('finds both sides at all', () => {
        assert.ok(serverKeys.length >= 4, `only found ${serverKeys.length} server keys`);
        assert.ok(clientKeys.length >= 4, `only found ${clientKeys.length} client keys`);
    });

    it('names every field the same way on both sides', () => {
        assert.deepEqual(clientKeys, serverKeys);
    });

    it('rides on the session block under the agreed name', () => {
        assert.match(CALLS_PY, /"transcript_coverage": _coverage_out\(session\),/);
        assert.match(CALLS_TS, /transcript_coverage: TranscriptCoverage \| null;/);
    });

    it('is NOT gated on the transcript-text permission', () => {
        // Coverage is metadata — durations and a closed code. A supervisor who
        // may read no quote at all is still entitled to know the call was only
        // half captured; gating it would hide a data-quality problem from the
        // one person looking for it. So the line that serializes it carries no
        // `may_read_text` condition, unlike `evidence_text` two blocks above.
        const line = /^.*"transcript_coverage".*$/m.exec(CALLS_PY);
        assert.ok(line, 'transcript_coverage is not on the session payload');
        assert.doesNotMatch(line[0], /may_read_text/);
    });
});

// ─── the band on the screen ──────────────────────────────────────────────────

describe('the partial-transcript band', () => {
    it('is keyed on reason codes the router can actually send', () => {
        // The bug this test exists for: an early draft keyed the cause line on
        // `transcription_stopped`, which is a plausible name and not a value the
        // router emits, so the specific sentence would never have rendered and
        // nothing would have failed.
        const keys = [...blockAfter(WRAPUP_TSX, 'const TRUNCATION_CAUSE').matchAll(
            /^ {4}(\w+):/gm,
        )].map(m => m[1]);
        assert.ok(keys.length > 0, 'no TRUNCATION_CAUSE entries found');
        assert.deepEqual(
            keys.filter(k => !isCoverageReason(k)),
            [],
        );
        // `truncated` is the only reason that arrives with `truncated: true`, so
        // it is the one entry that must be there.
        assert.ok(keys.includes('truncated'));
    });

    it('renders above the proposals, not under them', () => {
        // The AE has to read it before they start ticking rows. Below the groups
        // it is a footnote on an approval they have already made.
        const band = WRAPUP_TSX.indexOf('{partialTranscript && (');
        const rows = WRAPUP_TSX.indexOf('groups.map(');
        assert.ok(band > 0 && rows > 0, 'band or group list not found');
        assert.ok(band < rows, 'the coverage band renders below the proposal groups');
    });

    it('renders on nothing but the server verdict', () => {
        // Twice in the file and no more: the derivation, and the single gate.
        // A `!partialTranscript` branch would be the reassuring "fully captured"
        // badge this band is specified not to have.
        const uses = [...WRAPUP_TSX.matchAll(/partialTranscript/g)].length;
        assert.equal(uses, 2);
        assert.doesNotMatch(WRAPUP_TSX, /!partialTranscript/);
    });

    it('says what it means for the AE, not just what happened', () => {
        const start = WRAPUP_TSX.indexOf('{partialTranscript && (');
        const band = WRAPUP_TSX.slice(start, WRAPUP_TSX.indexOf('{total === 0 ?', start));
        assert.match(band, /not read/);
        assert.match(band, /by hand/);
    });

    it('is amber and free of interpolation', () => {
        // Amber, never red: a stopped transcript is not an error and is usually
        // the AE's own deliberate choice. Full literal class strings, because
        // the Tailwind JIT purges what it cannot see.
        const tones = [...WRAPUP_TSX.matchAll(/const (COVERAGE_[A-Z_]+) =\s*'([^']+)'/g)];
        assert.ok(tones.length >= 3, `only found ${tones.length} coverage class strings`);
        for (const [, name, value] of tones) {
            assert.ok(!value.includes('${'), `${name} interpolates`);
            assert.ok(!/\bred-/.test(value), `${name} is red`);
        }
        assert.ok(tones.some(([, , value]) => /\bamber-/.test(value)));
    });
});
