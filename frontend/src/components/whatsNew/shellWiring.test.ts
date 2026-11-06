// Contract tests for how What's New is wired into the app shell. The rules
// themselves are tested in logic / memory / store / returnTo / spotlight
// tests; these pin the call sites, which a compiler cannot check and no page
// crashes without: where the provider and the drawer are mounted, that the
// button and the deep links are company-wide (not behind the copilot pilot),
// the return trip after sign-in, and the drawer's place among the overlays.
//
//   cd <repo>/frontend && node --test src/components/whatsNew/shellWiring.test.ts
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..', '..');
const read = (relative: string): string => fs.readFileSync(path.join(FRONTEND, relative), 'utf8');
/** The source without its comments, for the "never does X" checks (the
 *  comments explain exactly what the code must not do). */
const code = (src: string): string => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

const app = read('src/App.tsx');
const layout = read('src/components/Layout.tsx');
const homefeed = read('src/pages/Homefeed.tsx');
const provider = read('src/components/whatsNew/WhatsNewProvider.tsx');
const drawer = read('src/components/whatsNew/WhatsNewDrawer.tsx');
const button = read('src/components/whatsNew/WhatsNewButton.tsx');
const links = read('src/pages/WhatsNewLink.tsx');

/** The JSX between `<Route path="…"` and the end of that element's line. */
const routeLine = (src: string, routePath: string): string => {
    const line = src.split('\n').find(l => l.includes(`<Route path="${routePath}"`));
    assert.ok(line, `no <Route path="${routePath}"> in App.tsx`);
    return line;
};

describe('mount points', () => {
    it('the provider wraps Layout, inside ProtectedRoute and NotificationsProvider', () => {
        // The page tours' TourProvider sits between NotificationsProvider and
        // this provider, so the drawer and the tease can tell a tour is running.
        assert.match(
            app,
            /<ProtectedRoute>[\s\S]*?<NotificationsProvider>[\s\S]*?(?:<TourProvider>\s*)?<WhatsNewProvider>\s*<Layout \/>\s*<\/WhatsNewProvider>\s*(?:<\/TourProvider>\s*)?<\/NotificationsProvider>\s*<\/ProtectedRoute>/,
        );
        assert.equal(app.match(/<WhatsNewProvider>/g)?.length, 1);
    });

    it('the drawer is mounted once, in Layout, beside the toast and confirm hosts', () => {
        assert.match(layout, /<ToastHost \/>\s*<ConfirmHost \/>[\s\S]{0,200}<WhatsNewDrawer \/>/);
        assert.equal(layout.match(/<WhatsNewDrawer \/>/g)?.length, 1);
        assert.doesNotMatch(app, /<WhatsNewDrawer/);
    });

    it('files that export a hook beside a component carry the react-refresh disable', () => {
        assert.match(provider, /^\/\* eslint-disable react-refresh\/only-export-components/);
    });

    it('the provider does not ride on the copilot-only notifications poll', () => {
        assert.doesNotMatch(code(provider), /useNotifications|NotificationsContext/);
        assert.match(provider, /hasFeature\('whats_new'\)/);
    });
});

describe('company-wide, not behind the copilot', () => {
    it('the footer button is gated on the feed, never on canAccessFullApp', () => {
        assert.match(layout, /\{showWhatsNewButton && \(\s*<div[^>]*>\s*<WhatsNewButton /);
        assert.doesNotMatch(layout, /\{canAccessFullApp && \(\s*<div[^>]*>\s*<WhatsNewButton /);
        assert.equal(layout.match(/<WhatsNewButton /g)?.length, 1);
        // Shown once the feed loads, empty or not: an empty drawer says so.
        assert.match(provider, /showButton = !off && feed !== null;/);
    });

    it('the deep link sits inside Layout, outside any feature gate', () => {
        const item = routeLine(app, 'whats-new/:itemId');
        assert.match(item, /element=\{<WhatsNewItemLink \/>\}/);
        assert.doesNotMatch(item, /FeatureRoute/);
        // The approvers' review link left with the approval flow.
        assert.doesNotMatch(app, /whats-new\/review|WhatsNewReviewLink/);
        // Child routes of the Layout route (relative paths, no leading slash).
        assert.ok(app.indexOf(item) > app.indexOf('<Route path="/" element={'));
        assert.ok(app.indexOf(item) < app.indexOf('</Routes>'));
    });

    it('a deep link counts a Slack arrival, opens the drawer and goes home', () => {
        assert.match(links, /if \(fromSlack\) void recordOpen\(itemId, 'slack'\)/);
        assert.match(links, /openWhatsNew\(\{ itemId, from: fromSlack \? 'slack' : undefined \}\)/);
        assert.match(links, /if \(!isEntryId\(itemId\)\)/);
        assert.equal(links.match(/navigate\('\/', \{ replace: true \}\)/g)?.length, 1);
        assert.equal(links.match(/clearReturnTo\(\)/g)?.length, 1);
    });
});

describe('the return trip after sign-in', () => {
    it('ProtectedRoute remembers the link it redirects from', () => {
        assert.match(app, /return token \? <>\{children\}<\/> : <RedirectToLogin \/>;/);
        assert.match(app, /rememberReturnTo\(pathname, search\)/);
    });

    it('the login route sends a signed-in person back to it, else home', () => {
        assert.match(app, /<Route path="\/login" element=\{token \? <Navigate to=\{peekReturnTo\(\) \?\? '\/'\} replace \/> : <Login \/>\} \/>/);
    });
});

describe('the shell around the drawer', () => {
    it('Layout and the tease share one live-L10 test', () => {
        assert.match(layout, /const isNavHidden = isLiveL10Path\(location\.pathname\);/);
        assert.match(provider, /isLiveL10Path\(pathname\)/);
    });

    it("the admin settings are an Administration section, not a Settings row", () => {
        // Moved to Admin (pages/admin/whatsNewSettings.test.ts pins the
        // Admin side and the old link's redirect).
        assert.doesNotMatch(code(layout), /settings\?tab=whats-new|onWhatsNewSettings/);
        assert.match(read('src/pages/adminSections.ts'), /\{ key: 'whatsnew', label: "What's New", Icon: Megaphone \}/);
    });

    it('the Daily Briefing has the spotlight bar and the header button', () => {
        assert.match(homefeed, /<WhatsNewHeaderButton \/>\s*<button\s+onClick=\{refresh\}/);
        assert.match(homefeed, /<SpotlightBar \/>/);
    });

    it('the button names the count of new updates', () => {
        assert.match(button, /whatsNewButtonLabel\(count\)/);
        assert.match(button, /aria-label=\{label\}/);
    });
});

describe('the drawer among the overlays', () => {
    it('is portalled, at z-150, and reduces motion on request', () => {
        assert.match(drawer, /createPortal\(/);
        assert.match(drawer, /<MotionConfig reducedMotion="user">/);
        assert.match(drawer, /fixed inset-y-0 right-0 z-\[150\]/);
        assert.match(drawer, /fixed inset-0 z-\[150\]/);
    });

    it('full mode joins the shared dialog stack; the peek never does', () => {
        assert.match(drawer, /pushDialog\(id\)/);
        assert.match(drawer, /popDialog\(id\)/);
        assert.match(drawer, /if \(!isTopmostDialog\(id\)\) return;/);
        assert.equal(drawer.match(/pushDialog\(/g)?.length, 1);
        assert.match(drawer, /role=\{isFull \? 'dialog' : 'complementary'\}/);
    });

    it('never calls toast(): results show in its own status line', () => {
        assert.doesNotMatch(code(drawer), /\btoast[.(]/);
        assert.match(drawer, /role="status"/);
    });

    it('is a reader only: no tabs, no approvals, an empty state', () => {
        assert.doesNotMatch(drawer, /ApprovalsPanel|role="tablist"|role="tab"/);
        assert.match(drawer, /No updates yet\./);
    });

    it('"Try it" counts the open and stays inside the app', () => {
        assert.match(drawer, /recordOpen\(item\.id, 'try_it'\)/);
        assert.match(drawer, /<Link\s+to=\{item\.path\}/);
        assert.doesNotMatch(drawer, /target="_blank"/);
    });
});
