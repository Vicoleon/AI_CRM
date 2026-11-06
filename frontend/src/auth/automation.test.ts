import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { automationStatus } from './automation.ts';

describe('automatic work follows the loaded server profile', () => {
    it('waits while the profile is unavailable', () => {
        assert.equal(automationStatus(null), 'loading');
        assert.equal(automationStatus(undefined), 'loading');
    });

    it('keeps a server pause visible rather than treating it as missing data', () => {
        assert.equal(automationStatus({ automations_enabled: false }), 'paused');
    });

    it('starts only after a loaded profile permits automatic work', () => {
        assert.equal(automationStatus({ automations_enabled: true }), 'enabled');
    });

    it('preserves compatibility with a loaded profile from an older server', () => {
        assert.equal(automationStatus({}), 'enabled');
    });
});
