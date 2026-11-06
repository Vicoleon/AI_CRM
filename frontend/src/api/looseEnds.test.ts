// Drift tests for Loose Ends (Sales → Loose Ends and its My Day panel). The
// server files are read as text, the same way cockpit.test.ts and
// salesRhythm.test.ts pin their vocabularies:
//
//   cd <repo>/frontend && node --test "src/api/looseEnds.test.ts"
//
// Pinned here:
//
//   * The action kinds the server emits (router + read models) are exactly the
//     ones the client declares, and LooseEnds.tsx handles each one case by
//     case — an undeclared kind would fall through to a disabled button on
//     every row of its group.
//   * `DISMISS_REASONS` is the tuple in `rubik_core/loop_closure/constants.py`,
//     and every close reason there has a label on the Team tab.
//   * Every coverage state the design contract names has its own sentence —
//     "could not look" must never render as "all clear".
//   * The numbers print their denominators, and the team list keeps the
//     server's A–Z order (no leaderboard).
//   * The surface is wired: route, nav row, My Day panel, feature keys.
//   * "Log it in HubSpot" logs the AE's own sent emails:
//     no note text anywhere on the wire, the preview endpoint the client calls
//     is the one the router serves, only the emails sent to the item's own
//     contact start ticked, the confirm sends exactly the ticked ids HubSpot
//     does not already have, in batches the router accepts, and the limits the
//     page prints are the server's.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

// `api/looseEnds.ts` imports `./client` and `./cockpit` extensionless, the way
// every module in this app does. Teach the runner Vite's rule, scoped to our
// own files (an unscoped hook breaks axios's CJS requires).
const resolveExtensionless: ResolveHookSync = (specifier, context, nextResolve) => {
    const parent = context.parentURL ?? '';
    const relative = /^\.{1,2}\//.test(specifier);
    if (parent.includes('/frontend/src/') && relative && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
};

registerHooks({ resolve: resolveExtensionless });

const {
    CLOSE_REASON_LABELS,
    COVERAGE_COPY,
    COVERAGE_SOURCES,
    DISMISS_REASONS,
    DISMISS_REASON_LABELS,
    LOOSE_END_ACTION_KINDS,
    LOOSE_END_KINDS,
    MUTE_SCOPES,
    UNFIXED_CLOSE_REASONS,
    closeReasonLabel,
    coverageCounts,
    coverageGaps,
    coverageLine,
    defaultLogSelection,
    dismissReasonsFor,
    emailsToLog,
    isFixedCloseReason,
    isLooseEndActionKind,
    logBatches,
    logButtonLabel,
    logTargetLine,
    looseEndsError,
    looseEndsWriteError,
    medianDaysLabel,
    mutePatternLabel,
    noiseLine,
    scannedAtPhrase,
} = await import('./looseEnds.ts');
const { isForbidden, isNotEnabled } = await import('./cockpit.ts');

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');

const ROUTER_PY = read('backend/routers/loose_ends.py');
const VIEWS_PY = read('rubik_core/loop_closure/views.py');
const CONSTANTS_PY = read('rubik_core/loop_closure/constants.py');
const DB_PY = read('rubik_core/database/loop_closure.py');
const IDENTIFIERS_PY = read('rubik_core/identifiers.py');
const CONTRACT_MD = read('docs/loose_ends/README.md');
const PAGE_TSX = read('frontend/src/pages/sales/LooseEnds.tsx');
const PANEL_TSX = read('frontend/src/pages/sales/LooseEndsPanel.tsx');
const MY_DAY_TSX = read('frontend/src/pages/sales/MyDay.tsx');
const APP_TSX = read('frontend/src/App.tsx');
const LAYOUT_TSX = read('frontend/src/components/Layout.tsx');
const AUTH_TSX = read('frontend/src/context/AuthContext.tsx');
const API_TS = read('frontend/src/api/looseEnds.ts');
const CRM_LOG_PY = read('rubik_core/loop_closure/crm_log.py');
const HUBSPOT_WRITE_PY = read('rubik_core/retrieval/hubspot_write.py');

const SERVER_PY = [ROUTER_PY, VIEWS_PY, CONSTANTS_PY].join('\n');

const unique = (values: string[]): string[] => [...new Set(values)].sort();

/** A stored identifier frozen in `rubik_core/identifiers.py`
 *  (`NAME = "value"`), or the bare name when it is not there — so an entry the
 *  parser cannot resolve fails the comparison instead of vanishing. */
const pyIdentifier = (name: string): string =>
    new RegExp(`^${name}(?:\\s*:[^=\\n]+)?\\s*=\\s*["']([a-z_]+)["']`, 'm').exec(IDENTIFIERS_PY)?.[1] ?? name;

/** The entries of a module-level Python tuple / list / set named `name`
 *  (annotation allowed): `DISMISS_REASONS: tuple[str, ...] = ("a", …)`. An
 *  entry is a string literal or, on a line of its own, a constant frozen in
 *  `rubik_core/identifiers.py`, which is resolved to its value. One level of
 *  alias is followed — constants.py re-exports the vocabularies the table
 *  module defines (`DISMISS_REASONS = LOOP_DISMISS_REASONS`), so the alias is
 *  looked up in the table module too. */
const pyTuple = (source: string, name: RegExp): string[] => {
    const head = `^${name.source}(?:\\s*:[^=\\n]+)?\\s*=\\s*`;
    const literal = new RegExp(`${head}(?:frozenset\\()?[(\\[{]([\\s\\S]*?)[)\\]}]`, 'm').exec(source);
    if (literal) {
        return [...literal[1].matchAll(/["']([a-z_]+)["']|^\s*([A-Z][A-Z0-9_]*),?\s*$/gm)].map(x =>
            x[1] ?? pyIdentifier(x[2]),
        );
    }
    const alias = new RegExp(`${head}([A-Z_][A-Z0-9_]*)\\s*$`, 'm').exec(source);
    if (alias && alias[1] !== name.source) return pyTuple(`${source}\n${DB_PY}`, new RegExp(alias[1]));
    return [];
};

/** A module-level `NAME: dict[str, str] = {"key": "value", …}` as pairs. */
const pyDict = (source: string, name: string): Record<string, string> => {
    const m = new RegExp(`^${name}(?:\\s*:[^=\\n]+)?\\s*=\\s*\\{([\\s\\S]*?)\\n\\}`, 'm').exec(source);
    if (!m) return {};
    return Object.fromEntries([...m[1].matchAll(/"([a-z_]+)":\s*"([^"]+)"/g)].map(x => [x[1], x[2]]));
};

describe('loose-end action kinds', () => {
    // Every way a kind can be written as a literal on the server: a keyword
    // argument (`kind="draft_reply"`), a dict entry (`"kind": "open_link"`), or
    // a named constant (`ACTION_LOG_IN_HUBSPOT = "log_in_hubspot"`). The item
    // kinds (`unlogged` / `open_loop` / `todo`) share the word "kind" and are
    // subtracted — they are rows, not buttons.
    const literals = [
        ...[...SERVER_PY.matchAll(/\bkind\s*=\s*["']([a-z_]+)["']/g)].map(m => m[1]),
        ...[...SERVER_PY.matchAll(/["']kind["']\s*:\s*["']([a-z_]+)["']/g)].map(m => m[1]),
        ...[...SERVER_PY.matchAll(/^[A-Z_]*ACTION[A-Z_]*(?:\s*:\s*str)?\s*=\s*["']([a-z_]+)["']\s*$/gm)].map(
            m => m[1],
        ),
        ...pyTuple(SERVER_PY, /[A-Z_]*ACTION_KINDS/),
    ];
    const itemKinds = new Set<string>(LOOSE_END_KINDS);
    const emitted = unique(literals.filter(k => !itemKinds.has(k)));

    it('finds action kinds in the server at all', () => {
        assert.ok(emitted.length >= 2, `only found ${emitted.length} action kinds: ${emitted.join(', ')}`);
    });

    it('declares every kind the server emits', () => {
        for (const kind of emitted) {
            assert.ok(isLooseEndActionKind(kind), `${kind} is emitted by the server but not declared`);
        }
    });

    it('declares nothing the server never writes', () => {
        for (const kind of LOOSE_END_ACTION_KINDS) {
            assert.ok(
                SERVER_PY.includes(`"${kind}"`) || SERVER_PY.includes(`'${kind}'`),
                `${kind} is declared but no server file writes it`,
            );
        }
    });

    it('handles each kind case by case in LooseEnds.tsx, and nothing else', () => {
        const matches = [...PAGE_TSX.matchAll(/case '([a-z_]+)':/g)].map(m => m[1]);
        assert.ok(matches.length >= 2, `only found ${matches.length} cases`);
        assert.deepEqual(unique(matches), [...LOOSE_END_ACTION_KINDS].sort());
    });

    it('narrows before switching', () => {
        assert.match(PAGE_TSX, /isLooseEndActionKind\(/);
    });
});

describe('dismiss reasons and mute scopes', () => {
    const tuple = pyTuple(CONSTANTS_PY, /DISMISS_REASONS/);

    it('finds the tuple in constants.py', () => {
        assert.ok(tuple.length >= 2, `only found ${tuple.length} dismiss reasons`);
    });

    it('equals the server tuple', () => {
        assert.deepEqual([...DISMISS_REASONS].sort(), [...tuple].sort());
    });

    it('names each reason exactly as the server does', () => {
        const labels = pyDict(CONSTANTS_PY, 'DISMISS_REASON_LABELS');
        assert.ok(Object.keys(labels).length >= 2, 'DISMISS_REASON_LABELS not found in constants.py');
        assert.deepEqual({ ...DISMISS_REASON_LABELS }, labels);
    });

    it('gives every reason a label, and offers every reason for some kind', () => {
        const offered = new Set([...dismissReasonsFor('unlogged'), ...dismissReasonsFor('todo')]);
        for (const reason of DISMISS_REASONS) {
            assert.ok(DISMISS_REASON_LABELS[reason].length > 3 || reason === 'other', reason);
            assert.ok(offered.has(reason), `${reason} is never offered`);
        }
    });

    it('sends the mute scopes the router reads', () => {
        const found = MUTE_SCOPES.filter(s => SERVER_PY.includes(`"${s}"`) || SERVER_PY.includes(`'${s}'`));
        assert.ok(found.length >= 2, `the server mentions ${found.length} of the mute scopes`);
        assert.deepEqual(found, [...MUTE_SCOPES]);
    });

    it('names a domain mute as a domain', () => {
        assert.equal(mutePatternLabel('@acmecorp.example'), 'Everyone at acmecorp.example');
        assert.equal(mutePatternLabel('jane@acmecorp.example'), 'jane@acmecorp.example');
    });
});

describe('close reasons', () => {
    const tuple = pyTuple(CONSTANTS_PY, /[A-Z_]*CLOSE_REASONS/);

    it('finds the close reasons in constants.py', () => {
        assert.ok(tuple.length >= 2, `only found ${tuple.length} close reasons`);
    });

    it('labels every close reason the server can record', () => {
        for (const reason of tuple) {
            assert.ok(reason in CLOSE_REASON_LABELS, `${reason} has no label`);
        }
    });

    it('mirrors the stored close reasons exactly, the frozen one included', () => {
        assert.ok(tuple.includes('logged_from_assistant'), tuple.join(', '));
        assert.deepEqual(Object.keys(CLOSE_REASON_LABELS).sort(), [...tuple].sort());
    });

    it('never calls aging out a fix, and degrades an unknown reason to words', () => {
        assert.match(closeReasonLabel('aged_out'), /not fixed/);
        assert.equal(closeReasonLabel('some_new_reason'), 'some new reason');
    });

    it('never calls a reschedule a fix, and agrees with the server on which are not', () => {
        assert.match(closeReasonLabel('rescheduled'), /not fixed/);
        const unfixed = pyTuple(CONSTANTS_PY, /UNFIXED_REASONS/);
        assert.deepEqual([...unfixed].sort(), [...UNFIXED_CLOSE_REASONS].sort());
        assert.equal(isFixedCloseReason('replied'), true);
        assert.equal(isFixedCloseReason('rescheduled'), false);
    });
});

describe('coverage: "could not look" is never "all clear"', () => {
    // The contract's Coverage line: {"gmail": "ok|unavailable", "slack": …}.
    const states: Array<[string, string]> = [];
    for (const m of CONTRACT_MD.matchAll(/"(gmail|slack|hubspot|ai)":\s*"([a-z_|]+)"/g)) {
        for (const state of m[2].split('|')) if (state !== 'ok') states.push([m[1], state]);
    }

    it('finds the coverage states in the contract', () => {
        assert.ok(states.length >= 6, `only found ${states.length} coverage states`);
    });

    it('has a sentence for every non-ok state', () => {
        for (const [source, state] of states) {
            const copy = (COVERAGE_COPY as Record<string, Record<string, string>>)[source]?.[state];
            assert.ok(copy && copy.length > 20, `${source}:${state} has no sentence`);
        }
    });

    it('admits the gap for a state this build has never heard of', () => {
        const gaps = coverageGaps({ gmail: 'ok', slack: 'ok', hubspot: 'rate_limited', ai: 'ok' });
        assert.equal(gaps.length, 1);
        assert.match(gaps[0].sentence, /rate_limited/);
        assert.match(gaps[0].sentence, /all clear/);
    });

    it('treats ok and missing as no gap', () => {
        assert.deepEqual(coverageGaps({ gmail: 'ok', slack: 'ok', hubspot: 'ok', ai: 'ok' }), []);
        assert.deepEqual(coverageGaps(null), []);
        assert.equal(COVERAGE_SOURCES.length, 4);
    });
});

describe('every number carries its denominator', () => {
    it('prints the scan denominators like the spec example', () => {
        const coverage = {
            gmail: 'ok',
            slack: 'ok',
            hubspot: 'ok',
            ai: 'ok',
            threads_scanned: 52,
            slack_messages_scanned: 31,
            promises_scanned: 4,
            tasks_scanned: 7,
        };
        assert.equal(coverageCounts(coverage), '52 email threads, 31 Slack messages, 4 promises and 7 HubSpot tasks');
    });

    it('counts a partly read mailbox, which did read the threads it counts', () => {
        assert.equal(coverageCounts({ gmail: 'partial', threads_scanned: 300 }), '300 email threads');
        assert.match(coverageGaps({ gmail: 'partial' })[0].sentence, /oldest threads were not checked/);
    });

    it('leaves out a source that was not read rather than printing zero', () => {
        const counts = coverageCounts({
            gmail: 'unavailable',
            slack: 'not_connected',
            hubspot: 'no_owner',
            ai: 'ok',
            threads_scanned: 0,
            slack_messages_scanned: 0,
            promises_scanned: 1,
            tasks_scanned: 0,
        });
        assert.equal(counts, '1 promise');
    });

    it('says when the scan ran, on the reader’s clock', () => {
        const now = new Date(2026, 9, 7, 9, 30);
        const morning = new Date(2026, 9, 7, 7, 4).toISOString().replace('Z', '');
        assert.match(scannedAtPhrase(morning, now), /^this morning at /);
        const afternoon = new Date(2026, 9, 7, 14, 10).toISOString().replace('Z', '');
        assert.match(scannedAtPhrase(afternoon, new Date(2026, 9, 7, 16, 0)), /^today at /);
        const yesterday = new Date(2026, 9, 6, 7, 4).toISOString().replace('Z', '');
        assert.match(scannedAtPhrase(yesterday, now), /^yesterday at /);
        assert.match(scannedAtPhrase(new Date(2026, 9, 1, 7, 4).toISOString(), now), /^on .+ at /);
        assert.equal(scannedAtPhrase(null, now), '');
        const line = coverageLine({ gmail: 'ok', threads_scanned: 1 }, morning, now);
        assert.match(line, /^Checked 1 email thread this morning at .+\.$/);
    });

    it('states the noise rate as numerator of denominator', () => {
        assert.equal(noiseLine({ flagged: 41, dismissed: 3, noise_rate: 0.073 }), '3 of 41 flags marked not a real miss (7%)');
        assert.equal(noiseLine({ flagged: 4, dismissed: 1 }), '1 of 4 flags marked not a real miss (25%)');
        assert.doesNotMatch(noiseLine({ flagged: 0, dismissed: 0 }), /%/);
    });

    it('labels a median honestly', () => {
        assert.equal(medianDaysLabel(2.5), '2.5 days');
        assert.equal(medianDaysLabel(1), '1 day');
        assert.equal(medianDaysLabel(null), null);
    });
});

describe('errors', () => {
    const rejection = (status: number, detail: unknown = 'x') => ({ response: { status, data: { detail } } });

    it('reads a flag-off 404 and a roster 403 as calm states', () => {
        const off = looseEndsError(rejection(404, 'Not Found'));
        assert.ok(isNotEnabled(off));
        assert.match(off.message, /Loose Ends/);
        assert.ok(isForbidden(looseEndsError(rejection(403, 'Not on the roster'))));
    });

    it('never calls a 404 on a write "switched off"', () => {
        const msg = looseEndsWriteError(rejection(404, 'Not Found'));
        assert.match(msg, /no longer on your list/);
        assert.equal(looseEndsWriteError(rejection(502, 'HubSpot refused the note.')), 'HubSpot refused the note.');
    });
});

describe('no leaderboard, no content in the team view', () => {
    it('keeps the server’s A–Z order for people', () => {
        assert.doesNotMatch(PAGE_TSX, /people[^;\n]*\.sort\(/);
        assert.doesNotMatch(PAGE_TSX, /people[^;\n]*\.toSorted\(/);
    });

    it('gives the team item type no title, headline, subject or snippet', () => {
        const src = read('frontend/src/api/looseEnds.ts');
        const team = /export interface LooseEndsTeamItem \{([\s\S]*?)\n\}/.exec(src);
        assert.ok(team, 'LooseEndsTeamItem not found');
        assert.doesNotMatch(team[1], /\b(title|headline|subject|snippet)\??:/);
    });
});

describe('the surface is wired', () => {
    it('registers the page route', () => {
        assert.match(APP_TSX, /path="sales\/loose-ends" element=\{<FeatureRoute feature="copilot"><LooseEnds \/><\/FeatureRoute>\}/);
    });

    it('offers the nav row on the loop_closure verdict, independent of the cockpit probe', () => {
        assert.match(LAYOUT_TSX, /hasFeature\('loop_closure'\)/);
        assert.match(LAYOUT_TSX, /to: [^\n]*'\/sales\/loose-ends'[^\n]*isActive: under\('\/sales\/loose-ends'\)/);
        assert.match(LAYOUT_TSX, /isActive: exact\('\/sales'\)/);
    });

    it('gives a team-only leader the row too, pointed at the Team tab', () => {
        // Emery and Indigo hold loop_closure_team but not loop_closure: no
        // row (and a 403 Mine tab) would leave the digest link as the only door.
        assert.match(LAYOUT_TSX, /canSeeOwnLooseEnds \|\| hasFeature\('loop_closure_team'\)/);
        assert.match(LAYOUT_TSX, /'\/sales\/loose-ends\?view=team'/);
        assert.match(PAGE_TSX, /params\.get\('view'\) === 'team' \|\| !canSeeMine/);
        assert.match(PAGE_TSX, /canSeeTeam && canSeeMine && \(/);
    });

    it('puts the panel on My Day, above the daily rhythm', () => {
        const panel = MY_DAY_TSX.indexOf("hasFeature('loop_closure') && <LooseEndsPanel />");
        // `<DailyRhythm` with no closing: it takes an `onDeferred` prop now.
        const rhythm = MY_DAY_TSX.indexOf('<DailyRhythm');
        assert.ok(panel > 0, 'LooseEndsPanel is not rendered on My Day');
        assert.ok(panel < rhythm, 'LooseEndsPanel must sit above the daily rhythm');
    });

    it('knows both feature keys', () => {
        assert.match(AUTH_TSX, /looseEnds: 'loop_closure',/);
        assert.match(AUTH_TSX, /looseEndsTeam: 'loop_closure_team',/);
        assert.match(PAGE_TSX, /hasFeature\('loop_closure_team'\)/);
    });

    it('self-hides the panel when the feature is off or not for this reader', () => {
        assert.match(PANEL_TSX, /isNotEnabled\(error\) \|\| isForbidden\(error\)/);
    });
});

describe('"Log it in HubSpot" logs your sent emails, never a note', () => {
    it('carries no note text on the wire, in either direction', () => {
        for (const [name, src] of [
            ['looseEnds.ts', API_TS],
            ['LooseEnds.tsx', PAGE_TSX],
            ['views.py', VIEWS_PY],
            ['loose_ends.py', ROUTER_PY],
        ] as const) {
            assert.doesNotMatch(src, /note_preview/, `${name} still mentions note_preview`);
        }
    });

    it('writes HubSpot emails through create_email, and no note path exists', () => {
        assert.match(CRM_LOG_PY, /\.create_email\(/);
        assert.doesNotMatch(CRM_LOG_PY, /create_note|hs_note_body/);
        assert.doesNotMatch(HUBSPOT_WRITE_PY, /def create_note|NOTE_WRITABLE|_ASSOC_NOTE_/);
        assert.match(HUBSPOT_WRITE_PY, /EMAIL_WRITABLE = frozenset\(/);
    });

    it('calls the preview endpoint the router serves', () => {
        assert.match(ROUTER_PY, /"\/\{item_id\}\/log-preview"/);
        assert.match(API_TS, /itemPath\(id, 'log-preview'\)/);
    });

    it('sends message ids and the batch flag, the fields the router reads, and never content', () => {
        assert.match(API_TS, /itemPath\(id, 'log'\), \{ message_ids: messageIds, more \}/);
        assert.match(ROUTER_PY, /message_ids: list\[str\]/);
        assert.match(ROUTER_PY, /more: bool = False/);
    });

    const email = (message_id: string, over: { already_logged?: boolean; to_contact?: boolean } = {}) => ({
        message_id,
        subject: message_id,
        to: [],
        excerpt: '',
        chars: 0,
        truncated: false,
        already_logged: false,
        ...over,
    });

    it('ticks only the emails sent to the contact, and sends only the ticked ones HubSpot lacks', () => {
        assert.match(PAGE_TSX, /setSelected\(new Set\(defaultLogSelection\(data\)\)\)/);
        assert.match(PAGE_TSX, /const toLog = emailsToLog\(preview, selected\)/);
        assert.match(PAGE_TSX, /logBatches\(toLog\)/);
        assert.match(PAGE_TSX, /logLooseEnd\(item\.id, batches\[i\], i < batches\.length - 1\)/);
        assert.match(PAGE_TSX, /type="checkbox"/);
        assert.match(PAGE_TSX, /Attachments are not included\./);
        const preview = {
            emails: [
                email('m1', { already_logged: true, to_contact: true }),
                email('m2', { to_contact: true }),
                email('fwd', { to_contact: false }), // a forward to a partner
                email('m3', { to_contact: true }),
                email('old'), // a server that does not send the flag: not ticked
            ],
        };
        assert.deepEqual(defaultLogSelection(preview), ['m2', 'm3']);
        const ticked = new Set(defaultLogSelection(preview));
        assert.deepEqual(emailsToLog(preview, ticked), ['m2', 'm3']);
        // Ticking the forward adds it; an already-logged email is never sent,
        // ticked or not; order is the preview's, not the ticking's.
        assert.deepEqual(emailsToLog(preview, new Set(['m3', 'fwd', 'm1', 'm2'])), ['m2', 'fwd', 'm3']);
        assert.deepEqual(emailsToLog(preview, new Set()), []);
    });

    it('sends more than the router takes in batches, each within its limit', () => {
        const ids = Array.from({ length: 45 }, (_, i) => `m${i}`);
        const batches = logBatches(ids);
        assert.deepEqual(batches.map(b => b.length), [20, 20, 5]);
        assert.deepEqual(batches.flat(), ids);
        assert.deepEqual(logBatches([]), []);
        assert.deepEqual(logBatches(['a', 'b', 'c'], 2), [['a', 'b'], ['c']]);
        // The client's batch size is the router's cap.
        const cap = /^MAX_EMAILS_PER_LOG = (\d+)$/m.exec(CRM_LOG_PY);
        assert.ok(cap, 'crm_log.py defines MAX_EMAILS_PER_LOG');
        assert.match(API_TS, new RegExp(`export const MAX_EMAILS_PER_LOG = ${cap[1]};`));
    });

    it('prints the body limit the server cuts at, under HubSpot\'s 65,536', () => {
        const cap = /^MAX_BODY_CHARS = ([\d_]+)$/m.exec(CRM_LOG_PY);
        assert.ok(cap, 'crm_log.py defines MAX_BODY_CHARS');
        const chars = Number(cap[1].replace(/_/g, ''));
        assert.ok(chars < 65_536, 'HubSpot rejects a property value over 65,536 characters');
        assert.ok(PAGE_TSX.includes(chars.toLocaleString('en-US')), 'the page prints the server limit');
        assert.doesNotMatch(PAGE_TSX + API_TS + ROUTER_PY, /100,000/);
    });

    it('names where the emails land, and how many the button logs', () => {
        assert.equal(logTargetLine({ contact: 'Jane Doe', company: 'Acme Corp', deal: 'Q3 rollout' }), 'Jane Doe · Acme Corp · Q3 rollout');
        assert.equal(logTargetLine({ contact: 'Jane Doe', company: null }), 'Jane Doe');
        assert.equal(logTargetLine({}), 'the HubSpot contact');
        assert.equal(logButtonLabel(1), 'Log 1 email in HubSpot');
        assert.equal(logButtonLabel(3), 'Log 3 emails in HubSpot');
    });
});
