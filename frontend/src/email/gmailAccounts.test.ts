import assert from 'node:assert/strict';
import { test } from 'node:test';
import { gmailThreadUrl, selectedGmailAccount } from './gmailAccounts.ts';

test('secondary mailbox selection must belong to the current enabled account list', () => {
    const current = {
        enabled: true,
        default_account: 'work@rubik-soft.com',
        accounts: [{ id: 'personal-1', email: 'personal@gmail.com', status: 'connected', is_default: false }],
    };
    assert.equal(selectedGmailAccount(current, 'personal-1')?.email, 'personal@gmail.com');
    assert.equal(selectedGmailAccount(current, 'another-workspace'), null);
    assert.equal(selectedGmailAccount({ ...current, enabled: false }, 'personal-1'), null);
    assert.equal(selectedGmailAccount({ ...current, accounts: [{ ...current.accounts[0], status: 'revoked' }] }, 'personal-1'), null);
});

test('the same Gmail thread ID opens the selected mailbox', () => {
    const work = gmailThreadUrl('work@rubik-soft.com', 'thread-1');
    const personal = gmailThreadUrl('personal@gmail.com', 'thread-1');
    assert.notEqual(work, personal);
    assert.equal(new URL(work).searchParams.get('authuser'), 'work@rubik-soft.com');
    assert.equal(new URL(personal).searchParams.get('authuser'), 'personal@gmail.com');
    assert.ok(personal.endsWith('#inbox/thread-1'));
});
