import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { eosUiGate } from './eosAccess.ts';

describe('EOS UI access', () => {
    it('hides the EOS navigation and redirects direct EOS routes when the module is off or missing', () => {
        for (const user of [
            { modules: { eos: false } },
            { modules: {} },
            {},
        ]) {
            assert.deepEqual(eosUiGate({ token: 'signed-in', user, isLoading: false }), {
                showNav: false,
                route: 'help',
            });
        }
    });

    it('shows the EOS navigation and opens EOS routes only on an explicit enabled verdict', () => {
        assert.deepEqual(eosUiGate({ token: 'signed-in', user: { modules: { eos: true } }, isLoading: false }), {
            showNav: true,
            route: 'allow',
        });
        assert.deepEqual(eosUiGate({ token: 'signed-in', user: { modules: { eos: 'true' } }, isLoading: false }), {
            showNav: false,
            route: 'help',
        });
    });

    it('waits for a profile without showing EOS and sends signed-out visits to login', () => {
        assert.deepEqual(eosUiGate({ token: 'signed-in', user: null, isLoading: true }), {
            showNav: false,
            route: 'loading',
        });
        assert.deepEqual(eosUiGate({ token: 'signed-in', user: { modules: { eos: true } }, isLoading: true }), {
            showNav: false,
            route: 'loading',
        });
        assert.deepEqual(eosUiGate({ token: null, user: { modules: { eos: true } }, isLoading: false }), {
            showNav: false,
            route: 'login',
        });
    });
});
