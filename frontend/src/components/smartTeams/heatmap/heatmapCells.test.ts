// What a heatmap must never say.
//
//   cd <repo>/frontend && node --test "src/components/smartTeams/**/*.test.ts"
//
// Two of these would ship silently. A withheld score coalesced to zero renders
// as a real measurement of nothing; and `not_comparable` styled like `aligned`
// reports a match nobody took. Both look fine in a screenshot.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import type { HeatmapCell, PairHeatmapRow } from '../../../api/smartTeams';
import {
    OVERLAP_LEGEND,
    bandClass,
    bandFraction,
    bandLabel,
    groupByFramework,
    hasScore,
    listNames,
    overlapStyle,
    scoreLabel,
    summarise,
    tally,
} from './heatmapCells.ts';

const cell = (over: Partial<HeatmapCell> = {}): HeatmapCell => ({
    framework: 'disc',
    key: 'D',
    name: 'Dominance',
    band: 'High',
    band_ordinal: 2,
    band_count: 4,
    cross_person: true,
    ...over,
});

const row = (over: Partial<PairHeatmapRow> = {}): PairHeatmapRow => ({
    framework: 'disc',
    key: 'D',
    name: 'Dominance',
    reader: cell(),
    about: cell(),
    overlap: 'aligned',
    ...over,
});

describe('scores', () => {
    it('treats an absent score as absent, never as zero', () => {
        // The bug this exists for: `cell.score ?? 0` renders a number the
        // viewer was not allowed to see, as a measurement of nothing.
        assert.equal(hasScore(cell()), false);
        assert.equal(scoreLabel(cell()), null);
    });

    it('shows a genuine zero', () => {
        // And the mirror image: `!!cell.score` would hide a real 0.0.
        assert.equal(hasScore(cell({ score: 0 })), true);
        assert.equal(scoreLabel(cell({ score: 0 })), '0');
    });

    it('rounds, because an intensity is not precise to decimals', () => {
        assert.equal(scoreLabel(cell({ score: 71.4 })), '71');
    });

    it('is safe on a null cell', () => {
        assert.equal(hasScore(null), false);
        assert.equal(scoreLabel(undefined), null);
    });
});

describe('bands', () => {
    it('normalises against the cell’s own ladder, not a shared maximum', () => {
        // DISC's top rung is 3-of-4 and Big Five's is 4-of-5. Both are "top".
        assert.equal(bandFraction(cell({ band_ordinal: 3, band_count: 4 })), 1);
        assert.equal(bandFraction(cell({ band_ordinal: 4, band_count: 5 })), 1);
        assert.equal(bandFraction(cell({ band_ordinal: 0, band_count: 4 })), 0);
    });

    it('is null for a band this release cannot place', () => {
        // An older instrument version's band. Renders as unreadable, not as the
        // bottom of the scale.
        assert.equal(bandFraction(cell({ band_ordinal: null })), null);
        assert.match(bandClass(cell({ band_ordinal: null })), /repeating-linear-gradient/);
    });

    it('reads enum-shaped bands as English', () => {
        assert.equal(bandLabel('clearly_higher'), 'Clearly higher');
        assert.equal(bandLabel('Very high'), 'Very high');
        assert.equal(bandLabel(null), 'Not measured');
    });
});

describe('overlap styling', () => {
    it('never gives not_comparable the same treatment as aligned', () => {
        // The load-bearing one. These are opposite findings and a heatmap is
        // exactly the format that invites collapsing them into one grey.
        const unknown = overlapStyle('not_comparable');
        const same = overlapStyle('aligned');
        assert.notEqual(unknown.box, same.box);
        assert.match(unknown.box, /repeating-linear-gradient/);
        assert.doesNotMatch(same.box, /repeating-linear-gradient/);
    });

    it('says out loud that unknown is not agreement', () => {
        assert.match(overlapStyle('not_comparable').meaning, /not agreement/i);
    });

    it('has a style and a legend entry for every state', () => {
        for (const state of OVERLAP_LEGEND) {
            const style = overlapStyle(state);
            assert.ok(style.box && style.label && style.meaning, state);
        }
        assert.equal(OVERLAP_LEGEND.length, 4);
    });
});

describe('summarising a grid', () => {
    it('counts dimensions and never divides them into a compatibility score', () => {
        // `comparison_contracts.REJECT_COMPATIBILITY_SCORE` refuses this claim in
        // generated prose; computing it in the browser would be the same claim
        // with the screen routed around.
        const text = summarise([
            row({ overlap: 'aligned' }),
            row({ key: 'I', name: 'Influence', overlap: 'apart' }),
        ]);
        assert.doesNotMatch(text, /%|score|compatib|match rate/i);
        assert.match(text, /Influence/);
    });

    it('says plainly when nothing could be compared', () => {
        const text = summarise([row({ overlap: 'not_comparable' })]);
        assert.match(text, /nothing to compare/i);
        assert.doesNotMatch(text, /same|close/i);
    });

    it('reports how many were unreadable rather than dropping them', () => {
        const text = summarise([
            row({ overlap: 'aligned' }),
            row({ key: 'S', overlap: 'not_comparable' }),
        ]);
        assert.match(text, /1 could not be compared/);
    });

    it('tallies every state and counts what was readable', () => {
        const counts = tally([
            row({ overlap: 'aligned' }),
            row({ overlap: 'near' }),
            row({ overlap: 'apart' }),
            row({ overlap: 'not_comparable' }),
        ]);
        assert.deepEqual(counts, {
            aligned: 1, near: 1, apart: 1, not_comparable: 1, comparable: 3,
        });
    });
});

describe('helpers', () => {
    it('lists names as English', () => {
        assert.equal(listNames(['A']), 'A');
        assert.equal(listNames(['A', 'B']), 'A and B');
        assert.equal(listNames(['A', 'B', 'C']), 'A, B and C');
    });

    it('groups in server order rather than sorting', () => {
        // Two screens sorting differently would put one person's dimensions in
        // two orders. Server order is canonical.
        const groups = groupByFramework([
            { framework: 'big_five' },
            { framework: 'disc' },
            { framework: 'big_five' },
        ]);
        assert.deepEqual(groups.map(g => g.framework), ['big_five', 'disc']);
        assert.equal(groups[0].rows.length, 2);
        assert.equal(groups[0].label, 'Temperament');
    });
});
