// Contract tests for how the bell, the toaster and the meeting-prep page are
// wired to the notification feed. The rules themselves are tested in
// rules.test.ts / cardStore.test.ts; these pin the call sites, which a
// compiler cannot check and no page crashes without.
//
//   cd <repo>/frontend && node --test "src/components/notifications/**/*.test.ts"
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');

const provider = read('frontend/src/context/NotificationsContext.tsx');

describe('who the feed is for', () => {
    it('the notifications API is behind require_full_app_access (the premise of the two gates below)', () => {
        const main = read('backend/main.py');
        // The copilot gate comes first; later dependencies (bind_usage_user, which
        // stamps the caller on AI calls) only run for someone it let through.
        assert.match(
            main,
            /core = APIRouter\(\s*prefix="\/api",\s*dependencies=\[Depends\(require_full_app_access\)(?:,\s*Depends\(\w+\))*\],?\s*\)/,
        );
        assert.match(main, /core\.include_router\(notifications\.router\)/);
    });

    it('the provider polls only for someone with the full app (anyone else would get a 403 every minute)', () => {
        assert.match(provider, /const token = auth\.canAccessFullApp \? auth\.token : null;/);
    });

    it('the bell renders only for someone with the full app (anyone else would see a bell that is always empty)', () => {
        const layout = read('frontend/src/components/Layout.tsx');
        assert.match(layout, /\{canAccessFullApp && \(\s*<div[^>]*>\s*<NotificationBell /);
        assert.equal(layout.match(/<NotificationBell /g)?.length, 1);
    });
});

describe('"Mark all as read" marks the rows it showed, by id', () => {
    it('the client has no server-wide read-all to call', () => {
        // /read-all marks every unread row for the person, including one that
        // landed after the last poll — a "meeting prep ready" they never saw.
        assert.doesNotMatch(read('frontend/src/api/notifications.ts'), /read-all/);
        assert.doesNotMatch(provider, /markAllNotificationsRead|read-all/);
    });

    it('the bell passes the ids it listed', () => {
        assert.match(
            read('frontend/src/components/notifications/NotificationBell.tsx'),
            /markAllRead\(unread\.map\(n => n\.id\)\)/,
        );
    });
});

describe("the toaster's cards belong to the person signed in", () => {
    const toaster = read('frontend/src/components/notifications/NotificationToaster.tsx');

    it('claims the store for the signed-in person, and clears it on unmount (sign-out)', () => {
        assert.match(toaster, /cards\.claim\(owner\)/);
        assert.match(toaster, /useEffect\(\(\) => \(\) => cards\.clear\(\), \[\]\)/);
    });
});

describe('the meeting-prep page catches up with its own notifications', () => {
    const page = read('frontend/src/pages/MeetingPrep.tsx');

    it('watches unread rows for its meeting, and marks the ones it handles read', () => {
        assert.match(page, /unackedPrepNotices\(unread, eventId\)/);
        assert.match(page, /prepNoticeStep\(/);
        assert.match(page, /markRead\(/);
    });

    it('drops a build the server no longer reports, and never switches back to an older version', () => {
        assert.match(page, /forgetBuilds\(staleBuilds\(followed, id, res\.data\.pending_run\)\)/);
        assert.match(page, /readyAction\(/);
    });
});
