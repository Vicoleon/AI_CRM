// The Daily Briefing's spotlight sentence.
//
//   cd <repo>/frontend && node --test src/components/whatsNew/spotlight.test.ts
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { spotlightText } from './spotlight.ts';

const item = (title: string) => ({ title });

describe('spotlightText', () => {
    it('is nothing without unseen updates', () => {
        assert.equal(spotlightText([]), null);
    });

    it('is the title alone for one update', () => {
        assert.equal(spotlightText([item('Run Cubie by voice')]), 'Run Cubie by voice');
    });

    it('names how many more', () => {
        assert.equal(
            spotlightText([
                item('Attach files and pictures in Ask Cubie'),
                item('Share a file with the team'),
                item('Rocks can be reordered'),
                item('The weekly scorecard email is steadier'),
            ]),
            'Attach files and pictures in Ask Cubie, plus 3 more',
        );
    });
});
