// Page tours ("Walk me"): the cards that walk someone through a page the
// first time they open it, and through a feature added to a page they already
// know.
//
// A tour is DATA. Each area of the app keeps its tours in content/<area>.ts,
// registry.ts gathers them, and TourProvider.tsx decides when one starts. A
// card points at an element by its `data-tour` attribute, so a page opts in by
// carrying `data-tour="<name>"` on the element a card talks about, and nothing
// else about the page changes. tours.test.ts fails on a card whose target no
// page carries, a route App.tsx does not declare, a help topic Help does not
// have and a feature key the client does not know.
//
// Type-only import, so Node's test runner (which strips types) can load the
// content files without a bundler.
import type { FeatureKey } from '../context/AuthContext.tsx';

/** Which side of its target a card prefers. The card flips to the opposite
 *  side when there is no room, and docks to the bottom on a phone. */
export type TourPlacement = 'top' | 'bottom' | 'left' | 'right';

/** Who a tour or card is for: a feature verdict from `/api/users/me`, the
 *  `'admin'` role, or the live `'platform_support'` session state. The latter
 *  gates the banner tour to the only time its target exists. Never a
 *  department: feature audiences come from the server's registry data. */
export type TourAudience = FeatureKey | 'admin' | 'platform_support';

export interface TourStep {
    /** Stable inside its tour. */
    id: string;
    /** The card heading: a few words, sentence case. */
    title: string;
    /** One to three short sentences in plain language, for a business reader:
     *  what the person can do here and why it helps. No jargon, no file names. */
    body: string;
    /** The `data-tour` value of the element the card points at. Left out, the
     *  card sits in the middle of the screen (a welcome or a wrap-up). A target
     *  that is not on screen (an empty list, a tab this person cannot see)
     *  falls back to the middle too, never to an error. */
    target?: string;
    /** Preferred side of the target. Ignored when there is no target. */
    placement?: TourPlacement;
    /** Shown only to people the server lets use this feature (the same
     *  verdicts as `/api/users/me` → `features`), or only to admins. For a
     *  part of a page that only some of its readers see. */
    requires?: TourAudience;
}

export interface PageTour {
    /** Permanent id, the key stored per person: `<page>.intro` for a page's
     *  first-visit tour, `<page>.new.<feature>` for a feature added later.
     *  Never rename or reuse an id: a new id is shown again to everyone who
     *  saw the old one. */
    id: string;
    /** `intro` runs the first time someone opens the page. `new` runs for
     *  someone who already knows the page, the first time they open it after
     *  the feature ships. Finishing or skipping a page's intro also marks its
     *  `new` tours as seen, because the intro already covers the page as it
     *  is today. Keep the intro current when a `new` tour is added. */
    kind: 'intro' | 'new';
    /** The react-router paths the tour belongs to, written the way App.tsx
     *  declares them under "/" (`/chat`, `/eos/rocks`,
     *  `/customers/:companyId/xray`). */
    routes: string[];
    /** Only on a URL whose query string carries these values, e.g.
     *  `{ tab: 'files' }` for one tab of a page. */
    query?: Record<string, string>;
    /** Not on a URL whose query string carries these values, e.g.
     *  `{ view: 'team' }` keeps a page's default-tab tour off its Team tab. */
    unlessQuery?: Record<string, string>;
    /** Shown on the card ("Ask Cubie tour", "New in Rocks"). */
    title: string;
    /** A Help topic id (`/help/<id>`); the last card links to it. */
    helpTopic?: string;
    /** The whole tour is only for people with this feature (or, `'admin'`,
     *  only for admins). */
    requires?: TourAudience;
    /** `new` tours: the day the feature went live, YYYY-MM-DD. */
    since?: string;
    /** false = never starts by itself, only from "Replay this page's tour".
     *  For a page people use live in front of others (an L10 in progress, a
     *  sales call), where a card popping up would interrupt the meeting. */
    autoStart?: boolean;
    steps: TourStep[];
}
