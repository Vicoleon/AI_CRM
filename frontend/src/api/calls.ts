// Sales Call Capture (CCS) API client — start a captured call, read the wrap-up
// proposals, approve them.
//
// Uses the shared axios instance (bearer token + request id attached by its
// interceptor); full /api/... paths, no baseURL, and no timeout of our own —
// client.ts is 105s, deliberately above the backend's 90s cap.
//
// EVERY TYPE IN HERE IS TRANSCRIBED FROM backend/routers/calls.py. The router is
// the server, so where the two ever disagree again the router wins and this file
// changes.
//
// SIX THINGS IN HERE ARE LOAD-BEARING AND NOT STYLE CHOICES:
//
// 1. The router sends `detail` as a BARE STRING, not `{code, message}` — there
//    is no error envelope anywhere in `calls.py`. So the codes below are derived
//    from (status, detail text) in one place: a flag-off 404 ("Sales call
//    capture is not enabled here.") and a missing-session 404 ("call session not
//    found") arrive looking identical otherwise, and "the feature is off" and
//    "we can't find that call" must never render as the same sentence.
// 2. `MEET_DISABLED` (HTTP 503). `MeetSpaceWriteDisabled` is raised BEFORE any
//    credential request. With both Meet write scopes granted on the DWD client
//    id, this is `CCS_MEET_ENABLED` being off, a missing
//    service-account key, or credentials that would not load — still an operator
//    action rather than a fault, and still raised only on the AD-HOC start path
//    (the calendar path answers 200 with `arming.reason === "write_disabled"`,
//    because a Meet switch does not affect the company binding). The AE gets a
//    sentence they can act on; the router's own words stay in `detail` for
//    whoever has to fix it.
// 3. `WrapUpItem.is_overwrite` + `ApproveDecision.confirm_overwrite`. True means
//    HubSpot already holds a DIFFERENT value, and the router refuses the write
//    with `needs_confirmation` unless that row carries its own
//    `confirm_overwrite: true`. "Approve all" can never
//    supply it in bulk — the flag comes from the AE confirming that one row.
// 4. `wrap_opened_at` / `client_elapsed_ms` on the approve body. The wrap-up
//    target is "≤3 clicks and ≤90 seconds", measured from these on the
//    server (`attrs.wrap_elapsed_ms_client`). They have to be sent from day one;
//    a retrofit measures nothing about launch.
// 5. `nothing_here` is a COMPLETED wrap-up, not a cancel. It is a recorded
//    negative and a training signal, so the button that sends it belongs next
//    to Approve, not styled as a dismiss. The server drops it back to false if
//    the same request also accepted something.
// 6. Ten approve result statuses, not three. Four of them
//    (`needs_confirmation`, `invalid`, `drift`, `write_failed`) leave the
//    proposal CLAIMABLE — the wrap-up screen has to stay usable after them, or
//    the AE's only route back to a failed write is a re-extraction that will
//    never happen.
import client from './client';

// ─── error shape ─────────────────────────────────────────────────────────────

/** Flattened axios rejection. `code` is ours (the router names none); `message`
 *  is what the user reads; `detail` is the server's own sentence, kept for the
 *  operator line under an actionable failure. */
export interface CallsError {
    code: string | null;
    message: string;
    /** Verbatim `detail` when the router sent one. Null for a network failure.
     *  On a 503 this names the Meet switch or credential fault, and is worth showing to
     *  whoever can act on it. */
    detail: string | null;
    status: number | null;
}

/** Axios shape, narrowed by hand rather than typed as `any`: the only thing we
 *  trust about a rejection is that it MIGHT carry a response body. */
interface MaybeAxiosError {
    response?: { status?: number; data?: { detail?: unknown } };
    message?: string;
}

// Codes are CLIENT-SIDE labels for router states, because `calls.py` raises
// `HTTPException(status_code=…, detail="a sentence")` and nothing else. Each one
// below names the exact router site it stands for.

/** 404 + "Sales call capture is not enabled here." — `_require_ccs_enabled`,
 *  which runs first in every endpoint. `CCS_ENABLED` defaults false. */
export const NOT_ENABLED = 'not_enabled';

/** 404 + "call session not found" — `_session_for_reader` / `_session_for_ae`. */
export const SESSION_NOT_FOUND = 'session_not_found';

/** 403 — "not your call", "only the AE on a call can approve its wrap-up",
 *  "no authenticated identity", or the copilot gate's own sentence. The router's
 *  wording is the honest answer in every case, so it is shown verbatim. */
export const FORBIDDEN = 'forbidden';

/** 503 — `MeetSpaceWriteDisabled` on the AD-HOC start path. `CCS_MEET_ENABLED`
 *  is off, or the service-account credentials would not load. Nothing about the
 *  AE's account is wrong and no retry will help; the call cannot be transcribed
 *  until an admin acts. */
export const MEET_DISABLED = 'meet_disabled';

/** 502 — Google Meet errored on `spaces.create`. Transient. The router's
 *  sentence already tells the AE what to do, so it is passed through. */
export const MEET_FAILED = 'meet_failed';

/** 422 — pydantic rejected the body (a decision list over 200, a bad
 *  `capture_consent`, a negative `client_elapsed_ms`). A bug in this client, so
 *  it must not masquerade as a server outage. */
export const INVALID_REQUEST = 'invalid_request';

/** The AE-facing sentence for a 503. The router's `detail` is one of three
 *  `MeetSpaceWriteDisabled` messages — the `CCS_MEET_ENABLED` switch, a missing
 *  service-account key, or credentials that would not load — all true, all
 *  unreadable to a salesperson mid-call, so the wording below covers the three
 *  and `detail` carries the specifics to whoever can act. */
const MEET_DISABLED_MESSAGE =
    'Cubie cannot create a transcribed Meet space right now — creating Meet spaces is ' +
    'switched off on this environment. Start the call in Google Meet the way you normally ' +
    'would: Cubie still records that the call happened, it just has no transcript to wrap up.';

/** Pull a printable sentence out of `detail`, whatever shape it arrived in.
 *  FastAPI's 422 sends an ARRAY of objects; an array is `typeof === 'object'`,
 *  so a naive object branch reads `.message` off it and shows the fallback. */
function detailText(detail: unknown): string | null {
    if (typeof detail === 'string') return detail.trim() || null;
    if (Array.isArray(detail)) {
        const msgs = detail
            .map(d => (d && typeof d === 'object' ? String((d as { msg?: unknown }).msg ?? '') : ''))
            .filter(Boolean);
        return msgs.length ? msgs.join('; ') : null;
    }
    if (detail && typeof detail === 'object') {
        // Not a shape `calls.py` produces, but a future router might: honour
        // `{code, message}` if it ever shows up rather than dropping it.
        const obj = detail as { message?: unknown };
        return typeof obj.message === 'string' && obj.message.trim() ? obj.message : null;
    }
    return null;
}

export function callsError(e: unknown, fallback = 'Call capture could not be reached.'): CallsError {
    const err = (e ?? {}) as MaybeAxiosError;
    const status = err.response?.status ?? null;
    const raw = err.response?.data?.detail;
    const detail = detailText(raw);
    // A structured detail may carry its own code; a bare string cannot.
    const serverCode =
        raw && typeof raw === 'object' && !Array.isArray(raw)
            ? ((raw as { code?: string | null }).code ?? null)
            : null;

    let code: string | null = serverCode;
    let message = detail || err.message || fallback;

    if (code === null) {
        if (status === 404) {
            // Both 404s carry a bare string, so the string is the only signal.
            // `_require_ccs_enabled` runs before the session lookup, so a
            // flag-off environment always answers with the first sentence.
            code = /not found/i.test(detail ?? '') ? SESSION_NOT_FOUND : NOT_ENABLED;
            message =
                code === SESSION_NOT_FOUND
                    ? 'Cubie cannot find that call. It may have been purged, or the link may be wrong.'
                    : 'Call capture is not enabled yet.';
        } else if (status === 403) {
            code = FORBIDDEN;
            message = detail || 'This call is not yours to open.';
        } else if (status === 503) {
            code = MEET_DISABLED;
            message = MEET_DISABLED_MESSAGE;
        } else if (status === 502) {
            code = MEET_FAILED;
            message = detail || 'Google Meet could not create the meeting space.';
        } else if (status === 422) {
            code = INVALID_REQUEST;
            message = 'Cubie sent something this call could not accept.';
        }
    }

    return { code, message, detail, status };
}

/** The feature (or this endpoint's flag) is off. Render a calm empty state:
 *  nothing about the user's account is wrong and no retry will help. */
export const isNotEnabled = (err: CallsError): boolean => err.code === NOT_ENABLED;

/** That call id does not resolve. A different sentence from `isNotEnabled` on
 *  purpose — see rule 1 at the top of this file. */
export const isSessionNotFound = (err: CallsError): boolean => err.code === SESSION_NOT_FOUND;

/** The Meet space could not be created because Meet writes are switched off
 *  (`CCS_MEET_ENABLED`, or credentials that would not load). Actionable, and
 *  by an admin rather than the AE — show the message,
 *  then `err.detail` under a "for an administrator" line. */
export const isMeetDisabled = (err: CallsError): boolean => err.code === MEET_DISABLED;

/** The caller may not see this call (or Cubie is not switched on for them). The
 *  router's wording is already the honest answer. */
export const isForbidden = (err: CallsError): boolean => err.code === FORBIDDEN;

// ─── shared vocabulary ───────────────────────────────────────────────────────

/** "suppressed" is a confidential call: no transcript entries, no DAM asset,
 *  no participants row, no extraction. Mirrors `CAPTURE_CONSENTS` in
 *  rubik_core/database/call_capture.py, and the router's
 *  `StartCallRequest.capture_consent` pattern `^(granted|suppressed)$`. */
export type CaptureConsent = 'granted' | 'suppressed';

/** Mirrors `SESSION_STATUSES`. `no_transcript` is NOT a failure:
 *  transcription was off, or the call was 40 seconds. The wrap-up still opens,
 *  with an empty proposal set — the call is never invisible even when its
 *  content is. */
export type CallSessionStatus =
    | 'scheduled'
    | 'in_progress'
    | 'ended'
    | 'transcript_ready'
    | 'extracted'
    | 'wrapped'
    | 'no_transcript'
    | 'failed';

/** Mirrors `ITEM_STATUSES`. `GET /wrapup` only ever serializes the two the
 *  router calls claimable — `proposed` and `write_failed` — so a row that comes
 *  back at all is still actionable, and a `write_failed` one is a retry rather
 *  than a dead record. */
export type CallItemStatus =
    | 'proposed'
    | 'accepted'
    | 'rejected'
    | 'superseded'
    | 'written'
    | 'write_failed';

/** Mirrors `ITEM_KINDS` — the database vocabulary. NOT the render order; see
 *  `ITEM_KIND_ORDER`. */
export const ITEM_KINDS = [
    'vendor_field',
    'lead',
    'task',
    'sow_request',
    'stage_signal',
    'contract_expiry',
] as const;

export type ItemKind = (typeof ITEM_KINDS)[number];

/** Render order for the groups, mirroring the router's `_GROUP_ORDER`: what
 *  changes the CRM record first, then what starts a clock, then what creates new
 *  work. The server sends `groups` already in this order and that order wins —
 *  this is the tiebreak, so it has to AGREE with the server rather than fall
 *  back to the table's CHECK-constraint order (which puts `sow_request` fourth). */
export const ITEM_KIND_ORDER: readonly ItemKind[] = [
    'vendor_field',
    'sow_request',
    'lead',
    'task',
    'stage_signal',
    'contract_expiry',
] as const;

/** Short singular labels. Group headings are screen copy (they carry counts and
 *  the company name); these exist so "SOW request" is spelled the same way in
 *  the wrap-up, the cockpit and the digest. */
export const ITEM_KIND_LABELS: Record<ItemKind, string> = {
    vendor_field: 'Vendor field',
    lead: 'Lead',
    task: 'Task',
    sow_request: 'SOW request',
    stage_signal: 'Stage signal',
    contract_expiry: 'Contract expiry',
};

/** Falls back to the raw value: an unknown kind from a newer backend should
 *  render as itself, never as "undefined". */
export const itemKindLabel = (kind: string): string =>
    ITEM_KIND_LABELS[kind as ItemKind] ?? kind;

/** The lead's temperature, the one value the AE sets on a lead by hand. The
 *  AE changes it inline and it is sent as the item's `edited_value`.
 *
 *  The HubSpot property's internal name is still unknown and the LEAD object
 *  needs re-authorization, so `CCS_LEADS_ENABLED` is off and lead rows may
 *  never arrive at all. Offer the select when they do; do not build a parallel
 *  temperature field. */
export const LEAD_TEMPERATURES = ['hot', 'warm', 'cold'] as const;
export type LeadTemperature = (typeof LEAD_TEMPERATURES)[number];

export const isLeadTemperature = (v: unknown): v is LeadTemperature =>
    typeof v === 'string' && (LEAD_TEMPERATURES as readonly string[]).includes(v);

// ─── today's calendar (the entry point) ──────────────────────────────────────
//
// `GET /api/calls/scheduled` is a PURE READ: no Meet writes, no session rows,
// no HubSpot mutations. That is not an optimisation — a recurring series shares
// ONE Meet space, so a screen that armed meetings just by being opened would
// turn transcription on for every future occurrence of every weekly client call
// an AE scrolled past.
//
// THE SCREEN'S PRIMARY JOB IS THE COMPANY BINDING, not the arming. Three things
// make arm-everything impossible — a conference already running cannot be armed,
// a client-hosted space lives in another tenant and cannot be patched at all,
// and a series shares one space — so arming is SECONDARY and best-effort. The
// binding is what lets the wrap-up write to the right CRM record without asking,
// which is why a row whose arming fails is still worth capturing. A row must
// never look broken for failing to arm, and must never look armed when it is not.
//
// Every verdict below is computed SERVER-SIDE and arrives with a reason. Nothing
// on this screen may default to "fine".

/** Whether Cubie already has a `call_sessions` row for this occurrence, and how
 *  far it has got. Mirrors `CAPTURE_STATES` in
 *  `rubik_core/call_capture/calendar_scan.py`. Anything other than
 *  `not_started` means Capture must NOT be offered again — the row shows Join
 *  and the wrap-up instead. */
export const CAPTURE_STATES = [
    'not_started',
    'session_exists',
    'in_progress',
    'wrapped',
] as const;

export type CaptureState = (typeof CAPTURE_STATES)[number];

/** Mirrors `ARMING_STATES`. Three, not two: `unknown` is a real answer and the
 *  most common one on a fresh row, because `GET /scheduled` never asks Google. */
export const ARMING_STATES = ['armed', 'not_armable', 'unknown'] as const;

export type ArmingState = (typeof ARMING_STATES)[number];

/** Mirrors `ARMING_REASONS`. Closed server-side, and the reason — not the
 *  state — is what the AE can act on.
 *
 *  armed_by_cubie     — Cubie patched the space's artifactConfig
 *  already_on         — transcription was already on; Cubie changed nothing
 *  external_host      — the space is in another company's Workspace (a 404 on
 *                       `spaces.get` is what a client-hosted meeting looks like)
 *  not_owner          — someone else organized it, or Meet refused this subject
 *  already_started    — a conference in progress cannot be pre-armed. There is
 *                       no API for it; this is not a retry
 *  recurring_series   — one occurrence of a series, and a series shares ONE
 *                       space. Arrives with state `unknown`: it INFORMS, it does
 *                       not block
 *  arming_unsupported — the Workspace edition/licence answer. TENANT-WIDE, so
 *                       it is true of every other row too — say it once
 *  write_disabled     — `CCS_MEET_ENABLED` is off. Also tenant-wide
 *  not_checked_yet    — nobody has asked Google. On a row with NO session that
 *                       is "not yet"; on a row that HAS one it is the
 *                       confidential case, where Cubie deliberately never asked
 *  lookup_failed      — the calendar's Meet code is missing or malformed
 *  google_error       — Google answered the arming request with an error no
 *                       named reason covers (or could not be reached) */
export const ARMING_REASONS = [
    'armed_by_cubie',
    'already_on',
    'external_host',
    'not_owner',
    'already_started',
    'recurring_series',
    'arming_unsupported',
    'write_disabled',
    'not_checked_yet',
    'lookup_failed',
    'google_error',
] as const;

export type ArmingReason = (typeof ARMING_REASONS)[number];

/** Mirrors `COMPANY_MATCH_STATES`. FIVE, and the two that look alike are the
 *  point: `not_found` is "HubSpot has no such company", `error` is "we could
 *  not ask HubSpot". They lead to different actions, so they are never
 *  collapsed into one sentence. `skipped` is "there was nothing safe to look
 *  up" — the externals were on personal addresses or Google pseudo-domains. */
export const COMPANY_MATCH_STATES = [
    'unique',
    'ambiguous',
    'not_found',
    'skipped',
    'error',
] as const;

export type CompanyMatchState = (typeof COMPANY_MATCH_STATES)[number];

/** One arming verdict (`_verdict` / `_arming` server-side). `state` and
 *  `reason` are typed as bare strings on purpose — both vocabularies are closed
 *  SERVER-side, so a value from a newer backend must degrade to a neutral badge
 *  rather than crash the day's list. `detail` is the server's own sentence and
 *  is the only copy allowed to explain a specific row. */
export interface ArmingVerdict {
    state: string;
    reason: string;
    detail: string | null;
}

export const isArmingState = (v: unknown): v is ArmingState =>
    typeof v === 'string' && (ARMING_STATES as readonly string[]).includes(v);

export const isArmingReason = (v: unknown): v is ArmingReason =>
    typeof v === 'string' && (ARMING_REASONS as readonly string[]).includes(v);

/** Reasons that are true of the WHOLE TENANT, not of this meeting: a licence
 *  that does not include automatic transcription, and Cubie's own kill switch.
 *  They are identical on every row, so the screen states them once above the
 *  list — a per-row repeat of the same paragraph reads as thirty problems. */
export const TENANT_WIDE_ARMING_REASONS: readonly ArmingReason[] = [
    'arming_unsupported',
    'write_disabled',
];

export const isTenantWideArmingReason = (reason: string): boolean =>
    (TENANT_WIDE_ARMING_REASONS as readonly string[]).includes(reason);

/** One HubSpot company the domain lookup turned up. `hubspot_company_id` is
 *  the value that goes back to `POST /start`. */
export interface CompanyCandidate {
    hubspot_company_id: string;
    name: string;
    domain: string;
    lifecyclestage: string;
}

/** Which HubSpot company this meeting is with — the screen's PRIMARY output.
 *
 *  `hubspot_company_id` is NON-NULL ONLY when `match === "unique"`. That is the
 *  server's contract (`_company_block`) and it exists so no client can
 *  reproduce the `candidates[0]` bug: two candidates means a human chooses, not
 *  list order. Never prefill from `candidates` — read `hubspot_company_id`, or
 *  ask.
 *
 *  `domain` is the domain the verdict is ABOUT: the matched company's on a
 *  unique hit, the single looked-up domain when there was exactly one, and null
 *  when several were in play and naming one would mislead. It is what the
 *  "no HubSpot company" sentence has to name. */
export interface MeetingCompany {
    match: string;
    hubspot_company_id: string | null;
    name: string | null;
    domain: string | null;
    candidates: CompanyCandidate[];
}

/** An attendee as the UI shows it. Field names follow the homefeed briefing's
 *  `CalendarEvent` model so the two calendar surfaces in Cubie do not disagree
 *  about what an attendee looks like. Conference ROOMS are already dropped
 *  server-side — a room address read as a company is a wrong CRM write.
 *
 *  `is_optional` is surfaced and must never influence the company decision: an
 *  optional external guest is still the company on the call. */
export interface MeetingAttendee {
    email: string;
    display_name: string | null;
    is_organizer: boolean;
    is_self: boolean;
    is_external: boolean;
    is_optional: boolean;
    response_status: string | null;
}

// ─── the pre-call checklist (`scripts` on a scheduled row) ───────────────────
//
// Why the checklist exists: if something is not asked or not covered on the
// call, no automation afterwards can recover it. A perfect extractor over an
// incomplete conversation still produces an incomplete HubSpot record, and the
// wrap-up looks clean either way.
//
// A SCRIPT IS A CHECKLIST OF FACTS, NOT A TALK TRACK. A typical product's
// checklist is the shape: what scope is needed, how many units, how many hours
// each, what rate, what start date — the facts someone would otherwise have to
// reverse-engineer out of a call summary to build a real SOW.
//
// EVERY PLURAL BELOW IS DELIBERATE. A call that surfaces three products should
// produce THREE leads (they are supposed to be split), so a call carries N
// products, the panel renders one section per product, and coverage is
// computed per product. One product per call is the failure this workstream
// exists to fix, not a shape to encode.

/** One fact the call has to surface. `prompt_text` is what the AE reads;
 *  `fact_key` is what the extractor and the wrap-up's coverage match on. They
 *  are separate values because they change independently — a product owner
 *  rewording a question must not silently retarget the extractor. */
export interface ScriptItem {
    fact_key: string;
    prompt_text: string;
    /** Defaults to TRUE server-side for an item that never says, and the
     *  asymmetry is the point: a wrongly-required fact shows a gap the AE
     *  dismisses in a second, a wrongly-optional one never shows a gap at all. */
    required: boolean;
    help_text: string | null;
}

/** The checklist for ONE product — one section of the pre-call panel. */
export interface ScriptSection {
    script_id: string;
    product_key: string;
    name: string;
    items: ScriptItem[];
}

/** One product the picker can offer, i.e. one that HAS a live checklist.
 *  "Live" is approved, not merely active: scripts can be mined from the
 *  transcript corpus by a batch job, and only a named human's approval makes
 *  one authoritative. */
export interface ScriptOption {
    script_id: string;
    product_key: string;
    name: string;
    item_count: number;
}

/** The row's script block.
 *
 *  `selected_product_keys` is what this AE picked for THIS client last time
 *  (`[]` when never). It is deliberately NOT filtered against `available`:
 *  "what the AE picked" and "what Cubie can show a checklist for" are two
 *  different facts, and a product whose script was retired must still render as
 *  selected — with no section — rather than silently unpicking itself.
 *
 *  Empty lists are a NORMAL state, not a broken one: these tables ship empty
 *  because the content is the product owners' to author and approve. The
 *  whole block being `null` is the different, fail-soft state — see
 *  `meetingScripts`. */
export interface MeetingScripts {
    selected_product_keys: string[];
    available: ScriptOption[];
    /** The checklists for `selected_product_keys` ONLY — what is remembered for
     *  this client, not what the AE may tick now. Render a pick through
     *  `checklistsFor`, never against this list alone. */
    sections: ScriptSection[];
}

export interface ScheduledMeeting {
    /** `event["id"]` — the OCCURRENCE id. The dedupe key, and the value
     *  `POST /start` persists. */
    google_event_id: string;
    /** The SERIES id when this is one occurrence of a recurring meeting.
     *  Reported so the screen can warn that a series shares one Meet space;
     *  NEVER used as an identity. */
    recurring_event_id: string | null;
    title: string;
    /** Google's own ISO strings. Do NOT format these for display — use
     *  `start_local`, which the server rendered through the repo's one clock
     *  formatter (there is a recorded +2h drift bug from letting anything else
     *  render a time). */
    start: string | null;
    end: string | null;
    /** e.g. "Friday Aug 28, 2:00 PM CDT". Already in the calendar's own zone. */
    start_local: string;
    /** Signed whole minutes to the start — NEGATIVE once it has begun. Null
     *  when the event carried no parseable start. */
    starts_in_minutes: number | null;
    is_live: boolean;
    /** The calendar's OWN Meet link, host-checked to meet.google.com
     *  server-side. Nullable in the type because the field is nullable on the
     *  wire; a row with no Meet link is filtered out, so in practice it is set
     *  — never `window.open` it without checking. */
    meet_uri: string | null;
    meeting_code: string | null;
    organizer_email: string | null;
    organizer_is_me: boolean;
    /** 'accepted' | 'tentative' | 'needs_action' | 'none' — `declined` rows are
     *  filtered out server-side. Prep is still guarded on it client-side. */
    rsvp: string | null;
    attendees: MeetingAttendee[];
    /** The matchable external domains, in the order they were looked up. */
    external_domains: string[];
    /** Google's `attendeesOmitted` — it truncated a large invite. Forces the
     *  company match to `ambiguous`, because the one external attendee we can
     *  see may not be the only one there was. */
    attendees_truncated: boolean;
    company: MeetingCompany;
    call_session_id: string | null;
    /** One of `CAPTURE_STATES`; bare string so a newer backend degrades. */
    capture_state: string;
    /** A PRECONDITION verdict on this endpoint, never an outcome — the GET
     *  performs no Meet writes. `not_armable` when it is knowable for free
     *  (external organizer, already started, not the owner), else `unknown`.
     *  A row that already has a session replays THAT session's stored verdict,
     *  so an armed meeting never renders as unchecked. */
    arming: ArmingVerdict;
    /** The pre-call checklist block, ADDITIVE and FAIL-SOFT. `null` means the
     *  lookup failed — the row renders exactly as it did before this feature
     *  existed, with no panel and no error, because a checklist is worth less
     *  than the day. Optional on the wire so an older backend degrades the same
     *  way. Read it through `meetingScripts`, never directly. */
    scripts?: MeetingScripts | null;
}

/** The envelope, matching the shape `backend/routers/eos/l10.py` established
 *  for a calendar read: it NEVER 500s on a Calendar outage.
 *
 *  `calendar_available: false` with an empty list is "we could not look".
 *  `true` with an empty list is "you have no client calls today" — which the AE
 *  BELIEVES, and would then not capture a call they actually have. The screen
 *  must say two different things; never collapse them. */
export interface ScheduledDay {
    calendar_available: boolean;
    timezone: string;
    local_date: string;
    meetings: ScheduledMeeting[];
    /** The checklist for EVERY product the picker offers, sent once for the
     *  whole day, so a product the AE ticks can show its checklist even when
     *  nothing remembers it — see `checklistsFor`. `null` when the server could
     *  not read the checklist tables (every row's `scripts` is `null` then too),
     *  or predates this field. */
    script_catalogue: ScriptSection[] | null;
}

interface ScheduledResponse {
    calendar_available?: boolean | null;
    timezone?: string | null;
    local_date?: string | null;
    meetings?: ScheduledMeeting[] | null;
    script_catalogue?: ScriptSection[] | null;
}

/** Today's capturable client meetings. Coalesced defensively, and note the
 *  DEFAULT for `calendar_available`: **false**. A malformed envelope must read
 *  as "we could not look", never as "you have no calls" — the second is the
 *  answer an AE acts on by not capturing a call they really have. */
export async function fetchScheduledMeetings(): Promise<ScheduledDay> {
    const { data } = await client.get<ScheduledResponse>('/api/calls/scheduled');
    const catalogue = data?.script_catalogue;
    return {
        calendar_available: data?.calendar_available === true,
        timezone: data?.timezone || 'UTC',
        local_date: data?.local_date || '',
        meetings: data?.meetings ?? [],
        // Missing or malformed reads as "no catalogue", never as "no checklists
        // exist": the rows then fall back to their own `sections`.
        script_catalogue: Array.isArray(catalogue)
            ? catalogue.filter(sec => Boolean(sec?.product_key))
            : null,
    };
}

/** True when this row already has a session, so Capture must not be offered
 *  again. Reads `call_session_id` as well as `capture_state` — either one alone
 *  would be a second opinion, and an unknown state from a newer backend must
 *  still count as "there is a session". */
export const hasCallSession = (m: ScheduledMeeting): boolean =>
    Boolean(m.call_session_id) || m.capture_state !== 'not_started';

/** The external guests, for the row's attendee line. Self and internal
 *  colleagues are dropped: the question the row answers is "who is the client". */
export const externalAttendees = (m: ScheduledMeeting): MeetingAttendee[] =>
    (m.attendees ?? []).filter(a => a.is_external && !a.is_self);

/** Prep is offered for every row the server sent EXCEPT a declined one — the
 *  same guard the daily briefing's calendar column uses (`canPrep`). Declined
 *  rows are already filtered server-side; this keeps the two surfaces agreeing
 *  rather than trusting that they always will. */
export const canPrepMeeting = (m: ScheduledMeeting): boolean =>
    Boolean(m.google_event_id) && m.rsvp !== 'declined';

/** The row's script block, or null when there is nothing trustworthy to render.
 *
 *  THE ONE READER. `scripts: null` is the server's fail-soft answer — the
 *  checklist tables could not be read — and it must render as the row rendered
 *  before this feature shipped: no panel, no picker, no error. It is not a
 *  broken state and must never be dressed as one. `undefined` (an older
 *  backend) and a malformed block collapse to the same answer, because a block
 *  missing its arrays would otherwise render as "this client has no products",
 *  which is a claim rather than a shrug.
 *
 *  Empty ARRAYS are NOT null and do not come through here as null: "no
 *  checklist has been authored yet" is the normal early state of a system that
 *  ships empty on purpose, and the panel says so in words. */
export function meetingScripts(m: ScheduledMeeting): MeetingScripts | null {
    const s = m?.scripts;
    if (!s || typeof s !== 'object') return null;
    if (!Array.isArray(s.available) || !Array.isArray(s.sections)) return null;
    return {
        selected_product_keys: Array.isArray(s.selected_product_keys)
            ? s.selected_product_keys.filter(k => typeof k === 'string' && k.length > 0)
            : [],
        available: s.available.filter(o => Boolean(o?.product_key)),
        sections: s.sections.filter(sec => Boolean(sec?.product_key)),
    };
}

/** The checklist behind each product the AE has picked, in pick order — what
 *  the pre-call panel renders.
 *
 *  The row's own `sections` only hold what this AE picked for THIS client last
 *  time, and the AE can tick any product the picker offers: on a first call
 *  with a client nothing is remembered at all. Resolved against `sections`
 *  alone, a fresh pick rendered "No approved checklist for this one yet" under a
 *  chip reading "5 facts to cover", so the checklist only ever appeared from the
 *  second call on. The day's `script_catalogue` holds every product on offer.
 *
 *  A key that neither resolves is left out, and the panel gives it the section
 *  that says so: a remembered product whose checklist was retired is still on
 *  the call. A `null` catalogue (a failed read, an older server) falls back to
 *  the row's own `sections`, which is exactly the old behaviour. */
export function checklistsFor(
    selected: readonly string[],
    sections: readonly ScriptSection[],
    catalogue: readonly ScriptSection[] | null,
): ScriptSection[] {
    const byKey = new Map<string, ScriptSection>();
    // Catalogue first so the row's own section wins a tie. Both come from the
    // same server read, so a tie is the same checklist either way.
    for (const section of [...(catalogue ?? []), ...sections]) {
        byKey.set(section.product_key, section);
    }
    return selected.flatMap(key => {
        const section = byKey.get(key);
        return section ? [section] : [];
    });
}

/** A readable name for a bare `product_key`.
 *
 *  Used ONLY where no authored name exists: a remembered pick whose script was
 *  retired, and (on the wrap-up) a product the call discussed that no script
 *  covers. Wherever a `name` is present it wins — this is a fallback, never a
 *  substitute for what a product owner typed. */
export const productKeyLabel = (key: string): string => {
    const words = (key || '').trim().replace(/[_-]+/g, ' ').trim();
    if (!words) return 'Unnamed product';
    return words.charAt(0).toUpperCase() + words.slice(1);
};

/** The ONE reason worth stating above the list instead of on every row, or
 *  null. Scans the day's verdicts for a tenant-wide answer and returns the
 *  first whole verdict so its server-written sentence is what gets shown. */
export function tenantWideArming(
    meetings: readonly ScheduledMeeting[] | null | undefined,
): ArmingVerdict | null {
    for (const m of meetings ?? []) {
        const v = m.arming;
        if (v && isTenantWideArmingReason(v.reason)) return v;
    }
    return null;
}

// ─── the company picker (`GET /api/calls/companies`) ─────────────────────────
//
// This endpoint exists because `/api/xray/customers` — the only other company
// typeahead in Cubie — sits behind Customer X-Ray's gate, a different audience
// that moves for its own reasons. While that gate was admins-only it 403'd
// every non-admin Sales AE, on the ONE control that unmatched, ambiguous and
// personal-address rows depend on. `/api/calls/companies` runs the same
// `search_companies` behind `require_ccs` — this feature's own audience.
//
// So the screen no longer asks `canAccessXray` about anything. Whether the
// SEARCH is callable is discovered from the search, and it is a different
// question from whether the AE may use call capture at all.

/** A row the picker offers. `hubspot_company_id` (not `company_id`) because
 *  the value goes straight back to `POST /start` under that name. */
export interface CompanyOption {
    hubspot_company_id: string;
    name: string;
    domain: string;
    client_status: string;
}

interface CompaniesResponse {
    companies?: CompanyOption[] | null;
}

/** Company typeahead for binding a call. Fail-soft server-side (a search that
 *  throws returns `[]`), and an EMPTY query returns nothing rather than the
 *  first page of every company — a picker that answers before it is asked is a
 *  way to hand an AE a plausible wrong company.
 *
 *  Rejections still propagate: a 404 means `CCS_ENABLED` is off and a 403 means
 *  the copilot gate refused, and the caller has to tell those apart from "no
 *  company matched". */
export async function searchBindableCompanies(q: string): Promise<CompanyOption[]> {
    const needle = (q ?? '').trim();
    if (!needle) return [];
    const { data } = await client.get<CompaniesResponse>('/api/calls/companies', {
        params: { q: needle },
    });
    return (data?.companies ?? []).filter(c => Boolean(c?.hubspot_company_id));
}

/** The label for a company row: name, then domain, then the bare id. Never
 *  "undefined", and never an empty string that renders as a blank button. */
export const companyOptionLabel = (c: CompanyOption | CompanyCandidate): string =>
    c.name?.trim() || c.domain?.trim() || c.hubspot_company_id;

// ─── starting a call ─────────────────────────────────────────────────────────

export interface StartCallRequest {
    /** Optional on the wire: the router's `hubspot_company_id` defaults to None
     *  because the ad-hoc path exists — a call the sweep finds from a conference
     *  record has no company binding either. The Start-call SCREEN still asks
     *  for one (a bound call is the only kind the wrap-up can write anywhere),
     *  but the contract must not forbid the unbound case. */
    hubspot_company_id?: string | null;
    hubspot_contact_id?: string | null;
    hubspot_deal_id?: string | null;
    /** THE FIELD THAT PICKS THE ORIGIN. Present → `origin="calendar"`: the
     *  router creates NO Meet space, arms the calendar's existing one
     *  best-effort, and is idempotent on (ae_email, calendar_event_id) at the
     *  DATABASE. Absent → `origin="cubie"`, the old `spaces.create` path.
     *
     *  It is `event["id"]` — the OCCURRENCE id — never `recurringEventId`. A
     *  series id here would collapse every occurrence of a weekly client call
     *  into one session forever. `ScheduledMeeting.google_event_id` is already
     *  the right value; pass it through, do not re-derive it. */
    calendar_event_id?: string | null;
    /** REQUIRED whenever `calendar_event_id` is sent — the router 422s without
     *  it. It is the only address `spaces.get` accepts for a space Cubie did
     *  not create, so without it there is nothing to arm and nothing to
     *  explain. Send `ScheduledMeeting.meeting_code` verbatim. */
    meet_meeting_code?: string | null;
    /** The calendar's own Meet URL, passed back rather than re-fetched. */
    meet_uri?: string | null;
    /** The products the AE picked on the row: which checklists this call is
     *  running against, and therefore how many leads it should split into. A
     *  LIST because a call that surfaces three products should produce three
     *  leads — one product per call is the failure being fixed, not the shape.
     *
     *  OMITTED AND EMPTY ARE DIFFERENT REQUESTS, and the difference is
     *  load-bearing. The router reads `model_fields_set`: sent-and-empty is the
     *  AE deselecting every product, which CLEARS their remembered pick for this
     *  client; omitted leaves last week's pick alone. So send `[]` when the
     *  picker rendered and the AE chose nothing, and send NOTHING when there was
     *  no picker to answer with (`scripts: null`) — an undefined key is dropped
     *  by `JSON.stringify` and never reaches the wire. Never send `null`: the
     *  field is `list[str]` server-side and would 422.
     *
     *  Honoured on the CALENDAR path only; the ad-hoc path accepts and ignores
     *  it rather than pretending otherwise. */
    product_keys?: string[];
    /** "suppressed" creates the space with transcription OFF and captures
     *  nothing. There is no third value; the router pattern-checks it. Required
     *  here on purpose — the confidential toggle must resolve to one of these
     *  two before the call is made, rather than inheriting a server default.
     *
     *  On a CALENDAR row it also means "do not arm": the router stores
     *  `_suppressed_arming()` and never asks Google for anything. */
    capture_consent: CaptureConsent;
}

export interface StartCallResponse {
    call_session_id: string;
    /** The URL to `window.open`. NULL is possible and must be handled: Meet
     *  returns no `meetingUri` for a space it could not fully create, and the
     *  60-second de-duplication path hands back an existing session whose own
     *  uri may be null. Never open a null — say the call was recorded but Meet
     *  could not be created. Use `hasMeetSpace` rather than re-deriving it. */
    meet_uri: string | null;
    /** Meet's `meetingCode`, from the same response. Also nullable. */
    meeting_code: string | null;
    /** `cubie` | `adhoc` | `calendar` (`_start_out`). In the response so the UI
     *  never has to infer which of the two starts it got. Typed as a bare
     *  string: the column has no CHECK constraint, so a value this build has
     *  never heard of must degrade rather than throw. */
    origin: string;
    /** The arming verdict AS AN OUTCOME — this is the write path, so unlike
     *  `GET /scheduled` it carries `armed_by_cubie` / `already_on` /
     *  `arming_unsupported` for real. Never absent: `_start_out` always sends
     *  it, on both origins. */
    arming: ArmingVerdict;
}

/** True when there is genuinely a Meet space to open. The one gate between the
 *  response and `window.open`, so no screen can reach for `meet_uri` directly
 *  and pass a null through. */
export const hasMeetSpace = (res: StartCallResponse | null | undefined): boolean =>
    typeof res?.meet_uri === 'string' && res.meet_uri.length > 0;

/** Idempotency differs by origin, and the calendar one is the stronger of the
 *  two:
 *
 *  * CALENDAR (`calendar_event_id` sent) — idempotent at the DATABASE on
 *    `UNIQUE(ae_email, calendar_event_id)`. A second Start returns the FIRST
 *    session and its STORED arming verdict rather than re-arming; Meet cannot
 *    arm a conference already in progress, so a retry would only spend a quota'd
 *    write to replace one honest verdict with another.
 *  * AD HOC (no `calendar_event_id`) — idempotent per (AE, company, consent,
 *    60-second window). That is a backstop against a double-click burning a
 *    second `spaces.create` against a quota of ten per minute per user, NOT the
 *    reason the button carries an in-flight lock. The lock is there to look
 *    sane.
 *
 *  Can throw with `code === MEET_DISABLED` (503) on the AD-HOC path only, where
 *  the Meet write IS the feature — no space, no call. The calendar path never
 *  503s: the session row and the company binding are unaffected by a Meet
 *  switch, so it comes back 200 carrying `arming.reason === "write_disabled"`.
 *  With both Meet write scopes granted on the DWD client id, a 503 means
 *  `CCS_MEET_ENABLED` is off rather than an ungranted scope. */
export async function startCall(body: StartCallRequest): Promise<StartCallResponse> {
    const { data } = await client.post<StartCallResponse>('/api/calls/start', body);
    return data;
}

// ─── pending wrap-ups (slot 1 of "My Day") ───────────────────────────────────

export interface PendingSession {
    call_session_id: string;
    /** Null for an ad-hoc call Cubie detected but could not tie to a company. */
    company_name: string | null;
    ended_at: string | null;
    /** Business-hours age from the cockpit's one clock, not `Date.now()` math.
     *  Never recompute it in the browser — a Friday call is not two days old on
     *  Monday morning. */
    age_hours: number | null;
    /** Includes `transcript_ready` while analysis is pending or retrying. */
    status: CallSessionStatus;
    extraction: CallExtractionState;
}

interface PendingResponse {
    sessions?: PendingSession[] | null;
}

/** The AE's un-wrapped calls, oldest first, capped at 50 server-side. `?? []` on
 *  purpose: an empty list is the good case and must render as "nothing
 *  waiting", not as a crash. */
export async function fetchPendingWrapups(): Promise<PendingSession[]> {
    const { data } = await client.get<PendingResponse>('/api/calls/pending');
    return data?.sessions ?? [];
}

// ─── the wrap-up screen ──────────────────────────────────────────────────────

/** One transcript line around the citation. The screen shows `evidence_text`
 *  and expands ±2 entries on [context] — cheap, and it is what makes the AE
 *  trust the thing in week one. Deliberately NOT an audio player: recording
 *  defaults to OFF, so there is no artifact to seek into. */
export interface EvidenceLine {
    /** Per-session integer the extractor cites, and the stable key for
     *  rendering the context list. */
    sequence: number;
    speaker_name: string | null;
    text: string;
    /** The line the proposal was drawn from — highlight exactly this one. */
    is_cited: boolean;
}

export interface WrapUpItem {
    id: string;
    kind: ItemKind;
    /** HubSpot object family: companies | leads | tasks | deals. Null on an
     *  item that proposes no write (a `sow_request` opens a commitment). */
    target_object: string | null;
    /** The record being updated. Null for a create. */
    target_id: string | null;
    /** The HubSpot property's internal name, e.g. "industry". */
    target_property: string | null;
    /** Server-rendered row label (`_item_label`). Already includes the
     *  company/field name — do not rebuild it from the ids. */
    label: string;
    proposed_value: string | null;
    /** What HubSpot holds right now, read live at proposal time. Render it as
     *  the left side of `current → proposed`; "—" when null. Re-anchored by the
     *  server on a `drift` result, so re-read it after an approve. */
    current_value: string | null;
    /** The AE's correction, when one was already recorded. Seeds the input and
     *  the lead-temperature select. */
    edited_value: string | null;
    /** HubSpot holds a different non-empty value. Amber "⚠ change" badge, and
     *  the row must send `confirm_overwrite: true` of its own before the server
     *  will write it — see `ApproveDecision`. */
    is_overwrite: boolean;
    /** The proposed value is not one of the property's existing enum options,
     *  so writing it would create one. Say so on the row — this is the
     *  "Acme vs Acmee" guard, and a silent new option is a data-quality bug
     *  nobody sees until a report is wrong. Accepting such a row is recorded as
     *  `triage`: nothing is written, ever. */
    needs_new_option: string | null;
    confidence: number | null;
    /** The quote. NULL when the caller may not read transcript text — the
     *  supervisor and the exec pass the row-level gate on a team call and still
     *  fail the field-level one, with no exception. Non-null for the AE's own
     *  call: the column is NOT NULL, so an item always has one. */
    evidence_text: string | null;
    /** ±2 surrounding lines for [context]. Empty when the caller may not read
     *  text, and empty on a Fathom-sourced item: Fathom has no per-utterance
     *  identifiers, so the citation is the text alone. Empty means "no context
     *  to expand", not an error. */
    evidence_context: EvidenceLine[];
    /** Create-shaped payload for a lead or task (name, due date, temperature).
     *  Opaque, and never null — the router coalesces it to `{}`. Display through
     *  `label` and the typed helpers, never by guessing at keys. */
    payload: Record<string, unknown>;
    /** The proposal's own lifecycle status. Only `proposed` or `write_failed`
     *  reach the screen; `write_failed` is a RETRY of a write HubSpot refused,
     *  and the row stays legible so the AE sees why nothing landed. */
    status: CallItemStatus;
    /** The server's own confidence verdict, against
     *  `settings.CCS_MIN_CONFIDENCE_SHOW`. Read this instead of hard-coding the
     *  threshold: the validator KEEPS low-confidence items and flags them, and
     *  the number lives in config where it can be tuned without a deploy of
     *  this file. */
    low_confidence: boolean;
}

export interface WrapUpGroup {
    kind: ItemKind;
    items: WrapUpItem[];
}

// ─── how much of the call the transcript actually covers ─────────────────────
//
// `artifactConfig.autoTranscriptionGeneration: ON` sets a DEFAULT, not a lock:
// Google's own words are "hosts and co-hosts can still override during the
// meeting". So an AE can stop transcription mid-call from the Meet UI, and Cubie
// can neither prevent nor detect it at the time — the whole Meet API has three
// write methods and none of them starts or stops transcription on a running
// conference.
//
// The consequence is the one this feature spends most of its code avoiding: a
// host-stopped transcript is INDISTINGUISHABLE from a complete one at the API
// level. It reports as ENDED, the sweep ingests it correctly, and the wrap-up
// that comes back looks entirely normal — built from part of the call. That
// matters because of WHEN people stop transcription: mid-call, when the
// conversation turns to pricing, a competitor or a complaint. Which is exactly
// where the SOW ask tends to live.
//
// So ingest measures the transcript against the call's own clock and says so.
// Coverage is METADATA — durations, never text — and is therefore NOT gated on
// `can_read_transcript`: a supervisor may see that a call was partly captured
// without seeing a word of what was said.

/** Why `truncated` is the value it is. Transcribed from `COVERAGE_REASONS` in
 *  `calls.py`, which is CLOSED server-side on purpose: coverage is the one block
 *  a supervisor reads while every quote beside it is redacted, so a free-text
 *  reason would be a hole straight through the field-level transcript fence.
 *  The router replaces anything outside its set with `unknown` — which is why
 *  `unknown` is declared here as a real wire value, not as a client fallback. */
export const COVERAGE_REASONS = [
    /** The transcript runs to the end of the call. */
    'covered',
    /** BOTH tests tripped — a low ratio AND a materially long gap. The only
     *  reason that arrives with `truncated: true`, and the one the screen has
     *  something to say about. */
    'truncated',
    /** Under the ratio floor, but the missing tail is minutes rather than a
     *  material stretch. Deliberately NOT truncated: a four-minute call that
     *  ends forty seconds early is a normal call, and a detector that cries
     *  wolf teaches the AE to ignore the one that is real. */
    'short_gap',
    /** A real gap, but proportionally most of the call is here. Not truncated,
     *  same reasoning. */
    'within_ratio',
    /** An utterance stamped past the end of the call — clock skew between Meet's
     *  offsets and our own timestamps, clamped rather than reported as a
     *  negative gap. Never truncated. */
    'clock_skew',
    /** No usable `started_at`/`ended_at`, so there was no denominator to
     *  measure against. Unknown, never truncated. */
    'no_call_duration',
    /** Entries exist, but none carries an end offset — how far the transcript
     *  reaches is unknowable. */
    'no_offsets',
    /** The Fathom case: Fathom carries NO per-utterance timestamps at all, so
     *  its coverage computes as zero and would flag EVERY Fathom call.
     *  Unknowable, not small — this is the likeliest false positive in
     *  the whole feature and it is named so it can never be mistaken for one. */
    'no_offsets_fathom',
    /** The router's own fallback for a code it does not recognise — an ingest
     *  build ahead of the router. It costs a label and leaks nothing, and it
     *  reaches this client as a literal `"unknown"`. */
    'unknown',
] as const;

export type CoverageReason = (typeof COVERAGE_REASONS)[number];

export const isCoverageReason = (v: unknown): v is CoverageReason =>
    typeof v === 'string' && (COVERAGE_REASONS as readonly string[]).includes(v);

/** How much of the call the transcript covers. Null on any session ingest never
 *  measured — every pre-existing one, and every call whose clock is unknown. */
export interface TranscriptCoverage {
    /** Minutes of the call the transcript reaches, from the last entry that
     *  carries an end offset. */
    covered_minutes: number | null;
    /** The call's own length, `ended_at - started_at`. Null when unknowable. */
    call_minutes: number | null;
    /** THE GATE. True only when the coverage ratio AND the absolute gap both
     *  clear their thresholds, which is a server-side judgement against two
     *  settings. Never re-derive it here from the minutes: the thresholds live
     *  in config so they can be tuned without a deploy of this file, and a
     *  second opinion in the client is how the two silently disagree. */
    truncated: boolean;
    /** The unread tail. Null when `call_minutes` is. */
    gap_minutes: number | null;
    /** One of `COVERAGE_REASONS` — typed as a bare string because the server's
     *  field is one, so an unrecognised code has to degrade rather than throw. */
    reason: string;
}

/** The one gate for rendering anything about coverage. `truncated` is the
 *  server's verdict and the only thing worth showing: a fully-captured call gets
 *  NO badge (noise on every normal call dilutes the one that matters), and an
 *  unmeasured one gets no sentence either — "we do not know" on every
 *  pre-existing session is worse than silence. */
export const isTruncatedCoverage = (
    coverage: TranscriptCoverage | null | undefined,
): boolean => Boolean(coverage?.truncated);

/** Whole minutes for the "covers X of Y minutes" line, or null when that
 *  sentence would be nonsense — an unmeasured call, or a pair that rounds to the
 *  same number. The band still has something true to say without them. */
export const coverageMinutes = (
    coverage: TranscriptCoverage | null | undefined,
): { covered: number; call: number } | null => {
    const covered = Math.round(coverage?.covered_minutes ?? NaN);
    const call = Math.round(coverage?.call_minutes ?? NaN);
    if (!Number.isFinite(covered) || !Number.isFinite(call)) return null;
    if (call <= 0 || covered < 0 || covered >= call) return null;
    return { covered, call };
};

export interface WrapUpSession {
    call_session_id: string;
    company_name: string | null;
    ae_name: string | null;
    duration_minutes: number | null;
    ended_at: string | null;
    status: CallSessionStatus;
    /** Already marked "nothing here". The screen is then a read-only record of
     *  a COMPLETED wrap-up — not an invitation to approve again. */
    nothing_here: boolean;
    /** False for a reader the transcript-text policy excludes (the supervisor,
     *  and an exec on a team call). Every quote then arrives null and every
     *  context list empty, so the screen must say it is showing metadata only
     *  rather than render empty quote boxes. */
    can_read_transcript: boolean;
    /** How much of the call the transcript actually covers — see the section
     *  above. Null when ingest never measured it, which is every session that
     *  predates the measurement. NOT gated on `can_read_transcript`: durations
     *  are metadata, not transcript content. */
    transcript_coverage: TranscriptCoverage | null;
}

/** Content-free model analysis status. Provider exceptions and transcript
 *  snippets never appear here; only validated partial-chunk counts do. */
export interface CallExtractionState {
    status: 'pending' | 'retrying' | 'partial' | 'complete' | 'not_applicable';
    chunks_failed: number;
    chunks_total: number;
    /** Only present on the wrap-up read, where ownership can be checked. */
    can_retry?: boolean;
}

/** One checklist fact, and whether the call actually established it.
 *
 *  `found: false` is the POINT of this type, not an error state: it is the
 *  question the AE did not ask. That is why `prompt_text` travels with it —
 *  the screen must show the question in the AE's own words, never a bare
 *  `fact_key`, because "rate" tells nobody what to go and chase. */
export interface ScriptCoverageItem {
    fact_key: string;
    prompt_text: string;
    required: boolean;
    found: boolean;
    value: string | null;
    evidence_entry_id: number | null;
}

/** Coverage for ONE product. Never merge two of these into a single score:
 *  the failure this feature exists to catch is an AE treating a multi-product
 *  call as one thing (a lead and its deal are 1:1), and a blended
 *  percentage would hide exactly that. */
export interface ScriptCoverageSection {
    script_id: string;
    product_key: string;
    name: string;
    items: ScriptCoverageItem[];
    missing_required: string[];
}

export interface WrapUp {
    session: WrapUpSession;
    extraction: CallExtractionState;
    /** Empty on a `no_transcript` session, and that is a normal state: the AE
     *  can still log the call. Render the empty case as the spec's calm
     *  "nothing to file" screen, never as a failure. */
    groups: WrapUpGroup[];
    /** One entry per checklist the call was STARTED against. Empty when the AE
     *  picked no product, or when no approved script existed for what they
     *  picked — both are normal, and neither is a gap worth reporting. */
    script_coverage?: ScriptCoverageSection[];
    /** Products the call discussed that it was never scripted for. This is the
     *  "split it into its own lead" prompt, and it is the one signal here that
     *  fires even on day one with an empty `call_scripts` table — so it can
     *  arrive with `script_coverage` empty. */
    unscripted_products?: string[];
}

/** AE-only retry after a failed analysis. No HubSpot mutation is performed. */
export async function retryWrapUpExtraction(sessionId: string): Promise<CallExtractionState> {
    const { data } = await client.post<{ extraction: CallExtractionState }>(
        `/api/calls/${encodeURIComponent(sessionId)}/wrapup/retry-extraction`,
    );
    return data.extraction;
}

/** GET stamps `wrap_opened_at` server-side on the AE's FIRST read — the
 *  numerator of the 90-second metric. A manager's read does not start it and a
 *  re-read does not move it, so re-fetching after an approve is safe and is the
 *  right move when any result came back unresolved. */
export async function fetchWrapUp(sessionId: string): Promise<WrapUp> {
    const { data } = await client.get<WrapUp>(
        `/api/calls/${encodeURIComponent(sessionId)}/wrapup`,
    );
    return data;
}

/** Flat item count across groups — for the header ("4 proposals") and for the
 *  Approve-all button's disabled state. */
export const countWrapUpItems = (groups: WrapUpGroup[] | null | undefined): number =>
    (groups ?? []).reduce((n, g) => n + (g.items?.length ?? 0), 0);

/** The temperature to bind the lead select to: the AE's edit if there is one,
 *  otherwise the model's proposal. Null when neither is a known value — show
 *  the placeholder rather than silently defaulting to "warm". */
export function leadTemperature(item: WrapUpItem): LeadTemperature | null {
    if (isLeadTemperature(item.edited_value)) return item.edited_value;
    if (isLeadTemperature(item.proposed_value)) return item.proposed_value;
    const fromPayload = item.payload?.temperature;
    return isLeadTemperature(fromPayload) ? fromPayload : null;
}

// ─── approval ────────────────────────────────────────────────────────────────

/** `_MAX_DECISIONS` in the router. Pydantic rejects a longer list with a 422
 *  before any claim runs, and a wrap-up this size is a process failure rather
 *  than a browsing problem. */
export const MAX_DECISIONS = 200;

export interface ApproveDecision {
    item_id: string;
    /** Everything defaults to ticked, so `accept: false` is the AE deliberately
     *  rejecting. Send a decision for EVERY item shown — an omitted item is
     *  left pending, which is neither approved nor recorded as declined. */
    accept: boolean;
    /** The corrected value (a fixed vendor name, a lead temperature). Omit when
     *  the proposal is accepted as-is. An edit is NOT more trusted than the
     *  model's value: a vendor field goes back through the same live HubSpot
     *  option closure, and a value with no option comes back `invalid`. */
    edited_value?: string | null;
    /** MANDATORY on any `is_overwrite` item the AE accepts. Without it the
     *  server refuses that row with `needs_confirmation` and leaves it pending —
     *  it never blind-overwrites a non-null HubSpot value.
     *  It must be set from a per-ROW confirmation the AE gave on that row, never
     *  from the Approve-all button. */
    confirm_overwrite?: boolean;
}

export interface ApproveRequest {
    decisions: ApproveDecision[];
    /** The explicit negative. Send `decisions: []` with this set. The server
     *  records it as false if the same request also accepted something, so the
     *  "nothing here" count cannot be inflated by an approval that filed. */
    nothing_here?: boolean;
    /** ISO stamp of when the screen was opened, and the wall-clock the AE
     *  actually spent. Both feed the wrap-up timing — send them on every
     *  approve. `wrap_opened_at` is only adopted when the server never stamped
     *  one, so it cannot be used to shorten the measure. */
    wrap_opened_at?: string;
    /** Must be ≥ 0; the router's field rejects a negative with a 422. */
    client_elapsed_ms?: number;
}

/** Every value in the router's `DECISION_RESULTS`, in its order. The UI branches
 *  on these strings, so all ten are typed rather than the three that are easy.
 *
 *  written            — reached HubSpot on this request
 *  already_written    — a ledger row already succeeded (AC 6: approving twice
 *                       writes once)
 *  recorded           — accepted; there is no HubSpot write for this kind (a SOW
 *                       clock, a stage signal, a contract expiry)
 *  triage             — `needs_new_option`: a value HubSpot has no option for.
 *                       Accepted, kept, and written nowhere
 *  rejected           — the AE said no
 *  skipped            — unknown item, or already decided by someone/something
 *  needs_confirmation — `is_overwrite` without `confirm_overwrite` on that row
 *  invalid            — the (possibly edited) value failed the vocabulary gate
 *  drift              — HubSpot moved since the proposal; the row was re-anchored
 *                       on reality and put back in front of the AE
 *  write_failed       — HubSpot refused or errored; retryable from this screen */
export const APPROVE_RESULT_STATUSES = [
    'written',
    'already_written',
    'recorded',
    'triage',
    'rejected',
    'skipped',
    'needs_confirmation',
    'invalid',
    'drift',
    'write_failed',
] as const;

export type ApproveResultStatus = (typeof APPROVE_RESULT_STATUSES)[number];

/** Per-item outcome. `message` is present on every status that needs
 *  explaining — `write_failed` carries HubSpot's own friendly error,
 *  `needs_confirmation` names the value being replaced, `drift` names what the
 *  field now reads, `invalid` lists the options that do exist, `triage` says
 *  nothing was written, and `skipped` says what already happened to the row. */
export interface ApproveResult {
    item_id: string;
    status: ApproveResultStatus;
    message?: string | null;
}

/** The call engagement — the write that ALWAYS happens, even for "nothing here"
 *  and even for a confidential call, because the record that a call took place
 *  is the point and the extraction is the bonus. It is not a decision, so it
 *  arrives beside `results` and the AE has to be told whether it landed. */
export interface CallLogResult {
    status: 'written' | 'already_written' | 'write_failed';
    message?: string | null;
    /** The HubSpot call-engagement id, on a fresh `written`. */
    hubspot_object_id?: string | null;
}

/** The whole approve response. Reading only `results` throws away the one write
 *  that always happens and the server's verdict on `nothing_here`. */
export interface ApproveOutcome {
    results: ApproveResult[];
    call_logged: CallLogResult | null;
    /** The server's answer, not the request's: false when the same request
     *  accepted something. */
    nothing_here: boolean;
    wrapped_at: string | null;
}

interface ApproveResponse {
    results?: ApproveResult[] | null;
    call_logged?: CallLogResult | null;
    nothing_here?: boolean | null;
    wrapped_at?: string | null;
}

/** Statuses that mean the proposal reached its destination (or was already
 *  there, or had no HubSpot destination to reach). Nothing is owed on these. */
export const APPROVE_RESULT_FILED: readonly ApproveResultStatus[] = [
    'written',
    'already_written',
    'recorded',
    'triage',
];

/** Statuses that leave the item CLAIMABLE, so the row is still the AE's to
 *  finish: confirm the overwrite, correct the value, look at what changed, or
 *  retry the write. The screen must stay usable after any of these — the
 *  session is wrapped, but these rows are not. */
export const APPROVE_RESULT_UNRESOLVED: readonly ApproveResultStatus[] = [
    'needs_confirmation',
    'invalid',
    'drift',
    'write_failed',
];

export const isFiledResult = (status: ApproveResultStatus): boolean =>
    APPROVE_RESULT_FILED.includes(status);

export const isUnresolvedResult = (status: ApproveResultStatus): boolean =>
    APPROVE_RESULT_UNRESOLVED.includes(status);

/** HubSpot writes happen synchronously inside this request (the write
 *  token is mounted only on the main service, so no job may write). It can
 *  therefore be slow — the 105s instance timeout is the ceiling, not 90s.
 *
 *  An empty results array is NORMAL for a "nothing here" approve and must not
 *  be read as failure: the call engagement still went out, and `call_logged`
 *  says so. */
export async function approveWrapUp(
    sessionId: string,
    body: ApproveRequest,
): Promise<ApproveOutcome> {
    const { data } = await client.post<ApproveResponse>(
        `/api/calls/${encodeURIComponent(sessionId)}/wrapup/approve`,
        body,
    );
    return {
        results: data?.results ?? [],
        call_logged: data?.call_logged ?? null,
        nothing_here: Boolean(data?.nothing_here),
        wrapped_at: data?.wrapped_at ?? null,
    };
}
