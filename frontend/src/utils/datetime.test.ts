// Tests for the API-timestamp reader.
//
// Runs on Node's built-in test runner + TypeScript stripping — `datetime.ts`
// has no imports, so it loads without a bundler:
//
//   cd <repo>/frontend && node --test "src/utils/**/*.test.ts"
//
// Assertions are written to hold in ANY timezone the runner happens to sit in:
// instants are checked via `toISOString()` (always UTC), calendar dates via
// the local getters (always local, by design).
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    formatInstant,
    formatInstantDate,
    formatInstantTime,
    instantMs,
    parseInstant,
} from './datetime.ts';

describe('parseInstant', () => {
    it('reads a timezone-less backend timestamp as UTC, not as local', () => {
        // The regression: `new Date('2026-09-09T17:01:57')` is LOCAL per spec,
        // so the L10 footer showed the UTC wall clock as if it were yours.
        assert.equal(
            parseInstant('2026-09-09T17:01:57')!.toISOString(),
            '2026-09-09T17:01:57.000Z',
        );
    });

    it('keeps the microseconds Python emits', () => {
        assert.equal(
            parseInstant('2026-09-09T17:01:57.123456')!.toISOString(),
            '2026-09-09T17:01:57.123Z',
        );
    });

    it('leaves a timestamp that already names its zone alone', () => {
        for (const iso of [
            '2026-09-09T17:01:57Z',
            '2026-09-09T17:01:57.000Z',
            '2026-09-09T19:01:57+02:00',
            '2026-09-09T19:01:57+0200',
            '2026-09-09T10:01:57-07:00',
        ]) {
            assert.equal(
                parseInstant(iso)!.toISOString(),
                '2026-09-09T17:01:57.000Z',
                iso,
            );
        }
    });

    it('accepts a space between the date and the time', () => {
        assert.equal(
            parseInstant('2026-09-09 17:01:57')!.toISOString(),
            '2026-09-09T17:01:57.000Z',
        );
    });

    it('reads a bare calendar date as LOCAL midnight, so the day never slides', () => {
        // `new Date('2026-08-30')` is UTC midnight — which is Aug 29 for
        // everyone west of Greenwich. A due date has no timezone to shift.
        const d = parseInstant('2026-08-30')!;
        assert.equal(d.getFullYear(), 2026);
        assert.equal(d.getMonth(), 7); // August
        assert.equal(d.getDate(), 30);
        assert.equal(d.getHours(), 0);
    });

    it('is idempotent — a value it already fixed parses the same way twice', () => {
        const once = parseInstant('2026-09-09T17:01:57')!;
        assert.equal(parseInstant(once.toISOString())!.getTime(), once.getTime());
    });

    it('returns null for nothing to parse', () => {
        for (const bad of [null, undefined, '', '   ', 'not a date', 'TBD']) {
            assert.equal(parseInstant(bad), null, String(bad));
        }
    });
});

describe('formatInstant', () => {
    it('renders in the viewer timezone', () => {
        // Same instant, two viewers: the clock they read differs by their offset.
        const opts = { hour: 'numeric', minute: '2-digit', timeZone: 'America/Los_Angeles' } as const;
        assert.equal(formatInstant('2026-09-09T17:01:57', opts), '10:01 AM');
        assert.equal(
            formatInstant('2026-09-09T17:01:57', { ...opts, timeZone: 'America/Chicago' }),
            '12:01 PM',
        );
    });

    it('falls back instead of rendering "Invalid Date"', () => {
        assert.equal(formatInstant(null), '—');
        assert.equal(formatInstant('nonsense', {}, 'nonsense'), 'nonsense');
    });
});

describe('formatInstantTime', () => {
    it('names the zone on request, so a distributed meeting is unambiguous', () => {
        const withZone = formatInstantTime('2026-09-09T17:01:57', { withZone: true });
        const plain = formatInstantTime('2026-09-09T17:01:57');
        assert.ok(withZone.startsWith(plain), `${withZone} should extend ${plain}`);
        assert.ok(withZone.length > plain.length, 'a zone label should be appended');
    });

    it('drops the seconds a session-start line never needed', () => {
        assert.doesNotMatch(formatInstantTime('2026-09-09T17:01:57'), /:\d{2}:\d{2}/);
    });
});

describe('formatInstantDate', () => {
    it('renders exactly what a bare toLocaleDateString() would', () => {
        // The point of the helper: swapping a call site to it must change the
        // timezone the date is read in, and nothing about how it looks.
        const iso = '2026-09-09T17:01:57';
        assert.equal(
            formatInstantDate(iso),
            parseInstant(iso)!.toLocaleDateString(),
            'a 4-digit year, not the 2-digit one `dateStyle: "short"` gives',
        );
    });

    it('falls back rather than rendering an empty date', () => {
        assert.equal(formatInstantDate(null), '—');
        assert.equal(formatInstantDate(null, 'undated'), 'undated');
    });
});

describe('instantMs', () => {
    it('gives elapsed maths a correct baseline', () => {
        const start = instantMs('2026-09-09T17:01:57')!;
        const now = instantMs('2026-09-09T18:31:57')!;
        assert.equal((now - start) / 60_000, 90);
    });

    it('is null when there is no timestamp', () => {
        assert.equal(instantMs(null), null);
    });
});
