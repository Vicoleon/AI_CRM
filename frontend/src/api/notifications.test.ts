// Contract tests for the in-app notification feed.
//
//   cd <repo>/frontend && node --test "src/api/**/*.test.ts"
//
// WHAT BREAKS WITHOUT THESE
// ─────────────────────────
// The scheduled-task dots are derived, not stored: nothing marks a chat
// session "unread". The sidebar finds them by matching a notification's
// `kind` against a string and pulling a session id out of its `link`. Both
// strings are written in Python, by `rubik_core/scheduled_tasks/delivery.py`,
// and TypeScript cannot see either one.
//
// So the failure this file exists to catch has no symptom a compiler or a
// runtime can show you: someone renames the kind, or changes the link to
// `/chat/<id>`, every request still succeeds, every page still renders — and
// the dots simply never appear again. The feature degrades to exactly the
// silence it was built to fix, and the only way to notice is for a person to
// wonder why their scheduled result stopped being flagged.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

// See cockpit.test.ts: teach the runner Vite's extensionless resolution, and
// only for our own files.
const resolveExtensionless: ResolveHookSync = (specifier, context, nextResolve) => {
    const parent = context.parentURL ?? '';
    const relative = /^\.{1,2}\//.test(specifier);
    if (parent.includes('/frontend/src/') && relative && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
};

registerHooks({ resolve: resolveExtensionless });

const { sessionIdFromLink } = await import('./notifications.ts');

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');

describe('sessionIdFromLink', () => {
    it('pulls the session id out of a result link', () => {
        assert.equal(sessionIdFromLink('/chat?session=abc-123'), 'abc-123');
    });

    it('returns null for the paused-task notice, which has no conversation', () => {
        // `delivery.notify` falls back to this link when chat_session_id is
        // None. Treating it as a session would put a dot on a chat row that
        // does not exist, and nothing would ever clear it.
        assert.equal(sessionIdFromLink('/settings?tab=scheduled-tasks'), null);
    });

    it('returns null for a missing link', () => {
        assert.equal(sessionIdFromLink(null), null);
    });

    it('ignores a path that merely contains "chat"', () => {
        // Guards the naive `includes('/chat')`: a future /chats or
        // /help/chat-tips route must not be read as a conversation.
        assert.equal(sessionIdFromLink('/help/chat-tips?session=abc'), null);
    });

    it('returns null when the chat link carries no session param', () => {
        assert.equal(sessionIdFromLink('/chat?q=hello'), null);
    });
});

describe('the wire strings the dots depend on', () => {
    const delivery = read('rubik_core/scheduled_tasks/delivery.py');

    it('delivery.py still writes kind="scheduled_task_result"', () => {
        assert.match(delivery, /kind="scheduled_task_result"/);
    });

    it('delivery.py still links results as /chat?session=<id>', () => {
        assert.match(delivery, /link=f"\/chat\?session=\{chat_session_id\}"/);
        // Round-trip the real shape through the parser rather than trusting
        // that two regexes agree with each other.
        assert.equal(sessionIdFromLink('/chat?session=6f1e-uuid'), '6f1e-uuid');
    });

    it('Settings still offers the kind as mutable', () => {
        // A kind the bell can deliver but the reader cannot silence is a
        // notification they are stuck with.
        assert.match(
            read('frontend/src/pages/Settings.tsx'),
            /value: 'scheduled_task_result'/,
        );
    });

    it('the response schema keeps kind open, so a new producer cannot 500 the feed', () => {
        // A Literal here fails validation on the WHOLE list, not the odd row.
        assert.match(read('rubik_core/database/schemas.py'), /^    kind: str$/m);
    });
});
