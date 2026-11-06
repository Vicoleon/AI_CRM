// Page tours ("Walk me"): the content can't drift from the app, and the rules
// for which tour runs hold.
//
// Runs on Node's built-in test runner against Node's built-in TypeScript
// stripping, like the other *.test.ts files here:
//
//   cd <repo>/frontend && node --test "src/tours/tours.test.ts"
//
// What is pinned here:
//
//   * Every card's target is a `data-tour` some page carries, and every
//     `data-tour` a page carries is some card's target (no dead anchors).
//   * Every tour's route is one App.tsx declares, every help topic exists, and
//     every content file is gathered by the registry.
//   * Ids are permanent keys the server stores: unique, in the shape the
//     server accepts (backend/routers/tours.py), `new` tours dated.
//   * The copy stays short and says "Cubie". No card, tour title or file under
//     src/tours names an old product or company (the shared matcher in
//     src/testing/legacyBrand.ts, pinned to the repo's legacy-brand scan).
//   * Pages Phase 1 changed for everyone announce it with a `new` tour.
//   * The live L10 never starts a tour by itself.
//   * Which tour runs: intro first and it settles the page's new tours; new
//     tours run together, oldest first; replay; a finished tour stays finished.
//   * Where the card goes.
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';
import { HELP_TOPICS } from '../pages/help/helpTopics.ts';
import { everyFile, oldNames, oldNamesUnder } from '../testing/legacyBrand.ts';
import { REPLAY_HINT } from './content/account.ts';
import {
    matchQuery,
    matchRoute,
    pickAutoRun,
    replayRun,
    settle,
    tourAudience,
    toursForPage,
    visibleTo,
    withFirstTourHint,
} from './logic.ts';
import { DOCK_BELOW, GAP, MARGIN, placeCard, spotlightBox } from './position.ts';
import { ALL_TOURS } from './registry.ts';
import type { PageTour } from './types.ts';

const SRC = fileURLToPath(new URL('..', import.meta.url));
const TOURS_DIR = fileURLToPath(new URL('.', import.meta.url));

/** The first day of the Phase 1 rebrand: a `new` tour for a Phase 1 change is dated on or after it. */
const PHASE_1 = '2026-10-27';

/** The app's source files under `dir`: .ts and .tsx, tests left out. */
const sourceFiles = (dir: string): string[] =>
    everyFile(dir).filter(file => /\.tsx?$/.test(file) && !/\.test\.tsx?$/.test(file));

/** Every `data-tour` value written in the app (outside the tour engine):
 *  a literal attribute, or the value a `data-tour={...}` expression yields. */
function anchorsInApp(): Map<string, string[]> {
    const found = new Map<string, string[]>();
    const add = (name: string, file: string) => found.set(name, [...(found.get(name) ?? []), file]);
    for (const file of sourceFiles(SRC)) {
        if (file.startsWith(TOURS_DIR)) continue;
        const text = readFileSync(file, 'utf8');
        for (const m of text.matchAll(/data-tour="([^"]+)"/g)) add(m[1], file);
        // An expression: `{'x'}`, or the value side of a ternary
        // (`{i === 0 ? 'x' : undefined}`), never the strings it compares with.
        for (const m of text.matchAll(/data-tour=\{([^}]*)\}/g)) {
            const literal = m[1].match(/^\s*['"`]([a-z0-9-]+)['"`]\s*$/);
            if (literal) add(literal[1], file);
            for (const q of m[1].matchAll(/\?\s*['"`]([a-z0-9-]+)['"`]/g)) add(q[1], file);
        }
    }
    return found;
}

/** The route paths App.tsx declares, without their leading slash. */
function declaredPaths(): Set<string> {
    const app = readFileSync(join(SRC, 'App.tsx'), 'utf8');
    return new Set([...app.matchAll(/<Route\s+path="([^"]+)"/g)].map(m => m[1].replace(/^\//, '')));
}

const ALL_STEPS = ALL_TOURS.flatMap(t => t.steps.map(s => ({ tour: t, step: s })));

describe('tour content matches the app', () => {
    it('gathers every content file in the registry', () => {
        const registry = readFileSync(join(TOURS_DIR, 'registry.ts'), 'utf8');
        for (const name of readdirSync(join(TOURS_DIR, 'content'))) {
            if (!name.endsWith('.ts')) continue;
            assert.ok(
                registry.includes(`'./content/${name}'`),
                `content/${name} is not imported in registry.ts, so its tours never run`,
            );
        }
    });

    it('points every card at a data-tour some page carries', () => {
        const anchors = anchorsInApp();
        const missing = ALL_STEPS.filter(({ step }) => step.target && !anchors.has(step.target)).map(
            ({ tour, step }) => `${tour.id} / ${step.id} → data-tour="${step.target}"`,
        );
        assert.deepEqual(missing, [], 'cards whose target no page carries');
    });

    it('leaves no data-tour that no card uses', () => {
        const used = new Set(ALL_STEPS.map(({ step }) => step.target).filter(Boolean));
        const dead = [...anchorsInApp().entries()]
            .filter(([name]) => !used.has(name))
            .map(([name, files]) => `${name} (${files.map(f => f.replace(SRC, 'src/')).join(', ')})`);
        assert.deepEqual(dead, [], 'data-tour attributes no tour points at');
    });

    it('uses only routes App.tsx declares', () => {
        const paths = declaredPaths();
        const ok = (route: string): boolean => {
            const r = route.replace(/^\//, '');
            if (r === '') return true; // the index route
            if (paths.has(r)) return true;
            // A nested route: "eos" + "rocks", "smart-teams" + "guide/:email".
            const segs = r.split('/');
            for (let i = 1; i < segs.length; i++) {
                if (paths.has(segs.slice(0, i).join('/')) && paths.has(segs.slice(i).join('/'))) return true;
            }
            return false;
        };
        const bad = ALL_TOURS.flatMap(t => t.routes.filter(r => !ok(r)).map(r => `${t.id}: ${r}`));
        assert.deepEqual(bad, []);
    });

    it('links only to help topics that exist', () => {
        const ids = new Set(HELP_TOPICS.map(t => t.id));
        const bad = ALL_TOURS.filter(t => t.helpTopic && !ids.has(t.helpTopic)).map(t => `${t.id}: ${t.helpTopic}`);
        assert.deepEqual(bad, []);
    });
});

describe('tour ids are permanent keys', () => {
    it('are unique and in the shape the server stores', () => {
        const seen = new Set<string>();
        for (const t of ALL_TOURS) {
            assert.match(t.id, /^[a-z0-9][a-z0-9._-]{0,119}$/, t.id);
            assert.ok(!seen.has(t.id), `duplicate tour id ${t.id}`);
            seen.add(t.id);
        }
    });

    it('name intro and new tours after their kind, and date the new ones', () => {
        for (const t of ALL_TOURS) {
            if (t.kind === 'intro') {
                assert.ok(t.id.includes('intro'), `${t.id}: an intro's id says "intro"`);
                assert.equal(t.since, undefined, `${t.id}: only new tours carry a date`);
            } else {
                assert.ok(t.id.includes('.new.'), `${t.id}: a new tour's id is <page>.new.<feature>`);
                assert.match(t.since ?? '', /^\d{4}-\d{2}-\d{2}$/, `${t.id}: since YYYY-MM-DD`);
                assert.ok(!Number.isNaN(Date.parse(t.since!)), `${t.id}: a real date`);
            }
            assert.ok(t.routes.length > 0, `${t.id}: at least one route`);
        }
    });

    it('give each step a unique id inside its tour', () => {
        for (const t of ALL_TOURS) {
            assert.ok(t.steps.length > 0, `${t.id}: no steps`);
            const ids = t.steps.map(s => s.id);
            assert.equal(new Set(ids).size, ids.length, `${t.id}: duplicate step id`);
        }
    });
});

describe('tour copy', () => {
    const words = (s: string) => s.trim().split(/\s+/).length;

    it('keeps cards short', () => {
        const long = ALL_STEPS.filter(({ step }) => words(step.title) > 8 || words(step.body) > 45).map(
            ({ tour, step }) => `${tour.id} / ${step.id} (${words(step.title)} + ${words(step.body)} words)`,
        );
        assert.deepEqual(long, []);
    });

    it('keeps intros to a handful of cards', () => {
        // Home has one extra card that exists only inside a support session.
        const bad = ALL_TOURS.filter(t => t.kind === 'intro' && t.steps.length > (t.id === 'home.intro' ? 8 : 7)).map(t => `${t.id}: ${t.steps.length}`);
        assert.deepEqual(bad, []);
    });

    it('says Cubie and avoids engineering words', () => {
        const engineering = /\b(RAG|API|endpoint|feature flag|LLM|component)\b/;
        const copy = [
            ...ALL_TOURS.map(t => ({ where: t.id, text: t.title })),
            ...ALL_STEPS.map(({ tour, step }) => ({ where: `${tour.id} / ${step.id}`, text: `${step.title} ${step.body}` })),
            { where: 'REPLAY_HINT', text: `${REPLAY_HINT.title} ${REPLAY_HINT.body}` },
        ];
        const bad = copy.flatMap(({ where, text }) => {
            const words = [text.match(engineering)?.[0], ...oldNames(text)].filter(Boolean);
            return words.length > 0 ? [`${where}: ${words.join(', ')}`] : [];
        });
        assert.deepEqual(bad, []);
        assert.ok(
            ALL_STEPS.some(({ step }) => /\bCubie\b/.test(`${step.title} ${step.body}`)),
            'no card calls the product Cubie',
        );
    });

    it('leaves no old product or company name in any tour file or file name', () => {
        assert.deepEqual(oldNamesUnder(TOURS_DIR), []);
    });
});

describe('pages Phase 1 changed for everyone announce it', () => {
    const newSince = (t: PageTour) => t.kind === 'new' && (t.since ?? '') >= PHASE_1;

    it('has a new home-page tour for the What’s new and Daily Briefing buttons everyone now sees', () => {
        const home = ALL_TOURS.filter(t => t.routes.includes('/') && newSince(t)).map(t => t.id);
        assert.ok(home.length > 0, `no home.new.* tour dated ${PHASE_1} or later`);
    });

    it('keeps the announced proposal-template tour and adds a new one for the reworked picker', () => {
        const announced = ALL_TOURS.find(t => t.id === 'proposal.new.marketing-templates');
        assert.equal(announced?.since, '2026-10-05', 'an announced tour keeps its id and date');
        const fresh = ALL_TOURS.filter(
            t => t.id.startsWith('proposal.new.') && t.id !== 'proposal.new.marketing-templates' && newSince(t),
        );
        assert.ok(fresh.length > 0, `no proposal.new.* tour dated ${PHASE_1} or later for the reworked template picker`);
    });

    it('has a new Insights tour, pointing at the panel heading, for Systems renamed Platform', () => {
        const fresh = ALL_TOURS.filter(t => t.id.startsWith('insights.new.') && newSince(t));
        assert.ok(
            fresh.some(t => t.steps.some(s => s.target === 'insights-platform-group')),
            `no insights.new.* tour dated ${PHASE_1} or later on data-tour="insights-platform-group"`,
        );
    });

    it('announces the support line at the foot of Help to admins, and only to admins', () => {
        const tour = ALL_TOURS.find(t => t.id === 'help.new.support-line');
        assert.ok(tour && newSince(tour), `no help.new.support-line tour dated ${PHASE_1} or later`);
        assert.equal(tour.requires, 'admin');
        assert.ok(tour.steps.every(s => s.target === 'help-support-line'));
        const intro = ALL_TOURS.find(t => t.id === 'help.intro');
        assert.ok(
            intro?.steps.some(s => s.target === 'help-support-line' && s.requires === 'admin'),
            'the Help intro covers the support line for admins',
        );

        const admin = tourAudience({ page_tours: true }, ['admin']);
        const member = tourAudience({ page_tours: true }, ['user']);
        const onHelp = (has: typeof admin) => toursForPage(ALL_TOURS, '/help', '', has);
        assert.ok(onHelp(admin).some(t => t.id === 'help.new.support-line'));
        assert.ok(!onHelp(member).some(t => t.id === 'help.new.support-line'));
        const memberIntro = onHelp(member).find(t => t.id === 'help.intro');
        assert.ok(memberIntro && !memberIntro.steps.some(s => s.target === 'help-support-line'));
    });

    it('announces read-only workspace domains and modules in Administration', () => {
        const tour = ALL_TOURS.find(t => t.id === 'admin.new.workspace-modules');
        assert.ok(tour && newSince(tour));
        assert.deepEqual(tour.query, { tab: 'workspace' });
        assert.ok(tour.steps.some(s => s.target === 'admin-workspace-modules'));
        const intro = ALL_TOURS.find(t => t.id === 'admin.intro');
        assert.ok(intro?.steps.some(s => s.id === 'workspace-access'));
    });

    it('explains EOS-only scorecard controls on the Loose Ends Team tab without interrupting an L10', () => {
        const tour = ALL_TOURS.find(t => t.id === 'loose-ends-team.new.eos-scorecard');
        assert.ok(tour && newSince(tour));
        assert.deepEqual(tour.query, { view: 'team' });
        assert.equal(tour.autoStart, false);
        assert.ok(tour.steps.some(s => s.target === 'loose-ends-team-scorecard'));
        const intro = ALL_TOURS.find(t => t.id === 'loose-ends-team.intro');
        assert.ok(intro?.steps.some(s => s.id === 'scorecard'));
    });
});

describe('tours that must never start by themselves', () => {
    it('includes the live L10 meeting', () => {
        const l10 = ALL_TOURS.filter(t => t.routes.includes('/eos/meetings/:meetingId'));
        for (const t of l10) assert.equal(t.autoStart, false, `${t.id} would pop up in the middle of an L10`);
    });
});

// ── which tour runs ─────────────────────────────────────────────────────────

const step = (id: string, extra: Partial<PageTour['steps'][number]> = {}) => ({ id, title: id, body: id, ...extra });

const INTRO: PageTour = { id: 'rocks.intro', kind: 'intro', routes: ['/eos/rocks'], title: 'Rocks', steps: [step('a'), step('b')] };
const NEW_A: PageTour = {
    id: 'rocks.new.carry-over',
    kind: 'new',
    routes: ['/eos/rocks'],
    title: 'Carry over',
    since: '2026-10-09',
    steps: [step('c')],
};
const NEW_B: PageTour = {
    id: 'rocks.new.all-teams',
    kind: 'new',
    routes: ['/eos/rocks'],
    title: 'All teams',
    since: '2026-10-13',
    requires: 'eos_rocks_all_teams',
    steps: [step('d')],
};
const OTHER: PageTour = { id: 'chat.intro', kind: 'intro', routes: ['/chat'], title: 'Ask Cubie', steps: [step('x')] };
const ALL = [NEW_B, INTRO, OTHER, NEW_A];
const everything = () => true;
const nothing = () => false;

describe('who a tour is for', () => {
    it('reads the server’s feature verdicts, and the admin role for admin', () => {
        const has = tourAudience({ todos: true, smart_teams: false }, ['admin']);
        assert.equal(has('todos'), true);
        assert.equal(has('smart_teams'), false);
        assert.equal(has('copilot'), false, 'a verdict the server did not send is a no');
        assert.equal(has('admin'), true);
        assert.equal(tourAudience({ todos: true }, ['user'])('admin'), false);
        assert.equal(tourAudience(undefined, undefined)('admin'), false);
        assert.equal(tourAudience(undefined, undefined)('todos'), false);
    });

    it('shows the platform support banner tour only during an active support session', () => {
        const home = ALL_TOURS.find(t => t.id === 'home.new.platform-support');
        assert.ok(home);
        assert.equal(home.steps[0]?.target, 'platform-support-banner');
        const features = { copilot: true, page_tours: true };
        const ordinaryAdmin = tourAudience(features, ['admin'], false);
        const support = tourAudience(features, ['admin'], true);
        assert.equal(toursForPage(ALL_TOURS, '/', '', ordinaryAdmin).some(t => t.id === home.id), false);
        assert.equal(toursForPage(ALL_TOURS, '/', '', support).some(t => t.id === home.id), true);
        assert.equal(toursForPage(ALL_TOURS, '/', '', ordinaryAdmin)
            .find(t => t.id === 'home.intro')?.steps.some(s => s.target === 'platform-support-banner'), false);
        assert.equal(toursForPage(ALL_TOURS, '/', '', support)
            .find(t => t.id === 'home.intro')?.steps.some(s => s.target === 'platform-support-banner'), true);
    });
});

describe('routes', () => {
    it('match segment by segment, params match one segment', () => {
        assert.ok(matchRoute('/eos/rocks', '/eos/rocks'));
        assert.ok(matchRoute('/eos/rocks', '/eos/rocks/'));
        assert.ok(!matchRoute('/eos/rocks', '/eos/rocks/12'));
        assert.ok(matchRoute('/customers/:companyId/xray', '/customers/123/xray'));
        assert.ok(!matchRoute('/customers/:companyId/xray', '/customers//xray'));
        assert.ok(matchRoute('/', '/'));
        assert.ok(!matchRoute('/', '/chat'));
    });

    it('match a query when the tour names one', () => {
        assert.ok(matchQuery(undefined, '?tab=files'));
        assert.ok(matchQuery({ tab: 'files' }, '?x=1&tab=files'));
        assert.ok(!matchQuery({ tab: 'files' }, '?tab=documents'));
    });

    it('give way on an unlessQuery URL only to a tour the person can see', () => {
        const mine: PageTour = { ...INTRO, unlessQuery: { view: 'team' } };
        const team: PageTour = { id: 'rocks.intro.team', kind: 'intro', routes: ['/eos/rocks'], query: { view: 'team' }, requires: 'loop_closure_team', title: 'Team', steps: [step('t')] };
        const ids = (search: string, has: (k: string) => boolean) => toursForPage([mine, team], '/eos/rocks', search, has).map(t => t.id);
        assert.deepEqual(ids('?view=team', everything), ['rocks.intro.team']);
        assert.deepEqual(ids('', everything), ['rocks.intro']);
        // Without the Team tab, a ?view=team link shows their own list: keep its tour.
        assert.deepEqual(ids('?view=team', k => k !== 'loop_closure_team'), ['rocks.intro']);
    });

    it('leave out tours and steps the person cannot see', () => {
        const gated: PageTour = { ...INTRO, steps: [step('a'), step('b', { requires: 'todos' })] };
        assert.deepEqual(
            toursForPage([gated, NEW_B], '/eos/rocks', '', nothing).map(t => [t.id, t.steps.map(s => s.id)]),
            [['rocks.intro', ['a']]],
        );
        const allGated: PageTour = { ...INTRO, steps: [step('a', { requires: 'todos' })] };
        assert.deepEqual(toursForPage([allGated], '/eos/rocks', '', nothing), []);
    });
});

describe('the tour that starts by itself', () => {
    const page = toursForPage(ALL, '/eos/rocks', '', everything);

    it('is the intro on a first visit, and it settles the page’s new tours', () => {
        const run = pickAutoRun(page, {}, ALL);
        assert.equal(run?.kind, 'intro');
        assert.deepEqual(run?.steps.map(s => s.id), ['a', 'b']);
        assert.deepEqual([...(run?.settles ?? [])].sort(), ['rocks.intro', 'rocks.new.all-teams', 'rocks.new.carry-over']);
    });

    it('leaves a new tour the person cannot see yet for the day they get the feature', () => {
        const has = (key: string) => key !== 'eos_rocks_all_teams';
        const visible = toursForPage(ALL, '/eos/rocks', '', has);
        const run = pickAutoRun(visible, {}, ALL.filter(t => visibleTo(t, has)));
        assert.ok(!run?.settles.includes('rocks.new.all-teams'));
        assert.ok(run?.settles.includes('rocks.new.carry-over'));
        // Given the feature later, the card is news to them.
        const later = toursForPage(ALL, '/eos/rocks', '', everything);
        const seen = settle({}, run!.settles, 'completed');
        assert.deepEqual(pickAutoRun(later, seen, ALL)?.settles, ['rocks.new.all-teams']);
    });

    it('is every unseen new tour as one run, oldest first, once the intro is seen', () => {
        const run = pickAutoRun(page, { 'rocks.intro': 'completed' }, ALL);
        assert.equal(run?.kind, 'new');
        assert.deepEqual(run?.settles, ['rocks.new.carry-over', 'rocks.new.all-teams']);
        assert.deepEqual(run?.steps.map(s => s.id), ['c', 'd']);
        assert.equal(run?.title, 'What’s new on this page');
    });

    it('is nothing once everything is seen, or on a page without tours', () => {
        const seen = { 'rocks.intro': 'skipped', 'rocks.new.carry-over': 'completed', 'rocks.new.all-teams': 'completed' } as const;
        assert.equal(pickAutoRun(page, seen, ALL), null);
        assert.equal(pickAutoRun(toursForPage(ALL, '/settings', '', everything), {}, ALL), null);
    });

    it('never starts a tour marked autoStart: false, and holds back its page’s new tours', () => {
        const live: PageTour = { ...INTRO, autoStart: false };
        const tours = [live, NEW_A];
        assert.equal(pickAutoRun(toursForPage(tours, '/eos/rocks', '', everything), {}, tours), null);
    });

    it('runs a page’s own intro before a tab’s intro', () => {
        const tab: PageTour = { id: 'rocks.intro.archive', kind: 'intro', routes: ['/eos/rocks'], query: { tab: 'archive' }, title: 'Archive', steps: [step('z')] };
        const tours = [tab, INTRO];
        const onTab = toursForPage(tours, '/eos/rocks', '?tab=archive', everything);
        assert.equal(pickAutoRun(onTab, {}, tours)?.key, 'rocks.intro');
        assert.equal(pickAutoRun(onTab, { 'rocks.intro': 'completed' }, tours)?.key, 'rocks.intro.archive');
        // Replay on the tab plays the tab's own intro.
        assert.equal(replayRun(onTab, tours)?.key, 'rocks.intro.archive');
    });
});

describe('replay and settling', () => {
    it('replays the intro whatever was seen, else the new tours', () => {
        const page = toursForPage(ALL, '/eos/rocks', '', everything);
        assert.equal(replayRun(page, ALL)?.key, 'rocks.intro');
        assert.equal(replayRun([NEW_A], [NEW_A])?.key, 'rocks.new.carry-over');
        assert.equal(replayRun([], []), null);
    });

    it('ends only the very first tour with where to replay tours', () => {
        const page = toursForPage(ALL, '/eos/rocks', '', everything);
        const first = withFirstTourHint(pickAutoRun(page, {}, ALL), {}, REPLAY_HINT);
        assert.deepEqual(first?.steps.map(s => s.id), ['a', 'b', 'replay-hint']);
        const seen = { 'chat.intro': 'completed' } as const;
        const later = withFirstTourHint(pickAutoRun(page, seen, ALL), seen, REPLAY_HINT);
        assert.deepEqual(later?.steps.map(s => s.id), ['a', 'b']);
        assert.equal(withFirstTourHint(null, {}, REPLAY_HINT), null);
    });

    it('points the replay hint at a data-tour the app carries', () => {
        assert.ok(REPLAY_HINT.target && anchorsInApp().has(REPLAY_HINT.target));
    });

    it('keeps a finished tour finished when a replay of it is skipped', () => {
        const next = settle({ 'rocks.intro': 'completed' }, ['rocks.intro', 'chat.intro'], 'skipped');
        assert.deepEqual(next, { 'rocks.intro': 'completed', 'chat.intro': 'skipped' });
        assert.deepEqual(settle({ 'chat.intro': 'skipped' }, ['chat.intro'], 'completed'), { 'chat.intro': 'completed' });
    });
});

// ── where the card goes ─────────────────────────────────────────────────────

describe('card placement', () => {
    const vp = { width: 1440, height: 900 };
    const card = { width: 360, height: 200 };

    it('sits below the target by default, centred on it', () => {
        const pos = placeCard({ top: 100, left: 600, width: 200, height: 40 }, card, vp);
        assert.equal(pos.placement, 'bottom');
        assert.equal(pos.top, 140 + GAP);
        assert.equal(pos.left, 700 - 180);
    });

    it('flips to the other side when there is no room', () => {
        const pos = placeCard({ top: 820, left: 600, width: 200, height: 40 }, card, vp, 'bottom');
        assert.equal(pos.placement, 'top');
        assert.equal(pos.top, 820 - GAP - 200);
    });

    it('honours a preferred side and stays on screen', () => {
        const pos = placeCard({ top: 10, left: 20, width: 200, height: 40 }, card, vp, 'right');
        assert.equal(pos.placement, 'right');
        assert.ok(pos.top >= MARGIN);
    });

    it('sits over a target too big for any side', () => {
        const pos = placeCard({ top: 0, left: 0, width: 1440, height: 900 }, card, vp);
        assert.equal(pos.placement, 'over');
        assert.equal(pos.left, 1440 - 360 - MARGIN);
    });

    it('sits in the middle with no target', () => {
        assert.deepEqual(placeCard(null, card, vp), { top: 350, left: 540, placement: 'center' });
    });

    it('keeps a phone card on the edge chosen for it while the page scrolls', () => {
        const phone = { width: DOCK_BELOW - 1, height: 800 };
        assert.equal(placeCard({ top: 700, left: 10, width: 100, height: 40 }, card, phone, undefined, 'dock-bottom').placement, 'dock-bottom');
    });

    it('docks on a phone, away from the target', () => {
        const phone = { width: DOCK_BELOW - 1, height: 800 };
        assert.equal(placeCard({ top: 100, left: 10, width: 100, height: 40 }, card, phone).placement, 'dock-bottom');
        assert.equal(placeCard({ top: 700, left: 10, width: 100, height: 40 }, card, phone).placement, 'dock-top');
    });

    it('keeps the spotlight on screen, and drops it for an off-screen target', () => {
        assert.deepEqual(spotlightBox({ top: 2, left: 2, width: 10, height: 10 }, vp), { top: 0, left: 0, width: 18, height: 18 });
        assert.equal(spotlightBox({ top: 1000, left: 0, width: 10, height: 10 }, vp), null);
    });
});
