import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
    personalPriorityComposeUrl,
    personalPriorityThreadUrl,
    priorityComposeUrl,
    priorityThreadUrl,
} from './priorityGmail.ts';

const attrs = {
    gmail_mailbox: 'personal@gmail.com',
    gmail_account_id: 'a594ef01-f357-429d-9fd7-1d090b639ef0',
    personal_gmail: true,
};

test('personal Gmail thread opens only the matching mailbox and raw thread', () => {
    const url = personalPriorityThreadUrl(attrs, `${attrs.gmail_account_id}:same-thread`);
    assert.ok(url);
    assert.equal(new URL(url).searchParams.get('authuser'), 'personal@gmail.com');
    assert.ok(url.endsWith('#inbox/same-thread'));
    assert.equal(personalPriorityThreadUrl(attrs, 'same-thread'), null);
    assert.equal(personalPriorityThreadUrl(attrs, 'other-account:same-thread'), null);
});

test('mixed mailboxes and malformed IDs cannot trigger a reply or compose from a guessed account', () => {
    const mixed = { ...attrs, gmail_mailbox: 'personal@gmail.com, work@rubik-soft.com', gmail_account_id: null };
    assert.equal(personalPriorityThreadUrl(mixed, `${attrs.gmail_account_id}:same-thread`), null);
    assert.equal(personalPriorityComposeUrl(mixed, 'friend@example.com'), null);
    assert.equal(personalPriorityThreadUrl(attrs, `${attrs.gmail_account_id}:../../other`), null);
    assert.equal(personalPriorityComposeUrl(attrs, 'invalid recipient'), null);
});

test('single personal mailbox compose link chooses the same Google account', () => {
    const url = personalPriorityComposeUrl(attrs, 'friend@example.com');
    assert.ok(url);
    assert.equal(new URL(url).searchParams.get('authuser'), 'personal@gmail.com');
    assert.equal(new URL(url).searchParams.get('to'), 'friend@example.com');
});

test('work-only cards keep work actions when the run also used private Gmail context', () => {
    const workAttrs = { private_gmail_context: true, gmail_mailbox: 'work@example.com' };
    assert.equal(priorityThreadUrl(workAttrs, 'work-thread'),
        'https://mail.google.com/mail/?authuser=work%40example.com#inbox/work-thread');
    assert.equal(priorityComposeUrl(workAttrs, 'colleague@example.com'),
        'https://mail.google.com/mail/?authuser=work%40example.com&view=cm&fs=1&to=colleague%40example.com');
    assert.equal(priorityThreadUrl({ ...attrs, private_gmail_context: true }, `${attrs.gmail_account_id}:personal-thread`),
        'https://mail.google.com/mail/?authuser=personal%40gmail.com#inbox/personal-thread');
});

test('ordinary work Gmail cards use the cited work mailbox, not browser account zero', () => {
    const workAttrs = { gmail_mailbox: 'work@example.com', personal_gmail: false };
    assert.equal(priorityThreadUrl(workAttrs, 'work-thread'),
        'https://mail.google.com/mail/?authuser=work%40example.com#inbox/work-thread');
    const compose = priorityComposeUrl(workAttrs, 'client@example.com');
    assert.ok(compose);
    assert.equal(new URL(compose).searchParams.get('authuser'), 'work@example.com');
    assert.equal(new URL(compose).searchParams.get('to'), 'client@example.com');
});

test('ordinary work Gmail cards with no single mailbox have no action', () => {
    assert.equal(priorityThreadUrl({}, 'work-thread'), null);
    assert.equal(priorityThreadUrl({ gmail_mailbox: 'work@example.com, personal@gmail.com' }, 'work-thread'), null);
    assert.equal(priorityComposeUrl({}, 'client@example.com'), null);
    assert.equal(priorityComposeUrl({ gmail_mailbox: 'work@example.com, personal@gmail.com' }, 'client@example.com'), null);
});

test('private Gmail context without a cited mailbox cannot guess a mail action', () => {
    const tainted = { private_gmail_context: true };
    assert.equal(priorityThreadUrl(tainted, 'possibly-personal-thread'), null);
    assert.equal(priorityComposeUrl(tainted, 'someone@example.com'), null);
});
