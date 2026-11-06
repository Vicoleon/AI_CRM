// Cubie Sales Cockpit API client — the four views (AE "My Day", supervisor
// worklist, manager roll-up, exec summary) and the commitment/stage-lag
// actions behind them.
//
// Uses the shared axios instance (bearer token + request id attached by its
// interceptor); full /api/... paths, no baseURL, and no timeout of our own —
// client.ts is 105s, deliberately above the backend's 90s cap.
//
// ── THE ROUTERS ARE THE CONTRACT ────────────────────────────────────────────
// Every wire string and every field in this file was read out of
// `backend/routers/cockpit.py` and `backend/routers/commitments.py`. Those are
// the server: where this file and a router disagreed, THIS FILE WAS WRONG and
// was changed. Do not "tidy" a name here — check the router first. The eight
// action kinds, the `unavailable_reason` codes, the nudge outcome fields and
// the mutation envelopes (`{commitment: …}`, `{exception: …}`,
// `{nudge: …, commitment: …}`) all came from reading the handlers, not the spec.
//
// Reconciled field by field against both routers, and every URL against the
// app's OpenAPI paths. Three things were wrong and are fixed below:
//
//   * `wrapupRoute` pointed at a route `App.tsx` does not register, so My Day's
//     FIRST block — the time-sensitive one — dead-ended on a blank page.
//   * `crm_read_failed` was missing from the `unavailable_reason` set, so a
//     HubSpot read failure rendered as "you are caught up". The router has
//     since folded that case into `crm_unavailable`; the code stays declared
//     anyway, because a client that only knows the codes its own build emits is
//     one rollback away from the same silent lie.
//   * The `was_breached` note claimed there is no `breached_at` column. There
//     is one now; it is simply not serialised.
//
// Everything else matched: every field name, optionality and nullability on
// Item / AgeChip / Block, the manager and exec payloads, and all six mutation
// request and response bodies. `frontend/src/api/cockpit.test.ts` re-derives the
// action kinds, the reason codes and the wrap-up route FROM the Python and the
// route table, so the next drift fails a test instead of a person's morning.
//
// Both routers mount on the copilot-gated `core` parent in `backend/main.py`,
// which supplies the `/api` prefix. `cockpit.py` adds `prefix="/sales"`;
// `commitments.py` adds none, so its paths are `/api/commitments/...` and
// `/api/stage-lag/...`.
//
// SIX THINGS IN HERE ARE LOAD-BEARING AND NOT STYLE CHOICES:
//
// 1. `isNotEnabled`. Every cockpit gate returns 404 while `COCKPIT_ENABLED`
//    is off, and the flags ship defaulting OFF. That is an <EmptyState/>,
//    never an <ErrorState/>. Sources inside the cockpit are separately
//    flagged (`CCS_ENABLED`, `CCS_LEADS_ENABLED`), and the router reports
//    those per BLOCK through `unavailable_reason` rather than by omitting the
//    block — see note 6.
// 2. `AgeState` is a union, not a string: the age chip keys a Record of FULL
//    literal Tailwind class strings off it (the JIT purges anything it cannot
//    see as a literal). `isAgeState` is there so an unrecognised value from a
//    newer backend degrades to the neutral chip instead of rendering unstyled.
//    It has SEVEN members, not four — `cockpit.py::AGE_STATES` is the list, and
//    it asserts at import time that nothing else can reach a chip.
// 3. `CockpitAge.label` is server-rendered and must be printed verbatim.
//    Elapsed time is business-hours arithmetic through a single module
//    (`rubik_core/cockpit/business_hours.py`) — recomputing "6 days" in the
//    browser with `Date.now()` produces the Friday-afternoon false positive
//    that kills trust in week one. Colour is never the only signal; always
//    render the label next to the chip.
// 4. `sla_is_placeholder`. The SLA numbers shipped as placeholders nobody has
//    agreed to. True means dotted underline + "Placeholder — not yet confirmed
//    by the sales manager". Presenting an unagreed number as agreed is how a
//    working system gets called wrong.
// 5. `suppressed_count`. Rule 2 caps a worklist at three items (the numbered
//    anti-tsunami rules cited in this file are listed in
//    `rubik_core/cockpit/digest.py`); the remainder renders as "+4 more" and
//    LINKS OUT. It must not expand inline, or the cap is defeated by a
//    chevron.
// 6. `unavailable_reason` on a block, and `dollar_value_available` /
//    `crm_available` on the aggregates. These are the difference between "you
//    have nothing here" and "Cubie could not look" — a named code, never an
//    empty list or a zero standing in for both. A `0.0` that means "HubSpot
//    was unreadable" must never render as "nothing is at stake".
import client from './client';
import type { DeferTarget } from './dealDefer';

// ─── error shape ─────────────────────────────────────────────────────────────

/** The cockpit routers send `detail: {code, message}` where the UI has to
 *  branch on a machine code — today that is exactly `_authz.require_team_scope`
 *  (409, `org_record_missing` / `team_unresolved`). Everything else sends a
 *  plain-string `detail` that is already a sentence worth showing, and it lands
 *  here with a code of null. Both shapes are handled; do not assume a dict. */
export interface CockpitError {
    code: string | null;
    message: string;
    status: number | null;
}

/** Axios shape, narrowed by hand rather than typed as `any`: the only thing we
 *  trust about a rejection is that it MIGHT carry a response body. */
interface MaybeAxiosError {
    response?: { status?: number; data?: { detail?: unknown } };
    message?: string;
}

/** The code a flag-off 404 is reported as. Not a server value — see below. */
export const NOT_ENABLED = 'not_enabled';

export function cockpitError(e: unknown, fallback = 'The sales cockpit could not be reached.'): CockpitError {
    const err = (e ?? {}) as MaybeAxiosError;
    const status = err.response?.status ?? null;
    const detail = err.response?.data?.detail as
        | { code?: string | null; message?: string }
        | string
        | undefined;

    let code: string | null = null;
    let message = fallback;
    if (detail && typeof detail === 'object') {
        code = detail.code ?? null;
        message = detail.message || fallback;
    } else {
        message = (typeof detail === 'string' && detail) || err.message || fallback;
    }

    // A 404 with no code of its own is the feature switched off: every cockpit
    // gate 404s when `COCKPIT_ENABLED` is false, which is its default
    // (`backend/auth.py::_require_cockpit_enabled`). Mapped here rather than in
    // each screen so no screen can forget and show a red error for a flag. A
    // 404 that DID name a code keeps it — "that commitment is gone" and "the
    // feature is off" are different sentences and the worklist has to tell them
    // apart.
    if (status === 404 && code === null) {
        code = NOT_ENABLED;
        message = 'The sales cockpit is not enabled yet.';
    }

    return { code, message, status };
}

/** The feature (or this view's flag) is off. Render a calm empty state: nothing
 *  about the user's account is wrong and no retry will help. */
export const isNotEnabled = (err: CockpitError): boolean => err.code === NOT_ENABLED;

/** This view is not for this reader (manager/exec/supervisor gating, enforced
 *  in the router — the frontend never re-derives a permission). Distinct from
 *  flag-off, and it deserves different copy. */
export const isForbidden = (err: CockpitError): boolean => err.status === 403;

/** The two machine codes `backend/routers/eos/_authz.py::require_team_scope`
 *  raises as a 409 on the team views. VERBATIM from that function — an earlier
 *  build of this client guessed five other names and matched none of them, so
 *  a missing Accountability-Chart row rendered as a red error instead of the
 *  fixable data problem it is.
 *
 *  * `org_record_missing` — no `eos_people` row for this address at all.
 *  * `team_unresolved`    — the row exists but resolves to nobody but the
 *    caller: no seat, or a led department with nobody seated in it.
 *
 *  Both are a 409 and both carry a `message` that names the fix. NEVER a 200
 *  with an empty list: on a worklist "your team has nothing outstanding" is the
 *  most damaging thing this feature could say wrongly. */
export const TEAM_SCOPE_CODES = ['org_record_missing', 'team_unresolved'] as const;

export type TeamScopeCode = (typeof TEAM_SCOPE_CODES)[number];

/** True when the Accountability Chart could not answer "who is on your team".
 *  Render the server's own `message` — it names the fix — never a retry. */
export const isTeamUnresolved = (err: CockpitError): boolean =>
    err.code !== null && (TEAM_SCOPE_CODES as readonly string[]).includes(err.code);

/** Narrower: no `eos_people` row at all, versus a row that resolves to nobody.
 *  Same fix, different sentence, so the copy can be specific. */
export const isOrgRecordMissing = (err: CockpitError): boolean => err.code === 'org_record_missing';

// ─── shared vocabulary ───────────────────────────────────────────────────────

/** The escalation ladder as the age chip sees it. This is `cockpit.py`'s
 *  `AGE_STATES`, verbatim and complete — the module has an import-time assert
 *  that `_STATE_TO_AGE_STATE` maps to nothing outside it, so these seven are
 *  the closed set.
 *
 *  Three of them are not colours on the chip's four-colour ladder, and each
 *  exists because the honest answer is sometimes not one of the four:
 *
 *  * `untracked`    — no commitment governs the row, so there is no SLA to be
 *    within or past. Elapsed time is shown; no lateness is claimed. Used by the
 *    stage-lag and no-contact rows, and by a wrap-up with no opener yet.
 *  * `unknown`      — `opened_at` IS NULL. The clock exists, its start does
 *    not. Unknown shown as unknown beats unknown shown as zero.
 *  * `exec_visible` — breach + 5 business days. RENDERED LIKE `breached`.
 *
 *  The commitment states `provisional`, `accepted` and `closed` never reach a
 *  chip; the router folds them into `open` / `breached` before serialising.
 *
 *  MUST stay a union: the chip keys a Record of full literal Tailwind class
 *  strings off it. */
export type AgeState =
    | 'untracked'
    | 'unknown'
    | 'open'
    | 'at_risk'
    | 'breached'
    | 'escalated'
    | 'exec_visible';

export const AGE_STATES: readonly AgeState[] = [
    'untracked',
    'unknown',
    'open',
    'at_risk',
    'breached',
    'escalated',
    'exec_visible',
];

/** Narrow before indexing a class Record, so an unknown state from a newer
 *  backend degrades to the neutral chip instead of an unstyled one. */
export const isAgeState = (v: unknown): v is AgeState =>
    typeof v === 'string' && (AGE_STATES as readonly string[]).includes(v);

/** Mirrors `COMMITMENT_KINDS` (rubik_core/database/commitments.py). */
export type CommitmentKind = 'sow' | 'wrapup' | 'lead_touch';

export const COMMITMENT_KIND_LABELS: Record<CommitmentKind, string> = {
    sow: 'SOW',
    wrapup: 'Wrap-up',
    lead_touch: 'Lead first touch',
};

/** Falls back to the raw value: an unknown kind should render as itself, never
 *  as "undefined". */
export const commitmentKindLabel = (kind: string): string =>
    COMMITMENT_KIND_LABELS[kind as CommitmentKind] ?? kind;

/** Mirrors `COMMITMENT_STATES`. Typed as a union for readability only — every
 *  read of a commitment's `state` goes through a plain `string`, because the
 *  ladder's thresholds are config rows and the set can grow without a deploy. */
export type CommitmentState =
    | 'provisional'
    | 'open'
    | 'at_risk'
    | 'breached'
    | 'escalated'
    | 'exec_visible'
    | 'accepted'
    | 'closed';

/** Mirrors `STAGE_LAG_STATUSES`. "rejected" is the tuning signal — the "Not
 *  right" rate per signal type is the honest measure of the detector. */
export type StageLagStatus = 'open' | 'applied' | 'rejected';

/** `cockpit_transcript_policy` (backend/auth.py) — the closed set is
 *  `COCKPIT_TRANSCRIPT_POLICIES`. Returned in the My Day envelope so the UI can
 *  label a metadata-only view honestly instead of rendering an empty quote box.
 *  "none" and "own" are both real answers; do not treat a missing quote as an
 *  error. */
export type TranscriptPolicy = 'team' | 'own' | 'none';

// ─── worklist items (Rule 3: exactly one primary action, and it writes) ──────

export interface CockpitAge {
    /** One of `AGE_STATES`. Typed as the union; narrow with `isAgeState` before
     *  indexing anything off it. */
    state: AgeState;
    /** Server-rendered, business-hours: "6d", "5d · SLA 7d", "9d · 2d over",
     *  "3d since the call", "age unknown". Print it verbatim. */
    label: string;
    /** Named on an `escalated` chip — "with Jordan". Escalation changes who is
     *  accountable, so naming the person IS the information (Rule 4). Absent
     *  when null: every cockpit response sets
     *  `response_model_exclude_none=True`, so an unset optional is MISSING from
     *  the JSON rather than present-and-null. */
    escalated_to?: string | null;
}

/** The action `kind` strings the router actually emits. All eight are grepped
 *  out of `backend/routers/cockpit.py`'s `Action(...)` constructions — there are
 *  exactly eight and this is the closed set:
 *
 *  | kind               | emitted by block   | what the client does           |
 *  |--------------------|--------------------|--------------------------------|
 *  | `review_wrapup`    | wrapups_pending    | navigate to the wrap-up screen |
 *  | `close_commitment` | leads, deals       | POST /commitments/{id}/close   |
 *  | `set_eta`          | leads, deals       | POST /commitments/{id}/eta     |
 *  | `accept_breach`    | deals (past SLA)   | POST /commitments/{id}/accept  |
 *  | `apply_stage`      | pipeline_hygiene   | POST /stage-lag/{id}/apply     |
 *  | `reject_stage`     | pipeline_hygiene   | POST /stage-lag/{id}/reject    |
 *  | `log_outreach`     | no_contact_90_days | open `href` (HubSpot, new tab) |
 *  | `nudge`            | supervisor         | POST /commitments/{id}/nudge   |
 *
 *  ⚠ An earlier build of this file declared `open`, `open_wrapup`, `close`,
 *  `accept`, `stage_lag_apply` and `stage_lag_reject`. Only `set_eta` and
 *  `nudge` matched the server, so six of the eight buttons on My Day fell
 *  through to the "Cubie does not know how to do that" fallback. That is the
 *  reason `isCockpitActionKind` exists and the reason it must stay in front of
 *  every switch: an unrecognised kind has to degrade to a link or a disabled
 *  button that explains itself, never throw. */
export const COCKPIT_ACTION_KINDS = [
    'review_wrapup',
    'close_commitment',
    'set_eta',
    'accept_breach',
    'apply_stage',
    'reject_stage',
    'log_outreach',
    'nudge',
] as const;

export type CockpitActionKind = (typeof COCKPIT_ACTION_KINDS)[number];

export const isCockpitActionKind = (v: unknown): v is CockpitActionKind =>
    typeof v === 'string' && (COCKPIT_ACTION_KINDS as readonly string[]).includes(v);

export interface CockpitAction {
    label: string;
    /** Typed as `string`, deliberately WIDER than `CockpitActionKind`. The
     *  router's `Action.kind` is a bare `str`, so a newer backend can send a
     *  ninth kind; narrowing at the type level would make that a compile error
     *  in the switch instead of the runtime degrade the UI is built for. Call
     *  `isCockpitActionKind` before switching. */
    kind: string;
    /** Populated ONLY for a link OUT of Cubie — today just the HubSpot deal URL
     *  on `log_outreach`, and only when the workspace HubSpot portal id is set (so it can be
     *  null even on that kind). The router deliberately emits no in-app route:
     *  "a path invented here would be a link that silently does nothing". */
    href?: string | null;
}

export interface CockpitItem {
    /** WHICH id depends on `primary_action.kind`, and this is the one place it
     *  matters:
     *    `review_wrapup`                     → a `call_sessions` id
     *    `apply_stage` / `reject_stage`      → a `commitment_stage_lags` id
     *    `log_outreach`                      → a HubSpot deal id (or
     *                                          `deal-{index}` when even that is
     *                                          missing — never POST it anywhere)
     *    everything else                     → a `commitments` id
     *  Posting a session id to /commitments/{id}/close would 404, which is why
     *  the switch keys on the kind and not on the block. */
    id: string;
    title: string;
    subtitle?: string | null;
    /** Absent on a row with no clock. No chip then — do not substitute a grey
     *  "open" one, which would claim a clock exists. Note the router prefers an
     *  `untracked` or `unknown` chip over omitting this, so in practice it is
     *  usually present; the optionality is the guard, not the expectation. */
    age?: CockpitAge | null;
    /** Required and singular (Rule 3). A worklist row with no action does not
     *  belong in a worklist. */
    primary_action: CockpitAction;
    secondary_action?: CockpitAction | null;
    /** The quote behind the row. AE and manager only — the supervisor view
     *  carries commitment METADATA and no transcript text at all. The router
     *  enforces that structurally: `/api/sales/supervisor` selects
     *  `_METADATA_COLUMNS` and `opened_evidence_text` is not in the SELECT.
     *
     *  ⚠ THE FIELD EXISTS ON THIS SHARED TYPE, SO IT COMPILES ON EVERY SCREEN.
     *  `SupervisorWorklist.tsx` must never render it — see the comment there. */
    evidence_text?: string | null;
    /** See header note 4. Null on a row no commitment governs (the router sends
     *  `None`, which `exclude_none` then drops). */
    sla_is_placeholder?: boolean;
    /** The deal this row is about, when the reader may defer it (router
     *  `DeferTarget`, on My Day's deal blocks and the Daily rhythm). Absent on
     *  every other row. Not a second primary action: it renders as a quiet
     *  "Defer" beside the row's own buttons — see `api/dealDefer.ts`. */
    defer?: DeferTarget | null;
}

/** Reasons a block has nothing in it that are NOT "you are caught up".
 *  Verbatim from `backend/routers/cockpit.py` — every string a `Block(...)` is
 *  constructed with, plus every non-null value `_Crm.reason` can hold. An empty
 *  list with no reason genuinely means nothing is outstanding.
 *
 *  ⚠ This set is CLOSED but the client's handling of it is not: an unknown code
 *  must still produce a sentence that admits the gap (see `blockUnavailableCopy`
 *  below), because a code missing from here renders as "you have nothing" —
 *  exactly the lie the field exists to prevent. `crm_read_failed` was missing
 *  for precisely that reason and turned a HubSpot outage into a clean slate. */
export const BLOCK_UNAVAILABLE_REASONS = [
    /** `CCS_ENABLED` is off: there are no call sessions, so no wrap-up can be
     *  owed. Distinguishable from an AE who is fully caught up. */
    'capture_disabled',
    /** `CCS_LEADS_ENABLED` is off — HubSpot's LEAD object is
     *  REQUIRES_REAUTHORIZATION on this portal, so no lead exists to touch. */
    'lead_capture_disabled',
    /** A temperature filter was requested and honestly refused: the HubSpot
     *  lead-temperature property's internal name is still unknown and
     *  inventing one is forbidden. The ROWS still come back — this reason says
     *  the filter did not apply. */
    'lead_temperature_unavailable',
    /** HubSpot could not be read at all (no token, or the client failed to
     *  construct). CRM-sourced blocks degrade; the rest of the page is fine. */
    'crm_unavailable',
    /** DECLARED BUT NOT CURRENTLY EMITTED, and deliberately kept.
     *
     *  `_no_contact_block` reports a failed stale-deal search (`stale_unavailable`
     *  — HubSpot answered, but this block's only source did not) and folds it
     *  into `crm_unavailable`, whose copy is already true of it. An earlier
     *  build of that handler sent `crm_read_failed` instead, and this client did
     *  not declare it: the block then rendered as "you are caught up" when what
     *  had actually happened was that Cubie could not look. Declaring it costs
     *  nothing and means a re-introduction — or an older backend still in front
     *  of a newer frontend — degrades to an honest sentence rather than to a
     *  clean slate. Do not delete it because grep finds no server-side use. */
    'crm_read_failed',
    /** HubSpot answered, but no HubSpot owner matches this person's Workspace
     *  address. Their CRM-sourced blocks are empty for a data reason, not
     *  because the pipeline is clean. */
    'no_hubspot_owner',
    /** The 90-day list came back, but the deals' client company records did
     *  not. The ROWS still come back, judged on each deal's own logged contact
     *  only — like `lead_temperature_unavailable`, this reason rides on rows.
     *  Without it, a deal whose client was emailed last week (logged on the
     *  company, not the deal) would read as quiet with nothing admitting why. */
    'client_records_unavailable',
] as const;

export type BlockUnavailableReason = (typeof BLOCK_UNAVAILABLE_REASONS)[number];

export const isBlockUnavailableReason = (v: unknown): v is BlockUnavailableReason =>
    typeof v === 'string' && (BLOCK_UNAVAILABLE_REASONS as readonly string[]).includes(v);

/** What to say instead of an empty-state line. Fixed copy, because each of
 *  these is "Cubie could not look" and the reader has to be able to tell that
 *  from "there is nothing here". An unrecognised code falls back to a sentence
 *  that at least admits the gap rather than claiming a clean slate. */
export const BLOCK_UNAVAILABLE_COPY: Record<BlockUnavailableReason, string> = {
    capture_disabled: 'Call capture is switched off here, so Cubie is not tracking wrap-ups yet. This is not "you are caught up".',
    lead_capture_disabled: 'Lead capture is switched off here, so Cubie cannot see leads yet. This is not "no leads".',
    lead_temperature_unavailable: 'Cubie cannot filter by temperature yet — the HubSpot field it needs has not been identified, so every lead is shown.',
    crm_unavailable: 'HubSpot could not be read, so this block is unavailable rather than empty. Nothing here means nothing is wrong.',
    crm_read_failed: 'HubSpot answered, but the search behind this block did not finish, so this block is unavailable rather than empty. Do not read it as "every account has been touched".',
    no_hubspot_owner: 'No HubSpot owner matches your Cubie email, so Cubie cannot find your deals. Ask Sales Ops to line the two up.',
    client_records_unavailable: "HubSpot's client company records could not be read this time, so these deals are judged on their own logged contact only. An email logged to someone at the client who is not on the deal is not counted here.",
};

/** The sentence for a block that came back unavailable, or null when the block
 *  is genuinely just empty. */
export const blockUnavailableCopy = (reason: string | null | undefined): string | null => {
    if (!reason) return null;
    if (isBlockUnavailableReason(reason)) return BLOCK_UNAVAILABLE_COPY[reason];
    // A code this build does not know. Say so rather than showing an empty
    // state, which would claim there is nothing to do.
    return `Cubie could not load this block (${reason}). An empty list here does not mean there is nothing outstanding.`;
};

export interface CockpitBlock {
    items: CockpitItem[];
    /** Rule 2: how many qualified beyond the cap. Renders as "+N more" linking
     *  to a fuller view — never an inline expander. */
    suppressed_count: number;
    /** THE DIFFERENCE BETWEEN "you have nothing here" AND "Cubie could not
     *  look" — a named code, never an empty list standing in for both. One of
     *  `BLOCK_UNAVAILABLE_REASONS`; absent (not null) when the block is fine,
     *  because the router sets `response_model_exclude_none=True`. Render it
     *  through `blockUnavailableCopy`. */
    unavailable_reason?: string | null;
}

/** Safe default for a block the server did not send. */
export const EMPTY_BLOCK: CockpitBlock = { items: [], suppressed_count: 0 };

// ─── AE view — "My Day" ──────────────────────────────────────────────────────

/** The AE's own morning order, matching `MyDayBlocks` field order in the
 *  router. The order is load-bearing: a parallel ritual will not survive, so
 *  the page mirrors the sequence the AE already works. Wrap-ups are first
 *  because they are time-sensitive. */
export const MY_DAY_BLOCK_ORDER = [
    'wrapups_pending',
    'leads',
    'pipeline_hygiene',
    'deals',
    'no_contact_90_days',
] as const;

export type MyDayBlockKey = (typeof MY_DAY_BLOCK_ORDER)[number];

export const MY_DAY_BLOCK_LABELS: Record<MyDayBlockKey, string> = {
    wrapups_pending: 'Wrap-ups waiting',
    leads: 'Leads',
    pipeline_hygiene: 'Pipeline hygiene',
    deals: 'Deals',
    no_contact_90_days: 'No contact in 90 days',
};

/** The router declares all five keys REQUIRED on `MyDayBlocks` and reports a
 *  switched-off source through `unavailable_reason` rather than by dropping the
 *  block — so in practice every key arrives. Kept optional per key anyway:
 *  there is no error boundary in this app, so a `blocks.leads.items.map(...)`
 *  on an absent key is a white screen. Read through `myDayBlock`. */
export type MyDayBlocks = { [K in MyDayBlockKey]?: CockpitBlock | null };

export interface MyDay {
    blocks: MyDayBlocks;
    /** How much transcript text this reader may see. "own" for an AE, "team"
     *  for a manager, "none" for anyone else — so a view with no quotes can say
     *  why instead of rendering an empty quote box. */
    transcript_policy?: TranscriptPolicy | string;
    /** False when HubSpot could not be read: CRM ids appear unlabelled ("deal
     *  <an id>") and the CRM-sourced blocks carry a reason. The page still
     *  works — an AE who cannot see their breached SOWs because a name lookup
     *  429'd has lost the feature entirely. */
    crm_available?: boolean;
}

/** The only safe way to read a block. Never index `myDay.blocks` directly. */
export const myDayBlock = (myDay: MyDay | null | undefined, key: MyDayBlockKey): CockpitBlock =>
    myDay?.blocks?.[key] ?? EMPTY_BLOCK;

/** The in-app wrap-up screen, for the `review_wrapup` action. NOT a server
 *  value: the router emits `review_wrapup` with no `href` on purpose, because
 *  "a path invented [server-side] would be a link that silently does nothing".
 *
 *  ⚠ THIS SHAPE IS NOT A CHOICE — it is pinned in two places at once and both
 *  are load-bearing:
 *
 *  * `frontend/src/App.tsx` registers `calls/:sessionId/wrapup`, and nothing
 *    else under `/calls`. An earlier build of this line returned
 *    `/calls/wrapup?session=<id>`, which matches NO registered route: an AE
 *    opened My Day, clicked the one primary action on the block the spec puts
 *    FIRST because it decays, and landed on a blank page. Wrap-ups waiting is
 *    the most visible path in the feature and it dead-ended.
 *  * `backend/routers/calls.py::_WRAPUP_PATH` is `/calls/{call_session_id}/wrapup`,
 *    and it is interpolated onto `FRONTEND_URL` into the body of the HubSpot
 *    call record. So the same shape has to be a live SPA route or the deep link
 *    sitting inside the client's CRM is dead too.
 *
 *  `pages/calls/WrapUp.tsx` reads its session id from `:sessionId`, `:id` OR
 *  `?session=`; `:sessionId` is the canonical one and the only one App.tsx
 *  registers. Check App.tsx and `_WRAPUP_PATH` together before touching this. */
export const wrapupRoute = (sessionId: string): string =>
    `/calls/${encodeURIComponent(sessionId)}/wrapup`;

export async function fetchMyDay(temperature?: string | null): Promise<MyDay> {
    // `temperature` is accepted and honestly refused by the router (the
    // lead-temperature property's internal name is still unknown): the
    // rows come back unfiltered with `unavailable_reason:
    // "lead_temperature_unavailable"` on the leads block. Passed through anyway
    // so the refusal is the SERVER's, reported in the response, rather than the
    // client quietly deciding not to ask.
    const { data } = await client.get<MyDay>('/api/sales/my-day', {
        params: temperature ? { temperature } : undefined,
    });
    return data;
}

// ─── supervisor view — the worklist ──────────────────────────────────────────

/** A worklist, not a dashboard: every row has one action, and that action
 *  writes something. Metadata only — company, owner, age, state. No transcript
 *  text, no exception, enforced by the router's SELECT.
 *
 *  This is NOT a `CockpitBlock`: the router returns `items` /
 *  `suppressed_count` / `team_size` at the top level, with no
 *  `unavailable_reason` — the whole view is one query and its failure mode is
 *  the 409, not an empty block. */
export interface SupervisorWorklist {
    items: CockpitItem[];
    suppressed_count: number;
    /** How many AEs the Accountability Chart resolved for this caller. THE
     *  REASON THIS FIELD EXISTS: a worklist that is empty because the chart is
     *  thin looks identical to a quiet week without it. Note that an
     *  unrestricted caller (admin / org-wide exec) sees every AE's rows while
     *  this number counts only their own resolved scope, so read it as "what
     *  the chart knows", not "how many people are in this list". */
    team_size?: number;
}

export async function fetchSupervisorWorklist(): Promise<SupervisorWorklist> {
    const { data } = await client.get<SupervisorWorklist>('/api/sales/supervisor');
    return data;
}

// ─── manager view ────────────────────────────────────────────────────────────

/** A stat tile. Aggregate reporting is exempt from Rule 3 — a roll-up number
 *  has no single action and must not pretend to. */
export interface CockpitTile {
    label: string;
    /** Pre-formatted by the server ("$1.2M", "3.5 business days", "82%", "—").
     *  The router's `Tile.value` is a `str`, always — "—" is what it sends when
     *  a number could not be computed, so never coerce this to 0. `number` is
     *  tolerated here only so a future bare count does not break the render. */
    value: string | number;
    hint?: string | null;
}

/** Breaches by AE WITH THE REASON, which is the whole point — a count alone is
 *  what the manager already has and cannot act on. */
export interface ManagerBreach {
    owner_name: string;
    /** Never null: the router's `_record_label` falls back to "no CRM record
     *  yet", and to "company 123" / "deal 456" when HubSpot could not be read.
     *  Print it verbatim. */
    company: string;
    /** A `COMMITMENT_KINDS` value, typed as `string` so an added kind renders
     *  as itself through `commitmentKindLabel` instead of failing to compile. */
    kind: string;
    /** Server-rendered business-hours age, same rule as `CockpitAge.label`. */
    age_label: string;
    /** The recorded human reason — an accepted-breach reason, else the reason
     *  given with an ETA. NEVER transcript text (this is an aggregate surface).
     *  Absent means no reason is on file: render "No reason recorded", never a
     *  blank cell that reads as "no problem". */
    reason?: string | null;
}

/** Wrap-up completion rate by AE — a process metric, not a performance score.
 *  Google's Meet API terms forbid using this data for user evaluation, which is
 *  why the exec view has no per-person drill-down at all. The router sorts
 *  these ALPHABETICALLY by name on purpose; do not re-sort by rate. */
export interface WrapupRate {
    owner_name: string;
    /** 0..1, rounded to 3dp. Format at the edge. */
    rate: number;
}

/** Stage-lag exception count by AE. A dedicated array, NOT one
 *  of the generic `tiles` — an earlier build of this client assumed the latter
 *  and dropped the number entirely. Sorted alphabetically by the router, for
 *  the same anti-leaderboard reason as `wrapup_rates`. */
export interface StageLagCount {
    owner_name: string;
    count: number;
}

/** Deals whose stage and evidence disagree, and the money sitting behind the
 *  disagreement. */
export interface ForecastIntegrity {
    count: number;
    dollar_value: number;
    /** False when HubSpot amounts could not be read. A `dollar_value` of 0 then
     *  means "we could not look", NOT "nothing is at stake" — render "—". */
    dollar_value_available: boolean;
}

/** A deal its rep deferred (router `DeferredDealRow`). Off the rep's lists
 *  until `follow_up_on`, listed here so a defer can never hide a deal from the
 *  manager. Ordered by date by the router, never by person. */
export interface ManagerDeferredDeal {
    owner_name: string;
    deal_name: string;
    /** YYYY-MM-DD — when it comes back on the rep's lists. */
    follow_up_on: string;
    /** YYYY-MM-DD. */
    deferred_on: string;
    note: string;
    hubspot_url?: string | null;
}

export interface ManagerView {
    tiles: CockpitTile[];
    breaches: ManagerBreach[];
    wrapup_rates: WrapupRate[];
    stage_lag_counts: StageLagCount[];
    forecast_integrity: ForecastIntegrity;
    /** People the Accountability Chart resolved. Same honesty role as the
     *  supervisor's: an empty roll-up because the chart is thin must not read
     *  as a clear team. */
    team_size?: number;
    /** False when HubSpot could not be read — company labels degrade to ids. */
    crm_available?: boolean;
    /** Every deal the team deferred. Cubie's own table, so an empty list is a
     *  real "none", not a failed read. Optional for an older backend. */
    deferred_deals?: ManagerDeferredDeal[];
}

export async function fetchManagerView(): Promise<ManagerView> {
    const { data } = await client.get<ManagerView>('/api/sales/manager');
    return data;
}

// ─── exec view ───────────────────────────────────────────────────────────────

export interface ExecTrendPoint {
    /** Bucket label as the server chose it — today "YYYY-MM". Do not parse. */
    period: string;
    value: number;
}

/** Deliberately thin: three numbers and a trend, no drill-down by person. Thin
 *  is the feature — it is what stops this becoming the leaderboard Google's
 *  terms warn against. Do not add a per-AE table here; the
 *  manager view exists for that. */
export interface ExecSummary {
    tiles: CockpitTile[];
    trend: ExecTrendPoint[];
    /** False when HubSpot could not be read. The router already renders the
     *  affected tile as "—" with a hint that says so, so this is confirmation
     *  rather than something the page must branch on. */
    crm_available?: boolean;
}

export async function fetchExecSummary(): Promise<ExecSummary> {
    const { data } = await client.get<ExecSummary>('/api/sales/exec');
    return data;
}

// ─── mutations ───────────────────────────────────────────────────────────────
//
// All six return a bare dict, NOT a pydantic model, so nothing is excluded and
// nothing is renamed. Every field below was read off `_commitment_out`,
// `_stage_lag_out` and `rubik_core/cockpit/nudge.py::_result`.
//
// Every field is still optional here. The envelope key (`commitment`,
// `exception`, `nudge`) is what the client branches on, and a screen that hard
// -reads a leaf key breaks the moment the engine grows a column. The LIST is
// the source of truth after a write, not the ack — always refetch.

/** `_commitment_out` (backend/routers/commitments.py). Datetimes are ISO
 *  strings or null; the client never does arithmetic on them. */
export interface CommitmentRecord {
    id?: string;
    kind?: string;
    owner_email?: string;
    hubspot_deal_id?: string | null;
    hubspot_company_id?: string | null;
    state?: CommitmentState | string;
    sla_hours?: number | null;
    opened_at?: string | null;
    promised_at?: string | null;
    promised_reason?: string | null;
    breach_at?: string | null;
    escalated_at?: string | null;
    escalated_to?: string | null;
    accepted_reason?: string | null;
    closed_at?: string | null;
    /** `manual` | `superseded`. There is deliberately no "email" or "stage"
     *  source: sent mail SUGGESTS a close and a human confirms it. */
    closed_source?: string | null;
    /** The one thing `state` cannot say. Closing overwrites the tier, so a
     *  client reading `state === "closed"` cannot otherwise tell a clean
     *  delivery from a late one.
     *
     *  There IS a `breached_at` column now (`rubik_core/database/commitments.py`,
     *  stamped write-once on the crossing by `commitment_engine._advance`), but
     *  `_commitment_out` does not serialise it and this client must not expect
     *  it. `was_breached` is the router's own read of that stamp — with a
     *  tier-based backstop for rows that predate it — and it stays the answer.
     *  If the router ever adds `breached_at` to the payload, add the field here
     *  and still do no arithmetic on it. */
    was_breached?: boolean;
}

/** What `/eta`, `/close`, `/accept` return: `{"commitment": {...}}`.
 *
 *  ⚠ An earlier build of this client expected `{ok, commitment_id, state,
 *  message}`. None of those keys exist. */
export interface CommitmentMutationResult {
    commitment?: CommitmentRecord;
}

/** `_stage_lag_out`. What `/stage-lag/{id}/apply` and `/reject` return under
 *  the `exception` key — NOT `{ok, stage_lag_id, status, message}`. */
export interface StageLagRecord {
    id?: string;
    hubspot_deal_id?: string;
    hubspot_stage_id?: string;
    evidence_stage_id?: string | null;
    evidence_text?: string | null;
    evidence_ref?: string | null;
    confidence?: number | null;
    status?: StageLagStatus | string;
    resolved_by?: string | null;
    resolved_at?: string | null;
}

export interface StageLagMutationResult {
    exception?: StageLagRecord;
}

/** The exec's refinement, and it is the reason the AE has any control at all: with
 *  an ETA set, the clock does not run against a generic SLA. A three-week RFP
 *  is not late on day four.
 *
 *  An ETA EARLIER than the SLA does not shorten the clock — an AE
 *  over-promising to a client never creates an internal breach. So the
 *  form must not warn about "too soon"; there is nothing to warn about. A
 *  back-dated ETA is accepted for the same reason.
 *
 *  `reason` is REQUIRED and a blank/whitespace one is a 422 with a renderable
 *  sentence — the guard is server-side, not only in the form. */
export interface SetEtaRequest {
    /** ISO datetime. FastAPI parses it into a naive-UTC `datetime`. */
    promised_at: string;
    reason: string;
}

export async function setCommitmentEta(
    commitmentId: string,
    body: SetEtaRequest,
): Promise<CommitmentMutationResult> {
    const { data } = await client.post<CommitmentMutationResult>(
        `/api/commitments/${encodeURIComponent(commitmentId)}/eta`,
        body,
    );
    return data;
}

/** Closing is always a human act recording `source: "manual"`. There is no
 *  email close source and no stage close source — sent-mail evidence SUGGESTS
 *  a close and a person confirms it. A wrong auto-close hides a real breach,
 *  which is the precise failure this feature exists to fix.
 *
 *  `note` has no column on `commitments`; the router records it in the audit
 *  row, which is in any case the durable answer to "who closed this, and why".
 *  The whole body is optional server-side. */
export interface CloseCommitmentRequest {
    note?: string;
}

export async function closeCommitment(
    commitmentId: string,
    body: CloseCommitmentRequest = {},
): Promise<CommitmentMutationResult> {
    const { data } = await client.post<CommitmentMutationResult>(
        `/api/commitments/${encodeURIComponent(commitmentId)}/close`,
        body,
    );
    return data;
}

/** Accepting a breach requires a reason, and the reason is stored — it is the
 *  audit trail the supervisor keeps by hand today.
 *
 *  The API returns 422 on a blank reason and that is deliberate: the guard is
 *  server-side, not only in the form. Disable the submit button too, and
 *  surface `cockpitError(e).message` if a 422 still comes back — never swallow
 *  it, or the AE sees a spinner stop and assumes it saved.
 *
 *  Only an ALREADY-BREACHED commitment may be accepted (`breached`,
 *  `escalated`, `exec_visible`); anything else is a 409 whose `detail` is a
 *  sentence explaining why. Accepting earlier would freeze a live clock. */
export interface AcceptBreachRequest {
    reason: string;
}

export async function acceptCommitmentBreach(
    commitmentId: string,
    body: AcceptBreachRequest,
): Promise<CommitmentMutationResult> {
    const { data } = await client.post<CommitmentMutationResult>(
        `/api/commitments/${encodeURIComponent(commitmentId)}/accept`,
        body,
    );
    return data;
}

// ─── the nudge, and why it needs its own vocabulary ─────────────────────────

/** Operational refusals — a 200 with `sent: false` and one of these. They are
 *  NOT errors: each is information the worklist should render, and each is a
 *  different sentence. Verbatim from `rubik_core/cockpit/nudge.py`. */
export const NUDGE_REFUSAL_REASONS = [
    /** Rule 4: two unacknowledged nudges is an ESCALATION, not a third nudge.
     *  Escalation changes who is accountable; it does not increase volume. The
     *  button must stop offering itself after this. */
    'escalation_required',
    /** The commitment closed between the render and the click. */
    'already_closed',
    /** No HubSpot deal AND no company on the row, so a task would attach to
     *  nothing and be invisible in the AE's context. Resolve the CRM record
     *  first; the clock keeps running either way. */
    'no_crm_record',
] as const;

export type NudgeRefusalReason = (typeof NUDGE_REFUSAL_REASONS)[number];

/** What happened to the Slack half, as its own named state. A 200 DOES NOT
 *  PROVE A DM WAS SENT. Verbatim from `nudge.py`'s `REASON_SLACK_*`. */
export const NUDGE_SLACK_STATES = [
    'delivered',
    /** `SlackBotClient.send_dm` returned None — the AE is not in the Slack
     *  workspace. A SKIP, not a failure: the HubSpot task landed and the nudge
     *  is on the record. Say "task created, Slack skipped". */
    'not_a_slack_workspace_member',
    /** No Slack bot token in this environment. */
    'slack_bot_unavailable',
    /** Slack was reachable and refused; `slack_error` carries its code. */
    'failed',
] as const;

export type NudgeSlackState = (typeof NUDGE_SLACK_STATES)[number];

/** `nudge.py::_result` — the body under the `nudge` key.
 *
 *  ⚠ An earlier build of this client expected `slack_sent: boolean`. THERE IS
 *  NO SUCH FIELD. `sent` is whether the NUDGE happened (the HubSpot task
 *  landed); `slack` is the separate named state of the DM. Reading `sent` as
 *  "the DM went out" is exactly the trap that had an existing Slack follow-up
 *  path reporting success while sending nothing. */
export interface NudgeOutcome {
    commitment_id?: string;
    from_email?: string;
    to_email?: string;
    /** True only when the HubSpot task was created. False with a `reason` from
     *  `NUDGE_REFUSAL_REASONS` on an operational refusal. A genuinely FAILED
     *  external write is a 502, never a `sent: false`, so the two are never
     *  confused. */
    sent?: boolean;
    reason?: NudgeRefusalReason | string | null;
    /** Present on success. */
    nudge_id?: string;
    /** `both` (task + DM) or `hubspot_task` (task only). What was actually
     *  recorded on the `commitment_nudges` row. */
    channel?: string | null;
    hubspot_task_id?: string | null;
    /** One of `NUDGE_SLACK_STATES`. Present on success only. */
    slack?: NudgeSlackState | string | null;
    /** Slack's own error code when `slack === "failed"`. */
    slack_error?: string | null;
    /** Sent-and-unacknowledged nudges on this commitment INCLUDING this one on
     *  success, or the blocking count on `escalation_required`. This is what
     *  makes "nudged twice, still open" a trigger rather than somebody's
     *  memory. */
    unacknowledged_nudges?: number;
    /** Who it escalates to, on `escalation_required`. */
    escalated_to?: string | null;
}

/** `POST /api/commitments/{id}/nudge` → `{"nudge": {...}, "commitment": {...}}`.
 *  The commitment comes back so the row's state can be re-read without a
 *  refetch; the list is still the source of truth. */
export interface NudgeResult {
    nudge?: NudgeOutcome;
    commitment?: CommitmentRecord;
}

/** A nudge writes a HubSpot task to the AE, sends a Slack DM, and logs a nudge
 *  row. The log is the point: it turns "I told them" into a record, and makes
 *  "nudged twice, still open" an escalation trigger rather than a memory.
 *
 *  Takes no body. Render what `result.nudge` actually reports — never a blind
 *  "Nudged" — through `nudgeOutcomeMessage` in `components/sales/index.ts`. */
export async function nudgeCommitment(commitmentId: string): Promise<NudgeResult> {
    const { data } = await client.post<NudgeResult>(
        `/api/commitments/${encodeURIComponent(commitmentId)}/nudge`,
    );
    return data;
}

/** Apply the stage Cubie believes the evidence supports, to HubSpot. One click
 *  from the AE's pipeline-hygiene block. Takes no body.
 *
 *  Two failures worth telling apart: 422 when the exception has no target stage
 *  (a "verbal agreement" signal maps to no stage on this portal — the router
 *  then offers "Not right" as the primary action instead), and 502 when the
 *  HubSpot write itself failed, whose `detail` already names the address Cubie
 *  acted as. Both come back through `cockpitError`. */
export async function applyStageLag(stageLagId: string): Promise<StageLagMutationResult> {
    const { data } = await client.post<StageLagMutationResult>(
        `/api/stage-lag/${encodeURIComponent(stageLagId)}/apply`,
    );
    return data;
}

/** "Not right." The reason is optional and worth asking for anyway — the
 *  rejection rate per signal type is the only honest measure of whether the
 *  detector helps, and a detector that cries wolf teaches the AE to dismiss
 *  without reading. The reason has no column; it lands in the audit row. */
export interface RejectStageLagRequest {
    reason?: string;
}

export async function rejectStageLag(
    stageLagId: string,
    body: RejectStageLagRequest = {},
): Promise<StageLagMutationResult> {
    const { data } = await client.post<StageLagMutationResult>(
        `/api/stage-lag/${encodeURIComponent(stageLagId)}/reject`,
        body,
    );
    return data;
}
