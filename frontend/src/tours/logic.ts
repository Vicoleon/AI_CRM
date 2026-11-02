// Which tour runs on a page, and what finishing it records. Pure functions, no
// React and no DOM, so Node's test runner covers them (tours.test.ts).
//
// The rules, in one place:
//
//   * A page's tours are the ones whose route matches the URL, whose query (if
//     any) matches the query string and whose `unlessQuery` does not, and
//     that the person may see (the tour's
//     `requires`, then each step's). A tour left with no steps is not a tour.
//   * First visit: the page's intro runs. Finishing or skipping it also marks
//     the page's `new` tours this person can see as seen: the intro covers the
//     page as it is today. One behind a feature they lack waits for the day
//     they get it.
//   * Later visits: every `new` tour the person has not seen runs as ONE tour,
//     oldest feature first, so two features shipped the same week are one
//     interruption, not two.
//   * `autoStart: false` never starts by itself; replay still offers it.
//   * Replay ignores what was seen: the intro (the most specific one when a
//     tab has its own), else the page's `new` tours.
import type { PageTour, TourStep } from './types.ts';

export type SeenStatus = 'completed' | 'skipped';
export type SeenMap = Readonly<Record<string, SeenStatus>>;

/** Does a react-router path (`/customers/:companyId/xray`) match a pathname?
 *  Segment by segment: a `:param` matches any one non-empty segment, a
 *  trailing slash is ignored, and nothing else is a wildcard. */
export function matchRoute(pattern: string, pathname: string): boolean {
    const split = (p: string) => p.split('/').filter(Boolean);
    const want = split(pattern);
    const got = split(pathname);
    if (want.length !== got.length) return false;
    return want.every((seg, i) => (seg.startsWith(':') ? got[i].length > 0 : seg === decodeSegment(got[i])));
}

function decodeSegment(seg: string): string {
    try {
        return decodeURIComponent(seg);
    } catch {
        return seg;
    }
}

/** Every value a tour's `query` names is present in the URL's query string. */
export function matchQuery(query: Record<string, string> | undefined, search: string): boolean {
    if (!query) return true;
    const params = new URLSearchParams(search);
    return Object.entries(query).every(([k, v]) => params.get(k) === v);
}

/** The person's feature, admin-role or live platform-support verdict. */
export type HasFeature = (key: NonNullable<PageTour['requires']>) => boolean;

/** The verdicts a tour reads for one person. Nothing missing counts as yes. */
export function tourAudience(
    features: Readonly<Record<string, boolean>> | undefined,
    roles: readonly string[] | undefined,
    platformSupport = false,
): HasFeature {
    return key => key === 'platform_support'
        ? platformSupport
        : key === 'admin' ? (roles ?? []).includes('admin') : features?.[key] === true;
}

/** Could this person see the tour on some URL: the tour's `requires`, and at
 *  least one step left after each step's `requires`. */
export function visibleTo(tour: PageTour, has: HasFeature): boolean {
    if (tour.requires && !has(tour.requires)) return false;
    return tour.steps.some(s => !s.requires || has(s.requires));
}

/** The tours on this URL that this person may see, steps they cannot see
 *  removed, in registry order. A tour's `unlessQuery` gives way only to a
 *  tour this person can see that is made for that URL (the Team tab's own
 *  tour): someone without the Team tab who follows a ?view=team link sees
 *  their own list, so they keep its tour. */
export function toursForPage(
    tours: readonly PageTour[],
    pathname: string,
    search: string,
    has: HasFeature,
): PageTour[] {
    const out: PageTour[] = [];
    for (const tour of tours) {
        if (!tour.routes.some(r => matchRoute(r, pathname))) continue;
        if (!matchQuery(tour.query, search)) continue;
        if (!visibleTo(tour, has)) continue;
        const steps = tour.steps.filter(s => !s.requires || has(s.requires));
        out.push(steps.length === tour.steps.length ? tour : { ...tour, steps });
    }
    return out.filter(
        t =>
            !t.unlessQuery ||
            Object.keys(t.unlessQuery).length === 0 ||
            !matchQuery(t.unlessQuery, search) ||
            !out.some(o => o !== t && o.query !== undefined && sameQuery(o.query, t.unlessQuery)),
    );
}

/** One running tour: the cards to show, and the tours it settles when it ends. */
export interface TourRun {
    /** Stable for the same set of tours; used to avoid restarting a run. */
    key: string;
    kind: 'intro' | 'new';
    /** Shown on every card. */
    title: string;
    /** Tour ids recorded as finished/skipped when the run ends. */
    settles: string[];
    steps: TourStep[];
    helpTopic?: string;
}

function sameQuery(a?: Record<string, string>, b?: Record<string, string>): boolean {
    const ka = Object.keys(a ?? {});
    const kb = Object.keys(b ?? {});
    return ka.length === kb.length && ka.every(k => (a ?? {})[k] === (b ?? {})[k]);
}

function sharesRoute(a: PageTour, b: PageTour): boolean {
    return a.routes.some(r => b.routes.includes(r));
}

/** The `new` tours an intro makes redundant: same page, same tab. */
export function coveredByIntro(intro: PageTour, all: readonly PageTour[]): string[] {
    return all
        .filter(t => t.kind === 'new' && sharesRoute(intro, t) && sameQuery(intro.query, t.query))
        .map(t => t.id);
}

function introRun(intro: PageTour, all: readonly PageTour[]): TourRun {
    return {
        key: intro.id,
        kind: 'intro',
        title: intro.title,
        settles: [intro.id, ...coveredByIntro(intro, all)],
        steps: intro.steps,
        helpTopic: intro.helpTopic,
    };
}

function newRun(pending: PageTour[]): TourRun | null {
    if (pending.length === 0) return null;
    const ordered = [...pending].sort((a, b) => (a.since ?? '').localeCompare(b.since ?? ''));
    return {
        key: ordered.map(t => t.id).join('+'),
        kind: 'new',
        title: ordered.length === 1 ? ordered[0].title : 'What’s new on this page',
        settles: ordered.map(t => t.id),
        steps: ordered.flatMap(t => t.steps),
        helpTopic: ordered.length === 1 ? ordered[0].helpTopic : ordered.find(t => t.helpTopic)?.helpTopic,
    };
}

/** Intros with no query first, so a page's own intro runs before a tab's. */
function introsInOrder(pageTours: readonly PageTour[]): PageTour[] {
    return pageTours
        .filter(t => t.kind === 'intro')
        .sort((a, b) => Object.keys(a.query ?? {}).length - Object.keys(b.query ?? {}).length);
}

/** The tour that should start by itself on this page now, if any.
 *  `pageTours` is `toursForPage(...)` for the current URL; `allTours` is every
 *  tour this person can see (`visibleTo`), so an intro also settles its page's
 *  `new` tours on other tabs. Never ones behind a feature the person lacks:
 *  when they are given that feature later, its card is news to them. */
export function pickAutoRun(
    pageTours: readonly PageTour[],
    seen: SeenMap,
    allTours: readonly PageTour[] = pageTours,
): TourRun | null {
    const intro = introsInOrder(pageTours).find(t => t.autoStart !== false && !seen[t.id]);
    if (intro) return introRun(intro, allTours);
    // An intro that has not been seen but never starts by itself (the live L10)
    // still holds back the page's `new` tours: they would assume the person
    // knows the page.
    const heldBack = new Set(
        introsInOrder(pageTours)
            .filter(t => t.autoStart === false && !seen[t.id])
            .flatMap(t => coveredByIntro(t, allTours)),
    );
    const pending = pageTours.filter(
        t => t.kind === 'new' && t.autoStart !== false && !seen[t.id] && !heldBack.has(t.id),
    );
    return newRun(pending);
}

/** What "Replay this page's tour" runs: the most specific intro on this URL,
 *  else every `new` tour on it. Null when the page has no tour. */
export function replayRun(pageTours: readonly PageTour[], allTours: readonly PageTour[] = pageTours): TourRun | null {
    const intros = introsInOrder(pageTours);
    const intro = intros[intros.length - 1];
    if (intro) return introRun(intro, allTours);
    return newRun(pageTours.filter(t => t.kind === 'new'));
}

/** The very first tour someone sees ends with one more card: where to see a
 *  tour again (content/account.ts → REPLAY_HINT). Once anything is seen, or
 *  when the tour already says it, the run is left alone. */
export function withFirstTourHint(run: TourRun | null, seen: SeenMap, hint: TourStep): TourRun | null {
    if (!run || Object.keys(seen).length > 0) return run;
    if (run.steps.some(s => s.id === hint.id || (hint.target !== undefined && s.target === hint.target))) return run;
    return { ...run, steps: [...run.steps, hint] };
}

/** The seen map after a run ends. A finished tour stays finished when a later
 *  replay of it is skipped (the server keeps the same rule). */
export function settle(seen: SeenMap, ids: readonly string[], status: SeenStatus): Record<string, SeenStatus> {
    const next: Record<string, SeenStatus> = { ...seen };
    for (const id of ids) {
        if (next[id] === 'completed' && status === 'skipped') continue;
        next[id] = status;
    }
    return next;
}
