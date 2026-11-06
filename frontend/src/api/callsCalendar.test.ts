// Contract tests for the CALENDAR half of the Sales Call Capture client.
//
//   cd <repo>/frontend && node --test "src/api/**/*.test.ts"
//
// (The bare directory form works on Node 22 but not on Node 24, which tries to
// load the path as a module — pass the glob. `npx tsc -b` covers this file too,
// through tsconfig.test.json, so it cannot rot silently.)
//
// WHY THIS SCREEN NEEDS TESTS OF THIS SHAPE
// ─────────────────────────────────────────
// The calendar screen's PRIMARY job is to bind a meeting to the right HubSpot
// company before the call, and its secondary job — arming transcription — is
// best-effort by nature: a running conference cannot be armed, a client-hosted
// space is in another tenant, and a recurring series shares ONE space. So the
// screen renders two verdicts whose vocabularies are closed SERVER-side, in
// `rubik_core/call_capture/calendar_scan.py`, and every failure worth catching
// here is a SILENT one:
//
//   * A vocabulary drifting. `state`, `reason` and `match` are bare `str` on the
//     wire. A code this build has never heard of must degrade to a neutral
//     badge; a code this client names but the server never sends is copy that
//     can never appear. Both are invisible in a browser — a badge that says
//     nothing looks like a badge with nothing to say.
//   * `candidates[0]`. The server withholds `hubspot_company_id` for every
//     non-unique match precisely so no client can pick a company by list order.
//     A single `candidates[0]` anywhere in the UI silently rebuilds the bug and
//     files a call note against the wrong client — which the AE cannot see from
//     the wrap-up.
//   * `calendar_available` defaulting the wrong way. "You have no client calls
//     today" is an answer an AE BELIEVES and acts on by not capturing a real
//     call, so a missing or malformed envelope has to read as "we could not
//     look".
//
// Each is re-derived FROM the other side (the Python source, the components' own
// source) rather than restated as a constant this file could get equally wrong.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

// `api/calls.ts` imports `./client`, extensionless, the way every module in this
// app does — a specifier Vite resolves and bare Node does not. Teach the runner
// Vite's rule, and only for our own files: an unscoped hook also rewrites the
// CJS `require`s inside node_modules and breaks axios's dependency tree.
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
    ARMING_REASONS,
    ARMING_STATES,
    CAPTURE_STATES,
    COMPANY_MATCH_STATES,
    TENANT_WIDE_ARMING_REASONS,
    canPrepMeeting,
    checklistsFor,
    companyOptionLabel,
    externalAttendees,
    hasCallSession,
    isTenantWideArmingReason,
    tenantWideArming,
} = await import('./calls.ts');
type ScheduledMeeting = import('./calls.ts').ScheduledMeeting;
type ScriptSection = import('./calls.ts').ScriptSection;

const { armingCopy, companyPrompt, isImminent, startsInLabel } = await import(
    '../components/calls/verdictCopy.ts'
);

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string =>
    fs.readFileSync(path.join(REPO, relative), 'utf8');

/** The source with its PROSE removed — whole-line `//` comments and `/* … *\/`
 *  blocks, JSX comments included. The `candidates[0]` check below is a ban on
 *  the CODE, and these files explain the banned pattern by name in their header
 *  comments; matching that explanation would make the guard fire on the
 *  documentation of the thing it protects. */
const codeOnly = (source: string): string =>
    source
        .replace(/\/\*[\s\S]*?\*\//g, ' ')
        .split('\n')
        .filter(line => !/^\s*\/\//.test(line))
        .join('\n');

const SCAN_PY = read('rubik_core/call_capture/calendar_scan.py');
const CALLS_TS = read('frontend/src/api/calls.ts');
const ROW_TSX = codeOnly(read('frontend/src/components/calls/MeetingRow.tsx'));
const PAGE_TSX = codeOnly(read('frontend/src/pages/calls/StartCall.tsx'));

/** The string members of a python `NAME = frozenset({...})`, however it is
 *  wrapped across lines. Read from the source rather than restated, so a member
 *  added on the server fails this file instead of silently never rendering. */
const frozensetMembers = (source: string, name: string): string[] => {
    const match = new RegExp(`${name} = frozenset\\(\\s*\\{([\\s\\S]*?)\\}\\s*\\)`).exec(
        source,
    );
    assert.ok(match, `could not find ${name} in calendar_scan.py`);
    return [...match[1].matchAll(/"([a-z_]+)"/g)].map(m => m[1]).sort();
};

const meeting = (over: Partial<ScheduledMeeting> = {}): ScheduledMeeting => ({
    google_event_id: 'evt_1',
    recurring_event_id: null,
    title: 'Acme Corp — onboarding scope',
    start: '2026-08-28T14:00:00-05:00',
    end: '2026-08-28T14:30:00-05:00',
    start_local: 'Friday Aug 28, 2:00 PM CDT',
    starts_in_minutes: 30,
    is_live: false,
    meet_uri: 'https://meet.google.com/abc-defg-hij',
    meeting_code: 'abc-defg-hij',
    organizer_email: 'ae@acme.example',
    organizer_is_me: true,
    rsvp: 'accepted',
    attendees: [],
    external_domains: ['globex.example'],
    attendees_truncated: false,
    company: {
        match: 'unique',
        hubspot_company_id: '123',
        name: 'Globex LLC',
        domain: 'globex.example',
        candidates: [],
    },
    call_session_id: null,
    capture_state: 'not_started',
    arming: { state: 'unknown', reason: 'not_checked_yet', detail: null },
    ...over,
});

// ─── the three closed vocabularies ───────────────────────────────────────────

describe('calendar vocabularies', () => {
    it('declares exactly the arming states the server can send', () => {
        assert.deepEqual(
            [...ARMING_STATES].sort(),
            frozensetMembers(SCAN_PY, 'ARMING_STATES'),
        );
    });

    it('declares exactly the arming reasons the server can send', () => {
        // Both directions on purpose. A reason the server gained and this client
        // has not heard of renders a neutral badge nobody can act on; a reason
        // this client names and the server never sends is copy that can never
        // appear, and the second is the one code review does not catch.
        assert.deepEqual(
            [...ARMING_REASONS].sort(),
            frozensetMembers(SCAN_PY, 'ARMING_REASONS'),
        );
    });

    it('declares exactly the capture states the server can send', () => {
        assert.deepEqual(
            [...CAPTURE_STATES].sort(),
            frozensetMembers(SCAN_PY, 'CAPTURE_STATES'),
        );
    });

    it('declares exactly the company match states the server can send', () => {
        assert.deepEqual(
            [...COMPANY_MATCH_STATES].sort(),
            frozensetMembers(SCAN_PY, 'COMPANY_MATCH_STATES'),
        );
    });

    it('keeps the tenant-wide reasons inside the real vocabulary', () => {
        // The page states these once above the list instead of on every row. A
        // typo here would suppress nothing and explain nothing.
        for (const reason of TENANT_WIDE_ARMING_REASONS) {
            assert.ok(
                (ARMING_REASONS as readonly string[]).includes(reason),
                `${reason} is not an arming reason the server sends`,
            );
            assert.ok(isTenantWideArmingReason(reason));
        }
        assert.equal(isTenantWideArmingReason('external_host'), false);
    });
});

// ─── the arming badge ────────────────────────────────────────────────────────

describe('arming copy', () => {
    it('gives every declared reason its own label', () => {
        const labels = new Set<string>();
        for (const reason of ARMING_REASONS) {
            const copy = armingCopy({ state: 'unknown', reason, detail: null }, {
                hasSession: false,
            });
            assert.ok(copy.label.trim(), `${reason} has no label`);
            labels.add(copy.label);
        }
        // Distinct copy per reason, so two different situations never read as
        // the same one. `not_checked_yet` is the only overloaded reason and it
        // is disambiguated by `hasSession`, not by its label.
        assert.equal(labels.size, ARMING_REASONS.length);
    });

    it('never renders a not_armable row as armed', () => {
        for (const reason of ARMING_REASONS) {
            const copy = armingCopy({ state: 'not_armable', reason, detail: null }, {
                hasSession: false,
            });
            // The prohibition, stated as a test: an unarmable row may look
            // amber, blue or grey — never green, which is the only tone that
            // means "a transcript is coming".
            if (reason !== 'armed_by_cubie' && reason !== 'already_on') {
                assert.notEqual(
                    copy.tone,
                    'green',
                    `not_armable/${reason} reads as armed`,
                );
            }
        }
    });

    it('degrades a reason this build has never heard of', () => {
        const copy = armingCopy(
            { state: 'not_armable', reason: 'host_left_the_building', detail: null },
            { hasSession: false },
        );
        assert.equal(copy.tone, 'amber');
        assert.ok(copy.label.trim());
        assert.notEqual(copy.tone, 'green');
    });

    it('degrades a state this build has never heard of', () => {
        const copy = armingCopy({ state: 'quantum', reason: 'quantum', detail: null }, {
            hasSession: false,
        });
        assert.equal(copy.tone, 'gray');
        assert.ok(copy.label.trim());
    });

    it('splits not_checked_yet on whether a session exists', () => {
        // Before a session: nobody has asked Google, because GET /scheduled
        // performs no Meet writes. After one: arming is attempted exactly once,
        // at Start, and the only path that STORES this verdict is the
        // confidential one — so "not checked yet" there would imply something
        // may still happen.
        const pending = armingCopy(
            { state: 'unknown', reason: 'not_checked_yet', detail: null },
            { hasSession: false },
        );
        const settled = armingCopy(
            { state: 'unknown', reason: 'not_checked_yet', detail: null },
            { hasSession: true },
        );
        assert.notEqual(pending.label, settled.label);
        assert.match(pending.label, /not checked/i);
        assert.match(settled.label, /not transcribed/i);
    });

    it('says nothing at all when there is no verdict', () => {
        assert.ok(armingCopy(null, { hasSession: false }).label.trim());
    });
});

// ─── the company verdict ─────────────────────────────────────────────────────

describe('company prompt', () => {
    it('asks nothing only when the match is unique', () => {
        assert.equal(companyPrompt(meeting()), null);
        for (const match of COMPANY_MATCH_STATES) {
            const prompt = companyPrompt(
                meeting({
                    company: {
                        match,
                        hubspot_company_id: match === 'unique' ? '123' : null,
                        name: match === 'unique' ? 'Globex' : null,
                        domain: 'globex.example',
                        candidates: [],
                    },
                }),
            );
            if (match === 'unique') {
                assert.equal(prompt, null);
            } else {
                assert.ok(prompt, `${match} renders no ask`);
                assert.ok(prompt.headline.trim(), `${match} has no headline`);
                assert.ok(prompt.hint.trim(), `${match} has no hint`);
            }
        }
    });

    it('names the domain that was tried on not_found', () => {
        const prompt = companyPrompt(
            meeting({
                company: {
                    match: 'not_found',
                    hubspot_company_id: null,
                    name: null,
                    domain: 'acme-partners.com',
                    candidates: [],
                },
            }),
        );
        assert.ok(prompt);
        assert.match(prompt.headline, /acme-partners\.com/);
    });

    it('never says "no company" when the lookup itself failed', () => {
        // `error` and `not_found` are the pair most likely to be collapsed, and
        // collapsing them sends an AE to create a company that already exists.
        const failed = companyPrompt(
            meeting({
                company: {
                    match: 'error',
                    hubspot_company_id: null,
                    name: null,
                    domain: 'globex.example',
                    candidates: [],
                },
            }),
        );
        assert.ok(failed);
        assert.match(failed.headline, /could not check HubSpot/i);
        assert.doesNotMatch(failed.headline, /no HubSpot company/i);
    });

    it('reads a personal address as "pick the company", never as internal', () => {
        // A blocked domain suppresses the MATCH, never the ROW: a client on
        // gmail.com is a real client call.
        const prompt = companyPrompt(
            meeting({
                external_domains: [],
                attendees: [
                    {
                        email: 'parker@gmail.com',
                        display_name: null,
                        is_organizer: false,
                        is_self: false,
                        is_external: true,
                        is_optional: false,
                        response_status: 'accepted',
                    },
                ],
                company: {
                    match: 'skipped',
                    hubspot_company_id: null,
                    name: null,
                    domain: null,
                    candidates: [],
                },
            }),
        );
        assert.ok(prompt);
        assert.match(prompt.headline, /parker@gmail\.com/);
        assert.match(prompt.headline, /pick the company/i);
        assert.doesNotMatch(`${prompt.headline} ${prompt.hint}`, /internal/i);
    });

    it('gives a truncated invite its own sentence', () => {
        const prompt = companyPrompt(
            meeting({
                attendees_truncated: true,
                company: {
                    match: 'ambiguous',
                    hubspot_company_id: null,
                    name: null,
                    domain: 'globex.example',
                    candidates: [],
                },
            }),
        );
        assert.ok(prompt);
        assert.match(prompt.headline, /truncated/i);
    });
});

// ─── the candidates[0] prohibition ───────────────────────────────────────────

describe('no company is ever picked by list order', () => {
    it('never indexes into candidates anywhere in the calendar UI', () => {
        for (const [name, source] of [
            ['MeetingRow.tsx', ROW_TSX],
            ['StartCall.tsx', PAGE_TSX],
        ] as const) {
            assert.doesNotMatch(
                source,
                /candidates\s*\[\s*\d/,
                `${name} indexes into candidates — the server withholds the id for a reason`,
            );
        }
    });

    it('only pre-selects a company on a unique match', () => {
        // The row derives its suggestion from `match === 'unique'` AND a
        // non-null id. Pinned on the source because the alternative — a
        // component render harness — is not what this repo has.
        assert.match(ROW_TSX, /match === 'unique' && company\.hubspot_company_id/);
    });
});

// ─── the envelope ────────────────────────────────────────────────────────────

describe('calendar availability', () => {
    it('defaults to "we could not look", never to "you have no calls"', () => {
        // A malformed or missing envelope must NOT read as an empty day: that is
        // the answer an AE acts on by not capturing a call they really have.
        assert.match(CALLS_TS, /calendar_available: data\?\.calendar_available === true/);
    });

    it('renders the outage and the empty day as different screens', () => {
        assert.match(PAGE_TSX, /could not read your calendar/i);
        assert.match(PAGE_TSX, /No client calls on your calendar today/i);
        // And the outage copy must deny the reading it would otherwise invite.
        assert.match(PAGE_TSX, /This is not an empty day/i);
    });
});

// ─── row helpers ─────────────────────────────────────────────────────────────

describe('row helpers', () => {
    it('treats an unknown capture state as "there is a session"', () => {
        assert.equal(hasCallSession(meeting()), false);
        assert.equal(hasCallSession(meeting({ capture_state: 'wrapped' })), true);
        assert.equal(hasCallSession(meeting({ call_session_id: 'cs_1' })), true);
        // A state from a newer backend must not re-offer Capture.
        assert.equal(hasCallSession(meeting({ capture_state: 'transcribing' })), true);
    });

    it('drops self and colleagues from the external guest list', () => {
        const rows = externalAttendees(
            meeting({
                attendees: [
                    {
                        email: 'ae@acme.example',
                        display_name: 'AE',
                        is_organizer: true,
                        is_self: true,
                        is_external: false,
                        is_optional: false,
                        response_status: 'accepted',
                    },
                    {
                        email: 'parker@globex.example',
                        display_name: 'Parker',
                        is_organizer: false,
                        is_self: false,
                        is_external: true,
                        is_optional: true,
                        response_status: 'accepted',
                    },
                ],
            }),
        );
        assert.deepEqual(
            rows.map(r => r.email),
            ['parker@globex.example'],
        );
    });

    it('withholds Prep from a declined event', () => {
        assert.equal(canPrepMeeting(meeting()), true);
        assert.equal(canPrepMeeting(meeting({ rsvp: 'declined' })), false);
        assert.equal(canPrepMeeting(meeting({ google_event_id: '' })), false);
    });

    it('reads the countdown off the server, signed', () => {
        assert.equal(startsInLabel(meeting({ starts_in_minutes: 12 })), 'In 12 minutes');
        assert.equal(
            startsInLabel(meeting({ starts_in_minutes: -4 })),
            'Started 4 minutes ago',
        );
        assert.equal(startsInLabel(meeting({ is_live: true })), 'Live now');
        assert.equal(startsInLabel(meeting({ starts_in_minutes: null })), null);
        assert.equal(isImminent(meeting({ starts_in_minutes: 90 })), false);
        assert.equal(isImminent(meeting({ starts_in_minutes: 5 })), true);
        assert.equal(isImminent(meeting({ is_live: true, starts_in_minutes: -30 })), true);
    });

    it('labels a company by name, then domain, then the bare id', () => {
        assert.equal(
            companyOptionLabel({
                hubspot_company_id: '9',
                name: 'Acme',
                domain: 'acmecorp.example',
                client_status: '',
            }),
            'Acme',
        );
        assert.equal(
            companyOptionLabel({
                hubspot_company_id: '9',
                name: '',
                domain: 'acmecorp.example',
                client_status: '',
            }),
            'acmecorp.example',
        );
        // The manual-ID path binds a company with neither, and the row must not
        // render a blank button for it.
        assert.equal(
            companyOptionLabel({
                hubspot_company_id: '9',
                name: '',
                domain: '',
                client_status: '',
            }),
            '9',
        );
    });

    it('states a tenant-wide arming answer once, from the first row that has it', () => {
        const day = [
            meeting({ google_event_id: 'a' }),
            meeting({
                google_event_id: 'b',
                arming: {
                    state: 'not_armable',
                    reason: 'arming_unsupported',
                    detail: 'Not available for this Workspace edition.',
                },
            }),
        ];
        const verdict = tenantWideArming(day);
        assert.ok(verdict);
        assert.equal(verdict.reason, 'arming_unsupported');
        // A per-meeting reason is NOT tenant-wide and must stay on its row.
        assert.equal(
            tenantWideArming([
                meeting({
                    arming: { state: 'not_armable', reason: 'external_host', detail: null },
                }),
            ]),
            null,
        );
        assert.equal(tenantWideArming([]), null);
        assert.equal(tenantWideArming(null), null);
    });
});

// ─── the pre-call checklist ──────────────────────────────────────────────────
//
// The failure pinned here was invisible from inside any one layer: the server
// sent `sections` for what was REMEMBERED for the client, the picker offered
// everything in `available`, and the panel resolved a fresh pick against
// `sections` alone. So a first-ever pick read "No approved checklist for this
// one yet" directly under a chip saying "5 facts to cover", and the checklist
// only ever appeared from the second call with a client on.

const section = (product_key: string, facts: string[]): ScriptSection => ({
    script_id: `s_${product_key}`,
    product_key,
    name: product_key,
    items: facts.map(fact_key => ({
        fact_key,
        prompt_text: `Ask about ${fact_key}`,
        required: true,
        help_text: null,
    })),
});

describe('the pre-call checklist', () => {
    const IP = section('implementation_plan', ['seat_count', 'hours_per_week']);
    const VR = section('vendor_review', ['vendor_modules']);

    it('shows a first-ever pick its checklist, from the day catalogue', () => {
        // Nothing is remembered for this client, so `sections` is empty by
        // definition — the catalogue is the only place the checklist can be.
        assert.deepEqual(checklistsFor(['implementation_plan'], [], [IP, VR]), [IP]);
    });

    it('keeps the AE’s pick order across remembered and fresh picks', () => {
        // VR is remembered (the row's section); IP was just ticked (catalogue).
        assert.deepEqual(
            checklistsFor(['vendor_review', 'implementation_plan'], [VR], [IP, VR]),
            [VR, IP],
        );
    });

    it('leaves out a pick nothing resolves, so the panel can say so', () => {
        // A remembered product whose checklist was retired stays picked, and
        // PreCallPanel renders its "no approved checklist" section for it.
        assert.deepEqual(checklistsFor(['retired_product', 'implementation_plan'], [], [IP]), [IP]);
        // Nothing picked is nothing shown, however much the catalogue holds.
        assert.deepEqual(checklistsFor([], [IP], [IP, VR]), []);
    });

    it('falls back to the row’s own sections when there is no catalogue', () => {
        // A failed read, or a server older than the field: the old behaviour.
        assert.deepEqual(checklistsFor(['implementation_plan'], [IP], null), [IP]);
        assert.deepEqual(checklistsFor(['vendor_review'], [IP], null), []);
    });

    it('lets the row’s own section win a tie', () => {
        const remembered: ScriptSection = { ...IP, script_id: 's_row' };
        assert.deepEqual(checklistsFor(['implementation_plan'], [remembered], [IP]), [remembered]);
    });

    it('is what the row hands the panel, fed by the page’s one catalogue', () => {
        // Source-level, because the whole bug was ONE prop.
        assert.match(
            ROW_TSX,
            /sections=\{checklistsFor\(selected, scripts\.sections, scriptCatalogue\)\}/,
        );
        assert.doesNotMatch(ROW_TSX, /sections=\{scripts\.sections\}/);
        assert.match(PAGE_TSX, /const scriptCatalogue = day\?\.script_catalogue \?\? null;/);
        assert.match(PAGE_TSX, /scriptCatalogue=\{scriptCatalogue\}/);
    });

    it('reads the envelope key the server sends, and a missing one as none', () => {
        // A renamed key would fail nowhere else: the client would read
        // `undefined`, fall back to `sections`, and the bug would be back.
        assert.match(SCAN_PY, /"script_catalogue": script_catalogue,/);
        assert.match(CALLS_TS, /const catalogue = data\?\.script_catalogue;/);
        assert.match(
            CALLS_TS,
            /script_catalogue: Array\.isArray\(catalogue\)\s*\?\s*catalogue\.filter\(sec => Boolean\(sec\?\.product_key\)\)\s*:\s*null,/,
        );
    });
});
