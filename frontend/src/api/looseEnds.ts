// Loose Ends — the AE loop-closure scan (Sales → Loose Ends, and its My Day
// panel).
//
// THE CONTRACT IS `docs/loose_ends/README.md` §5, and the server that keeps it
// is `backend/routers/loose_ends.py` with its read models in
// `rubik_core/loop_closure/views.py`. Every wire string in this file was read
// out of that contract: the three action kinds, the dismiss reasons, the mute
// scopes and the coverage states. `looseEnds.test.ts` re-derives the action
// kinds and the dismiss reasons FROM the Python and the coverage states from
// the README, so a drift fails a test instead of an AE's morning.
//
// Uses the shared axios instance exactly like `salesRhythm.ts` and
// `cockpit.ts`: full `/api/...` paths, no baseURL, no timeout of our own
// (client.ts is 105s, above the backend's 90s cap — the "Draft a reply" call
// runs one model call and a Gmail write, so it can take a few seconds).
//
// FOUR THINGS IN HERE ARE LOAD-BEARING AND NOT STYLE CHOICES:
//
// 1. Flag off is a 404 and not-on-the-roster is a 403, on every endpoint.
//    Both are calm empty states, never a red error: `looseEndsError` maps them
//    through the cockpit's `cockpitError`, so `isNotEnabled` / `isForbidden`
//    work unchanged. A 404 on a WRITE is different — the router answers 404
//    for an id that is not yours or no longer exists — so writes go through
//    `looseEndsWriteError`, which never says "not switched on" for a row that
//    simply closed itself between the page load and the click.
// 2. "Could not look" is never "all clear" (README §2). Coverage arrives as a
//    named state per source; every state other than `ok` has a sentence here,
//    and a state this build has never heard of still produces a sentence that
//    admits the gap rather than nothing.
// 3. Every number carries its denominator. The helpers that print a number
//    (`coverageCounts`, `noiseLine`, `closedShareLine`) print what it is out of
//    in the same sentence, or say there is nothing to divide by.
// 4. Nothing here closes an item. Closure is DETECTED by the morning scan;
//    the only manual exit is "Not a real miss", which is counted as noise.
//    That is why there is no `closeLooseEnd()`.
import client from './client';
import { cockpitError, NOT_ENABLED } from './cockpit';
import type { AgeState, CockpitError } from './cockpit';
import { parseInstant } from '../utils/datetime.ts';

// ─── the three kinds ─────────────────────────────────────────────────────────

/** `loop_items.kind` (README §3), in the order the page shows them. */
export const LOOSE_END_KINDS = ['unlogged', 'open_loop', 'todo'] as const;
export type LooseEndKind = (typeof LOOSE_END_KINDS)[number];

export const isLooseEndKind = (v: unknown): v is LooseEndKind =>
    typeof v === 'string' && (LOOSE_END_KINDS as readonly string[]).includes(v);

/** The group labels, for the places that do not get them from the server
 *  (the team view's per-person counts, the My Day panel's kind line). The
 *  mine endpoint sends its own `label` per group and that one wins there. */
export const LOOSE_END_KIND_LABELS: Record<LooseEndKind, string> = {
    unlogged: 'Not in HubSpot',
    open_loop: 'Waiting on your reply',
    todo: 'You said you would',
};

/** Third-person short form for the team view, where "your" would be wrong. */
export const LOOSE_END_KIND_SHORT: Record<LooseEndKind, string> = {
    unlogged: 'not in HubSpot',
    open_loop: 'waiting on a reply',
    todo: 'promised',
};

/** Falls back to the raw value: a kind a newer backend introduced renders as
 *  itself, never as "undefined". */
export const looseEndKindLabel = (kind: string): string =>
    isLooseEndKind(kind) ? LOOSE_END_KIND_LABELS[kind] : kind;

// ─── actions (closed set) ────────────────────────────────────────────────────

/** The action `kind` strings the router emits (README §5). Closed set:
 *
 *  | kind             | where                    | what the client does                      |
 *  |------------------|--------------------------|-------------------------------------------|
 *  | `log_in_hubspot` | unlogged email, primary  | GET …/log-preview, show the emails, then  |
 *  |                  |                          | POST …/log with the shown message ids     |
 *  | `draft_reply`    | open_loop, primary       | POST …/draft-reply, then link to the draft |
 *  | `open_link`      | todo / Slack primary,    | open `href` in a new tab                  |
 *  |                  | secondary                |                                           |
 *
 *  `log_in_hubspot` logs the AE's OWN SENT EMAILS, full text, as HubSpot
 *  emails — never a note and never AI text (call notes come from Fathom). It
 *  is only offered on an email with a HubSpot contact; a Slack item opens the
 *  record in HubSpot instead.
 *
 *  `isLooseEndActionKind` stays in front of every switch: an unrecognised kind
 *  from a newer backend degrades to a link (when it carries an `href`) or a
 *  disabled button that explains itself, never a throw. */
export const LOOSE_END_ACTION_KINDS = ['log_in_hubspot', 'draft_reply', 'open_link'] as const;
export type LooseEndActionKind = (typeof LOOSE_END_ACTION_KINDS)[number];

export const isLooseEndActionKind = (v: unknown): v is LooseEndActionKind =>
    typeof v === 'string' && (LOOSE_END_ACTION_KINDS as readonly string[]).includes(v);

export interface LooseEndAction {
    /** Typed as `string`, deliberately wider than `LooseEndActionKind`, so a
     *  fourth kind is a runtime degrade rather than a compile error. Narrow with
     *  `isLooseEndActionKind` before switching. */
    kind: string;
    label: string;
    /** `open_link` only: the Gmail / Slack / HubSpot URL, opened in a new tab. */
    href?: string | null;
}

// ─── dismiss ("Not a real miss") and mutes ───────────────────────────────────

/** `loop_items.dismiss_reason` — VERBATIM the `DISMISS_REASONS` tuple in
 *  `rubik_core/loop_closure/constants.py` (pinned by looseEnds.test.ts). A
 *  dismissal is the spec's noise signal, so the reason is required. */
export const DISMISS_REASONS = [
    'not_a_client',
    'automated',
    'internal',
    'handled_elsewhere',
    'not_a_promise',
    'other',
] as const;
export type DismissReason = (typeof DISMISS_REASONS)[number];

/** VERBATIM `DISMISS_REASON_LABELS` in `rubik_core/loop_closure/constants.py`
 *  (pinned by looseEnds.test.ts), so the picker here, the Team tab's noise
 *  breakdown and the Ask Cubie answer all name a reason the same way. */
export const DISMISS_REASON_LABELS: Record<DismissReason, string> = {
    not_a_client: 'Not a client',
    automated: 'Automated email',
    internal: 'Internal',
    handled_elsewhere: 'Handled somewhere else',
    not_a_promise: 'Not a promise',
    other: 'Other',
};

/** The reasons worth offering for one kind. A promise is never "automated",
 *  and an email is never "not a promise"; offering every reason everywhere
 *  makes the noise breakdown on the Team tab say less. The server accepts any
 *  of `DISMISS_REASONS` for any kind — this only shapes the picker. */
export const dismissReasonsFor = (kind: string): readonly DismissReason[] =>
    kind === 'todo'
        ? ['not_a_promise', 'handled_elsewhere', 'internal', 'other']
        : ['not_a_client', 'automated', 'internal', 'handled_elsewhere', 'other'];

export const dismissReasonLabel = (reason: string): string =>
    (DISMISS_REASON_LABELS as Record<string, string>)[reason] ?? reason.replace(/_/g, ' ');

/** The `mute` field of the dismiss body. `null` (not one of these) = keep
 *  flagging. Only offered on email items: a mute is a sender or a domain. */
export const MUTE_SCOPES = ['sender', 'domain'] as const;
export type MuteScope = (typeof MUTE_SCOPES)[number];

export const MUTE_SCOPE_LABELS: Record<MuteScope, string> = {
    sender: 'Stop flagging this sender',
    domain: 'Stop flagging everyone at this domain',
};

/** `loop_mutes.pattern` is `jane@acme.example` or `@acme.example`. */
export const mutePatternLabel = (pattern: string): string =>
    pattern.startsWith('@') ? `Everyone at ${pattern.slice(1)}` : pattern;

// ─── close reasons (Team tab, "closed by reason") ────────────────────────────

/** `loop_items.close_reason` (README §3). `aged_out` and `rescheduled` leave
 *  the list without being done: neither is ever counted as fixed.
 *  `logged_from_assistant` is a stored value, frozen in
 *  `rubik_core/identifiers.py` (backend/tests/test_identifiers.py pins it). */
export const CLOSE_REASONS = [
    'logged_in_hubspot',
    'logged_from_assistant',
    'replied',
    'logged_follow_up',
    'task_completed',
    'task_gone',
    'promise_kept',
    'promise_withdrawn',
    'rescheduled',
    'aged_out',
] as const;
export type CloseReason = (typeof CLOSE_REASONS)[number];

export const CLOSE_REASON_LABELS: Record<CloseReason, string> = {
    logged_in_hubspot: 'Logged in HubSpot',
    logged_from_assistant: 'Logged from Cubie',
    replied: 'Replied',
    logged_follow_up: 'Follow-up logged in HubSpot',
    task_completed: 'HubSpot task completed',
    task_gone: 'HubSpot task removed or reassigned',
    promise_kept: 'Promise kept',
    promise_withdrawn: 'Promise withdrawn',
    rescheduled: 'Rescheduled to a later date (not fixed)',
    aged_out: 'Aged out after 60 days (not fixed)',
};

/** The close reasons that are NOT a fix — `UNFIXED_REASONS` in
 *  `rubik_core/loop_closure/constants.py`. The health panel lists them in
 *  "How they closed" without counting them in the closed total. */
export const UNFIXED_CLOSE_REASONS: readonly string[] = ['aged_out', 'rescheduled'];

export const isFixedCloseReason = (reason: string): boolean => !UNFIXED_CLOSE_REASONS.includes(reason);

export const closeReasonLabel = (reason: string): string =>
    (CLOSE_REASON_LABELS as Record<string, string>)[reason] ?? reason.replace(/_/g, ' ');

// ─── coverage ("could not look" is never "all clear") ────────────────────────

/** The four sources a morning scan reads, in the order they are named. */
export const COVERAGE_SOURCES = ['gmail', 'slack', 'hubspot', 'ai'] as const;
export type CoverageSource = (typeof COVERAGE_SOURCES)[number];

const SOURCE_NAMES: Record<CoverageSource, string> = {
    gmail: 'Gmail',
    slack: 'Slack',
    hubspot: 'HubSpot',
    ai: 'the AI check',
};

/** One plain sentence per non-`ok` state, written to the AE (README §3's
 *  `Coverage`). The server also sends its own `notes` on the mine endpoint;
 *  these are for the places it does not (the panel footer, an older backend),
 *  and they are the source of the Team tab's badge tooltips. */
export const COVERAGE_COPY: Record<CoverageSource, Record<string, string>> = {
    // Worded as `constants.coverage_notes()` words them, so the panel footer,
    // the Team tab's tooltips and the page's notes never disagree.
    gmail: {
        unavailable:
            'Cubie could not read your mailbox in the last scan, so email was not checked and nothing from email was closed.',
        partial:
            'Your mailbox had more email in the last two weeks than Cubie reads in one morning, so the oldest threads were not checked and some loose ends may be missing from this list.',
    },
    slack: {
        not_connected:
            'Slack is not connected, so Slack messages were not checked. Connect it in Settings → Slack.',
        unavailable: 'Slack could not be read in the last scan, so Slack messages were not checked.',
    },
    hubspot: {
        no_owner:
            'Your Cubie email does not match a HubSpot owner, so your overdue HubSpot tasks were not checked. An admin can fix the match in HubSpot.',
        unavailable:
            'HubSpot could not be read in the last scan, so nothing new was flagged as missing from HubSpot and nothing was closed by HubSpot activity.',
        partial:
            'HubSpot returned only part of its activity history in the last scan, so some emails could not be checked against it and were not flagged.',
    },
    ai: {
        unavailable:
            'The AI check was unavailable in the last scan, so new client emails and Slack messages were not added. Items already on your list were still re-checked.',
    },
};

/** Short badge text for the Team tab, one per non-`ok` state. */
export const COVERAGE_BADGES: Record<CoverageSource, Record<string, string>> = {
    gmail: { unavailable: 'Gmail not read', partial: 'Gmail partly read' },
    slack: { not_connected: 'Slack not connected', unavailable: 'Slack not read' },
    hubspot: { no_owner: 'No HubSpot owner', unavailable: 'HubSpot not read', partial: 'HubSpot partial' },
    ai: { unavailable: 'AI check skipped' },
};

/** Per-source states. Typed as strings so a state a newer backend adds still
 *  reaches `coverageGaps` (and its admits-the-gap fallback). */
export interface LooseEndsSourceStates {
    gmail?: string | null;
    slack?: string | null;
    hubspot?: string | null;
    ai?: string | null;
}

/** README §3 `Coverage`, as the mine endpoint sends it. */
export interface LooseEndsCoverage extends LooseEndsSourceStates {
    threads_scanned?: number | null;
    slack_messages_scanned?: number | null;
    promises_scanned?: number | null;
    tasks_scanned?: number | null;
    new?: number | null;
    closed?: number | null;
}

export interface CoverageGap {
    source: CoverageSource;
    state: string;
    /** Badge text. */
    badge: string;
    /** The full sentence. */
    sentence: string;
}

/** Every source that was NOT read cleanly, with its words. A missing state is
 *  not a gap (an older payload without that key); an unknown state is one. */
export function coverageGaps(coverage: LooseEndsSourceStates | null | undefined): CoverageGap[] {
    if (!coverage) return [];
    const out: CoverageGap[] = [];
    for (const source of COVERAGE_SOURCES) {
        const state = coverage[source];
        if (!state || state === 'ok') continue;
        out.push({
            source,
            state,
            badge: COVERAGE_BADGES[source][state] ?? `${SOURCE_NAMES[source]}: ${state.replace(/_/g, ' ')}`,
            sentence:
                COVERAGE_COPY[source][state] ??
                `Cubie could not fully check ${SOURCE_NAMES[source]} in the last scan (${state}). Do not read an empty list as "all clear".`,
        });
    }
    return out;
}

const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

/** "1, 2 and 3" — the Oxford-less join the rest of Cubie writes. */
const joinAnd = (parts: string[]): string =>
    parts.length <= 1 ? (parts[0] ?? '') : `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;

/** The denominators of a scan: "52 email threads, 31 Slack messages, 4
 *  promises and 7 HubSpot tasks". A source that was not read is LEFT OUT
 *  rather than printed as zero — "checked 0 email threads" next to an
 *  unreadable mailbox would read as an empty inbox; the coverage sentence says
 *  what happened instead. Returns '' when nothing was read at all. */
export function coverageCounts(coverage: LooseEndsCoverage | null | undefined): string {
    if (!coverage) return '';
    const parts: string[] = [];
    // `partial` read the threads it counts; only the oldest were left out.
    const mailRead = coverage.gmail === 'ok' || coverage.gmail === 'partial';
    if (mailRead || (coverage.gmail == null && coverage.threads_scanned != null)) {
        parts.push(plural(coverage.threads_scanned ?? 0, 'email thread'));
    }
    if (coverage.slack === 'ok' || (coverage.slack == null && coverage.slack_messages_scanned != null)) {
        parts.push(plural(coverage.slack_messages_scanned ?? 0, 'Slack message'));
    }
    if (coverage.promises_scanned != null) {
        parts.push(plural(coverage.promises_scanned, 'promise'));
    }
    const tasksRead = coverage.hubspot === 'ok' || coverage.hubspot === 'partial';
    if (tasksRead || (coverage.hubspot == null && coverage.tasks_scanned != null)) {
        parts.push(plural(coverage.tasks_scanned ?? 0, 'HubSpot task'));
    }
    return joinAnd(parts);
}

/** When the last scan ran, in the reader's own clock: "this morning at 7:04
 *  AM", "today at 2:10 PM", "yesterday at 7:04 AM", "on Mon, Oct 5 at 7:04
 *  AM". `scannedAt` is naive UTC from the API — `parseInstant` reads it as the
 *  instant it is. `now` is a parameter so a render stays pure and a test can
 *  pin it. Returns '' for a missing or unreadable timestamp. */
export function scannedAtPhrase(scannedAt: string | null | undefined, now: Date): string {
    const at = parseInstant(scannedAt);
    if (!at) return '';
    const time = at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    const day = (d: Date): string => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    if (day(at) === day(now)) return at.getHours() < 12 ? `this morning at ${time}` : `today at ${time}`;
    if (day(at) === day(yesterday)) return `yesterday at ${time}`;
    const date = at.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    return `on ${date} at ${time}`;
}

/** "Checked 52 email threads, 31 Slack messages, 4 promises and 7 HubSpot
 *  tasks this morning at 7:04 AM." — the page's one-line denominator. */
export function coverageLine(
    coverage: LooseEndsCoverage | null | undefined,
    scannedAt: string | null | undefined,
    now: Date,
): string {
    // No coverage = no scan has covered this person; there is nothing to
    // count, and "no source could be read" would be the wrong sentence.
    if (!coverage) return '';
    const counts = coverageCounts(coverage);
    const when = scannedAtPhrase(scannedAt, now);
    if (!counts) return when ? `The last scan ran ${when}, but no source could be read.` : '';
    return `Checked ${counts}${when ? ` ${when}` : ''}.`;
}

// ─── the mine payload (GET /api/sales/loose-ends) ────────────────────────────

/** The age chip. `state` is one of the cockpit's `AGE_STATES` — the scan uses
 *  `at_risk` below the escalation days and `breached` at or over it (README
 *  §4.7). `label` ("4 days") is server-rendered: print it verbatim. */
export interface LooseEndAge {
    state: AgeState | string;
    label: string;
}

export interface LooseEnd {
    id: string;
    kind: string;
    /** `gmail` | `slack` | `promise` | `hubspot_task`. Email items (`gmail`)
     *  are the ones a sender / domain mute applies to. */
    source: string;
    /** "" when `content_hidden` (impersonation). */
    title: string;
    /** The AI one-liner. Omitted when there is none or it is hidden — every
     *  response sets `response_model_exclude_none=True`, so an unset optional
     *  is MISSING rather than null. */
    headline?: string | null;
    counterparty?: string | null;
    company?: string | null;
    /** Naive UTC instants — read with `parseInstant`, never `new Date`.
     *  Optional on the router's schema, so a row without one still renders. */
    opened_at?: string | null;
    first_flagged_at?: string | null;
    days_open: number;
    /** Absent = no chip (never a made-up one). */
    age?: LooseEndAge | null;
    /** It closed once and a new unlogged email re-opened it. */
    reopened: boolean;
    /** One per row by contract (README §5). The router's schema lets it be
     *  absent, so the page promotes the secondary action rather than crash. */
    primary_action?: LooseEndAction | null;
    secondary_action?: LooseEndAction | null;
}

export interface LooseEndsGroup {
    kind: string;
    label: string;
    hint: string;
    /** What an empty group means, in its own words. */
    empty_line: string;
    count: number;
    items: LooseEnd[];
}

export interface LooseEndsTotals {
    open: number;
    over_escalation: number;
    flagged_this_week: number;
    closed_this_week: number;
}

export interface LooseEndsThresholds {
    open_loop_business_days: number;
    todo_grace_days: number;
    todo_undated_days: number;
    escalation_days: number;
}

export interface LooseEndsMute {
    id: string;
    pattern: string;
    created_at?: string | null;
}

export interface LooseEndsMine {
    /** Naive UTC, or missing/null = this person has never been scanned. */
    scanned_at?: string | null;
    /** Missing/null before the first scan. */
    coverage?: LooseEndsCoverage | null;
    /** Server-written plain sentences — print each one. */
    notes?: string[] | null;
    totals: LooseEndsTotals;
    groups: LooseEndsGroup[];
    thresholds: LooseEndsThresholds;
    mutes?: LooseEndsMute[] | null;
    /** True under impersonation: titles and headlines are blanked, and the
     *  router refuses the HubSpot and Gmail writes. */
    content_hidden: boolean;
}

// ─── the team payload (GET /api/sales/loose-ends/team) ───────────────────────
//
// NO titles, headlines, subjects or snippets exist in this shape, by contract
// (README §2: mailbox content never leaves its owner). Do not add them here —
// a field on the type is a field some screen will render.

export interface LooseEndsTeamItem {
    id: string;
    kind: string;
    /** "Jane Doe · Acme Corp" — CRM names only. */
    who: string;
    days_open: number;
    age?: LooseEndAge | null;
}

/** Aging buckets, in bar order. */
export const AGING_BUCKETS = ['d0_2', 'd3_7', 'd8_14', 'd15_plus'] as const;
export type AgingBucket = (typeof AGING_BUCKETS)[number];

export const AGING_BUCKET_LABELS: Record<AgingBucket, string> = {
    d0_2: '0–2 days',
    d3_7: '3–7 days',
    d8_14: '8–14 days',
    d15_plus: '15+ days',
};

export type LooseEndsAging = Partial<Record<AgingBucket, number>>;

export interface TeamPerson {
    email: string;
    name: string;
    open: number;
    by_kind: Partial<Record<LooseEndKind, number>> & Record<string, number>;
    aging: LooseEndsAging;
    over_escalation: number;
    closed_this_week: number;
    /** States only, no counts. Missing = not scanned yet. */
    coverage?: LooseEndsSourceStates | null;
    /** Oldest first, at most 25. */
    items: LooseEndsTeamItem[];
}

export interface LooseEndsHealthWeek {
    /** A calendar date (`YYYY-MM-DD`), not an instant. */
    week_start: string;
    flagged: number;
    closed: number;
    median_days_to_close?: number | null;
}

export interface LooseEndsHealth {
    window_days: number;
    flagged: number;
    dismissed: number;
    /** dismissed / flagged; missing when nothing was flagged. */
    noise_rate?: number | null;
    dismissed_by_reason: Record<string, number>;
    closed: number;
    closed_by_reason: Record<string, number>;
    median_days_to_close?: number | null;
    weekly: LooseEndsHealthWeek[];
}

export interface LooseEndsScorecard {
    metric_name: string;
    metrics_found: number;
    people_without_metric: number;
    /** Emails of the missing ones with no Accountability Chart seat — setup
     *  cannot add a row for them, so `can_setup` ignores them. */
    people_without_seat?: string[] | null;
    can_setup: boolean;
}

export interface LooseEndsTeam {
    scanned_at?: string | null;
    /** A–Z by name, from the server. Never re-sort: no leaderboard. */
    people: TeamPerson[];
    roster_size: number;
    /** Optional on the router's schema; the page says so when it is missing
     *  rather than printing zeros. */
    health?: LooseEndsHealth | null;
    scorecard?: LooseEndsScorecard | null;
    /** The same tunables the AE view sends ("past 7 days"). */
    thresholds?: LooseEndsThresholds | null;
}

/** "3 of 41 flags marked not a real miss (7%)" — always numerator of
 *  denominator. With nothing flagged there is no rate to show, and saying 0%
 *  would claim a clean detector on no evidence. */
export function noiseLine(health: Pick<LooseEndsHealth, 'flagged' | 'dismissed' | 'noise_rate'>): string {
    if (!health.flagged) return 'Nothing flagged yet, so there is no noise rate to show.';
    const rate = health.noise_rate ?? health.dismissed / health.flagged;
    const pct = Math.round(rate * 100);
    return `${health.dismissed} of ${plural(health.flagged, 'flag')} marked not a real miss (${pct}%)`;
}

/** "9 of 22 closed" — one close reason against every close in the window. */
export function closedShareLine(count: number, closed: number): string {
    return `${count} of ${closed}`;
}

/** "2.5 days" / "1 day"; null → no closes to take a median of. */
export function medianDaysLabel(days: number | null | undefined): string | null {
    if (days == null || Number.isNaN(days)) return null;
    const rounded = Math.round(days * 10) / 10;
    return `${rounded} ${rounded === 1 ? 'day' : 'days'}`;
}

// ─── "Log it in HubSpot": the preview, then the write ────────────────────────

/** One email the AE sent, as it will be logged (GET …/log-preview). Read
 *  fresh from the AE's own mailbox when they click; the row never carries it. */
export interface LogPreviewEmail {
    /** The Gmail message id — what POST …/log sends back, never the content. */
    message_id: string;
    subject: string;
    to: string[];
    cc?: string[] | null;
    /** Naive UTC — read with `parseInstant`. */
    sent_at?: string | null;
    /** The first 600 characters of the text that will be logged. */
    excerpt: string;
    /** How many characters of text will be logged in full. */
    chars: number;
    /** The text was cut at 65,000 characters (HubSpot's limit is 65,536). */
    truncated: boolean;
    /** The formatted version is too long for HubSpot, so the email is logged
     *  as its plain text. */
    plain_text_only?: boolean;
    /** To or Cc holds the contact the email is logged on. Only these start
     *  ticked; a forward to anyone else is the AE's call. */
    to_contact?: boolean;
    /** Already in HubSpot (the Gmail extension, or an earlier click): shown
     *  greyed and never sent again. */
    already_logged: boolean;
}

export interface LogPreview {
    emails: LogPreviewEmail[];
    /** HubSpot names the emails will be logged on. `deal` only when the deal
     *  is in a pipeline Cubie may write to. Missing = not attached there. */
    contact?: string | null;
    company?: string | null;
    deal?: string | null;
    hubspot_url?: string | null;
}

/** The most message ids one POST …/log takes (`crm_log.MAX_EMAILS_PER_LOG`);
 *  more are sent in batches (`logBatches`). */
export const MAX_EMAILS_PER_LOG = 20;

/** The emails ticked when the preview opens: the ones that went to the
 *  contact they will be logged on, and that HubSpot does not already have. A
 *  forward to someone else (a partner, a personal address) starts unticked. */
export const defaultLogSelection = (preview: Pick<LogPreview, 'emails'>): string[] =>
    preview.emails.filter(e => !e.already_logged && e.to_contact === true).map(e => e.message_id);

/** The message ids the confirm button sends: the ticked emails HubSpot does
 *  not already have, in the order shown. */
export const emailsToLog = (
    preview: Pick<LogPreview, 'emails'>,
    selected: ReadonlySet<string>,
): string[] => preview.emails.filter(e => !e.already_logged && selected.has(e.message_id)).map(e => e.message_id);

/** `ids` in POST-sized batches, in order. Every batch but the last is sent
 *  with `more: true`, so the item closes only once all of them are in. */
export const logBatches = (ids: readonly string[], size: number = MAX_EMAILS_PER_LOG): string[][] => {
    const out: string[][] = [];
    for (let i = 0; i < ids.length; i += size) out.push(ids.slice(i, i + size));
    return out;
};

/** "Jane Doe · Acme Corp · Acme 2026 renewal" — the records the emails land
 *  on; "the HubSpot contact" when the server sent no names. */
export const logTargetLine = (preview: Pick<LogPreview, 'contact' | 'company' | 'deal'>): string => {
    const names = [preview.contact, preview.company, preview.deal].filter(
        (n): n is string => typeof n === 'string' && n.trim().length > 0,
    );
    return names.length > 0 ? names.join(' · ') : 'the HubSpot contact';
};

/** "Log 2 emails in HubSpot" — the confirm button, naming exactly how many. */
export const logButtonLabel = (count: number): string =>
    `Log ${count} ${count === 1 ? 'email' : 'emails'} in HubSpot`;

// ─── mutation results ────────────────────────────────────────────────────────

export interface LogLooseEndResult {
    /** `logged` (the item is closed) or `in_progress` (a batch sent with
     *  `more`; the item stays open for the next one). */
    status: 'logged' | 'in_progress' | string;
    hubspot_url?: string | null;
    /** Emails written now. */
    logged?: number;
    /** Emails HubSpot already had (not written again). */
    already?: number;
}

export interface DraftLooseEndReplyResult {
    status: 'drafted' | string;
    draft_url?: string | null;
}

export interface DismissLooseEndResult {
    status: 'dismissed' | string;
}

export interface RemoveLooseEndsMuteResult {
    status: 'removed' | string;
}

export interface LooseEndsScorecardSetupResult {
    created: number;
    /** AEs with no `eos_people` row, so no measurable could be owned by them. */
    skipped_no_person: string[];
    already: number;
}

// ─── errors ──────────────────────────────────────────────────────────────────

// The cockpit's error shape and its two calm-state predicates are reused as
// they are: the routers share the 404-when-off / 403-when-not-on-the-roster
// convention, so there is nothing to re-derive.
export { isForbidden, isNotEnabled } from './cockpit';
export type { CockpitError as LooseEndsError } from './cockpit';

/** A READ failed. 404 = the feature is switched off (`LOOP_CLOSURE_ENABLED`),
 *  403 = not on the roster; both keep the cockpit's codes so `isNotEnabled` /
 *  `isForbidden` work, but with this feature's own sentence. */
export function looseEndsError(e: unknown): CockpitError {
    const err = cockpitError(e, 'Loose Ends could not be reached.');
    if (err.code === NOT_ENABLED) return { ...err, message: 'Loose Ends is not switched on yet.' };
    return err;
}

/** A WRITE failed. The router answers 404 for an id that is not yours or no
 *  longer exists — on a write that almost always means the item closed itself
 *  (the scan saw your reply) or was handled in another tab, never that the
 *  feature is off. 409 is a refusal with a reason (not open any more, or you
 *  are viewing as someone else) and 502 is HubSpot's or Gmail's own error in
 *  plain words; both are printed as the server wrote them. */
export function looseEndsWriteError(e: unknown): string {
    const err = cockpitError(e, 'That did not go through. Try again in a moment.');
    if (err.status === 404) {
        return 'That loose end is no longer on your list — it may have closed on its own. The list has been refreshed.';
    }
    return err.message;
}

// ─── calls ───────────────────────────────────────────────────────────────────

const BASE = '/api/sales/loose-ends';
const itemPath = (id: string, tail: string): string => `${BASE}/${encodeURIComponent(id)}/${tail}`;

export async function fetchLooseEnds(): Promise<LooseEndsMine> {
    const { data } = await client.get<LooseEndsMine>(BASE);
    return data;
}

/** Sales leaders (`loop_closure_team`). Every read writes an audit row
 *  server-side: it is a cross-person read. */
export async function fetchLooseEndsTeam(): Promise<LooseEndsTeam> {
    const { data } = await client.get<LooseEndsTeam>(`${BASE}/team`);
    return data;
}

/** The AE's sent emails in the item's thread, as "Log it in HubSpot" would
 *  log them. Reads the AE's own mailbox server-side; refused (409) while
 *  viewing as someone else. Writes nothing. */
export async function fetchLooseEndLogPreview(id: string): Promise<LogPreview> {
    const { data } = await client.get<LogPreview>(itemPath(id, 'log-preview'));
    return data;
}

/** Logs the emails the reader ticked — by Gmail message id only; the server
 *  re-reads their content from the mailbox — as HubSpot emails, and closes the
 *  item `logged_from_assistant` (unless `more`: another batch follows and the item
 *  stays open). Idempotent server-side. Only call this from the preview
 *  dialog's confirm, with a batch of `emailsToLog(preview, selected)`. */
export async function logLooseEnd(id: string, messageIds: string[], more = false): Promise<LogLooseEndResult> {
    const { data } = await client.post<LogLooseEndResult>(itemPath(id, 'log'), { message_ids: messageIds, more });
    return data;
}

/** Creates a Gmail DRAFT in the thread — never sends. The item stays open
 *  until the reply is actually sent and the next scan sees it. */
export async function draftLooseEndReply(id: string): Promise<DraftLooseEndReplyResult> {
    const { data } = await client.post<DraftLooseEndReplyResult>(itemPath(id, 'draft-reply'));
    return data;
}

/** "Not a real miss". `mute` is `null` to keep flagging the sender. */
export async function dismissLooseEnd(
    id: string,
    reason: DismissReason,
    mute: MuteScope | null,
): Promise<DismissLooseEndResult> {
    const { data } = await client.post<DismissLooseEndResult>(itemPath(id, 'dismiss'), { reason, mute });
    return data;
}

export async function removeLooseEndsMute(muteId: string): Promise<RemoveLooseEndsMuteResult> {
    const { data } = await client.delete<RemoveLooseEndsMuteResult>(
        `${BASE}/mutes/${encodeURIComponent(muteId)}`,
    );
    return data;
}

/** Creates the missing per-AE "Loose ends over 7 days" measurables in the Sales
 *  scorecard. AEs with no Accountability Chart row come back in
 *  `skipped_no_person`. */
export async function setupLooseEndsScorecard(): Promise<LooseEndsScorecardSetupResult> {
    const { data } = await client.post<LooseEndsScorecardSetupResult>(`${BASE}/team/scorecard-setup`);
    return data;
}
