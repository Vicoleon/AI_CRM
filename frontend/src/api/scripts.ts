// Call Scripts API client — the authoring surface that keeps scripts DATA, not code.
//
// The client half of `backend/routers/scripts.py`. THE ROUTER IS THE SERVER: every
// type below is transcribed from it, and where the two ever disagree the router
// wins and this file changes.
//
// WHY THIS SURFACE EXISTS. A script is a per-product checklist of the FACTS a
// client call has to surface for a complete HubSpot record and a buildable SOW —
// a typical product needs about five of them (scope, how many units, hours per
// unit, rate, start date). Without it, a perfect extractor over an incomplete
// conversation still produces an incomplete record and the wrap-up looks clean.
// The screen this client serves is the feature's promise: adding a product
// script is a FORM ENTRY by a product owner — no deploy, no engineering ticket.
// A script system only an engineer can update is accurate for one quarter and
// wrong forever after.
//
// SIX THINGS HERE ARE LOAD-BEARING AND NOT STYLE CHOICES:
//
// 1. APPROVAL, NOT `is_active`, IS WHAT MAKES A SCRIPT LIVE. The router resolves
//    the AE-facing script as newest-version + `is_active` + `approved_by NOT
//    NULL`, and `is_live` on the wire is that answer already computed. Three
//    columns, one question — so `scriptState()` below is the ONLY place this
//    client decides what a version is, and no screen re-derives it.
// 2. EDITING AN APPROVED SCRIPT FORKS A NEW VERSION. `PUT` on an approved script
//    returns a DIFFERENT `script_id` at `version + 1`, unapproved, with
//    `forked: true`. Calls already running on the current version keep running on
//    it. A caller that assumes it just edited the row it sent is holding the
//    wrong id and will approve the wrong thing.
// 3. OMITTED AND NULL ARE DIFFERENT ON `PUT`. The router reads pydantic's
//    `model_fields_set`: absent `notes` leaves the note alone, `notes: null`
//    clears it; absent `items` KEEPS the current checklist, `items: []` empties
//    it. `undefined` disappears in JSON.stringify, so leaving a key off
//    `ScriptUpdate` is how you say "leave it alone" — see `updateScript`.
// 4. `is_active` ALONE EDITS IN PLACE; `is_active` ALONGSIDE CONTENT FORKS.
//    Retire and restore must therefore be sent on their own, never bundled into
//    a save, or retiring a live script would silently mint an unapproved copy
//    that cannot be live and therefore cannot restore anything.
// 5. THE FOUR MUTATING ENDPOINTS CARRY A THIRD, NARROWER GATE than the two reads
//    (`user_can_access_cockpit_supervisor`: the `sales_cockpit_supervisor`
//    feature, admins plus any team an admin adds in Feature Access).
//    `can_author` on the list response is the server's own answer to "may this
//    person author", and it is a MIRROR for hiding controls, never a substitute
//    for the gate.
// 6. THERE IS NO READ FOR ONE SCRIPT BY ID. `GET /api/scripts/{product_key}`
//    returns the LIVE APPROVED script for a product; the catalogue returns
//    counts without items. So a draft's questions can be read back only from the
//    response that created or last saved it — see `ScriptDetail` and the note on
//    `fetchScripts`. Consequence for callers: when you do not hold a script's
//    items, OMIT `items` from the `PUT` (rule 3) rather than sending an empty
//    list, which would wipe a checklist nobody could see.
import client from './client';

// ─── error shape ─────────────────────────────────────────────────────────────

/** Flattened axios rejection. `code` is ours (the router names none); `message`
 *  is what a person reads; `detail` is the server's own sentence.
 *
 *  Most of this router's sentences are written FOR THE AUTHOR and are more
 *  precise than anything this file could invent — "fact_key 'rate' appears twice
 *  in this script." names the fix. So `message` is the server's detail verbatim
 *  wherever the server wrote one for a human, and ours only where it did not. */
export interface ScriptsError {
    code: string | null;
    message: string;
    detail: string | null;
    status: number | null;
}

/** Axios shape narrowed by hand: the only thing we trust about a rejection is
 *  that it MIGHT carry a response body. */
interface MaybeAxiosError {
    response?: { status?: number; data?: { detail?: unknown } };
    message?: string;
}

/** 404 + "Sales call capture is not enabled here." — `_require_ccs_enabled`,
 *  imported by this router from `calls.py` so there is one flag and one message.
 *  `CCS_ENABLED` defaults false. Nothing about the account is wrong. */
export const NOT_ENABLED = 'not_enabled';

/** 404 + "No such call script." / "No approved call script for 'x'." — a real
 *  miss, and a DIFFERENT sentence from `NOT_ENABLED` on purpose: "the feature is
 *  off" and "that script does not exist" must never render as the same thing. */
export const SCRIPT_NOT_FOUND = 'script_not_found';

/** 403 from `require_ccs` — outside Sales Call Capture altogether. */
export const FORBIDDEN = 'forbidden';

/** 403 from the authoring gate (`_AUTHOR_DENIED`). Narrower than `FORBIDDEN`:
 *  this caller may READ the catalogue and may not change it, which is a
 *  different screen state from "this surface is not yours". */
export const AUTHOR_DENIED = 'author_denied';

/** 400 — a shape the author can fix: a product/fact key that is not a slug, a
 *  duplicated fact key, a nameless script, an approve with no items. The
 *  router's sentence names the offending value; show it verbatim. */
export const INVALID = 'invalid';

/** 409 — either a lost version race (two authors forked the same script) or an
 *  approve against a retired version. Both are recoverable and both arrive with
 *  an instruction in `detail`. */
export const CONFLICT = 'conflict';

/** 422 — pydantic refused the body (over 50 items, a field past its cap). A bug
 *  in this client or an unvalidated form, so it must not masquerade as an outage. */
export const INVALID_REQUEST = 'invalid_request';

/** Pull a printable sentence out of `detail`, whatever shape it arrived in.
 *  FastAPI's 422 sends an ARRAY of objects, and an array is `typeof 'object'`,
 *  so a naive object branch reads `.message` off it and shows nothing. */
function detailText(detail: unknown): string | null {
    if (typeof detail === 'string') return detail.trim() || null;
    if (Array.isArray(detail)) {
        const msgs = detail
            .map(d => (d && typeof d === 'object' ? String((d as { msg?: unknown }).msg ?? '') : ''))
            .filter(Boolean);
        return msgs.length ? msgs.join('; ') : null;
    }
    if (detail && typeof detail === 'object') {
        const obj = detail as { message?: unknown };
        return typeof obj.message === 'string' && obj.message.trim() ? obj.message : null;
    }
    return null;
}

export function scriptsError(e: unknown, fallback = 'Call scripts could not be reached.'): ScriptsError {
    const err = (e ?? {}) as MaybeAxiosError;
    const status = err.response?.status ?? null;
    const detail = detailText(err.response?.data?.detail);

    let code: string | null = null;
    let message = detail || err.message || fallback;

    if (status === 404) {
        // Both 404s carry a bare string, so the string is the only signal.
        // `_require_ccs_enabled` runs FIRST in every handler, so a flag-off
        // environment always answers with its sentence and never the other.
        const off = /not enabled/i.test(detail ?? '');
        code = off ? NOT_ENABLED : SCRIPT_NOT_FOUND;
        message = off ? 'Call scripts are not switched on in this environment.' : detail || 'No such call script.';
    } else if (status === 403) {
        // `_AUTHOR_DENIED` opens with "Authoring call scripts…"; `require_ccs`
        // opens with "Sales Call Capture is limited…". Both sentences are the
        // honest answer, so both are shown — only the CODE differs, and only so
        // a screen can tell "read-only for you" from "not yours at all".
        code = /^authoring call scripts/i.test(detail ?? '') ? AUTHOR_DENIED : FORBIDDEN;
        message = detail || 'You cannot change call scripts.';
    } else if (status === 400) {
        code = INVALID;
        message = detail || 'That script could not be saved as written.';
    } else if (status === 409) {
        code = CONFLICT;
        message = detail || 'Someone else changed this script a moment ago. Reload and try again.';
    } else if (status === 422) {
        code = INVALID_REQUEST;
        message = 'Cubie sent something this script could not accept.';
    }

    return { code, message, detail, status };
}

/** `CCS_ENABLED` is off. Render a calm empty state: no retry helps and nothing
 *  about the account is wrong. */
export const isNotEnabled = (err: ScriptsError): boolean => err.code === NOT_ENABLED;

/** Outside Sales Call Capture entirely — the read gate refused. */
export const isForbidden = (err: ScriptsError): boolean => err.code === FORBIDDEN;

/** May read the catalogue, may not change it. The editor hides its controls;
 *  the page still renders the live scripts. */
export const isAuthorDenied = (err: ScriptsError): boolean => err.code === AUTHOR_DENIED;

/** No such script / no approved script for that product. */
export const isScriptNotFound = (err: ScriptsError): boolean => err.code === SCRIPT_NOT_FOUND;

/** A lost version race or an approve against a retired version. Recoverable,
 *  and `detail` says how. */
export const isConflict = (err: ScriptsError): boolean => err.code === CONFLICT;

// ─── shared vocabulary ───────────────────────────────────────────────────────

/** Mirrors `SCRIPT_SOURCES`. `"mined"` rows are drafted from the historic
 *  transcript corpus by a batch job — written by a machine, with `is_active`
 *  already true — which is exactly why `approved_by` and not `is_active` is what
 *  makes a script live. `POST /api/scripts` always writes `"manual"`; the router
 *  does not accept `source` from the body, because a forgeable provenance label
 *  is worse than none when provenance is what an approver reads before signing. */
export const SCRIPT_SOURCES = ['mined', 'manual', 'imported'] as const;
export type ScriptSource = (typeof SCRIPT_SOURCES)[number];

/** Typed as a bare string on the wire: the column has no CHECK constraint this
 *  client can rely on, so an unknown source must degrade to itself rather than
 *  render as "undefined". */
export const SCRIPT_SOURCE_LABELS: Record<ScriptSource, string> = {
    mined: 'Drafted from past calls',
    manual: 'Written by hand',
    imported: 'Imported',
};

export const scriptSourceLabel = (source: string): string =>
    SCRIPT_SOURCE_LABELS[source as ScriptSource] ?? source;

/** The four states a version can be in, and they are NOT interchangeable — this
 *  is the whole of what an author needs to know before touching a version:
 *
 *  * `draft`      — unapproved. Edits in place. No AE has ever seen it.
 *  * `approved`   — the live version. This is what an AE sees on a client call.
 *  * `superseded` — approved and active, but a NEWER approved version won. It is
 *                   still the version some finished call ran under, which is why
 *                   it is kept rather than deleted.
 *  * `retired`    — `is_active` false. Off, reversibly, and invisible to AEs.
 *
 *  `retired` is tested FIRST because it is the author's own switch and it
 *  overrides everything else: a retired approved version is not "superseded by"
 *  anything, it was turned off. */
export const SCRIPT_STATES = ['draft', 'approved', 'superseded', 'retired'] as const;
export type ScriptState = (typeof SCRIPT_STATES)[number];

export const SCRIPT_STATE_LABELS: Record<ScriptState, string> = {
    draft: 'Draft',
    approved: 'Approved',
    superseded: 'Superseded',
    retired: 'Retired',
};

/** One sentence per state, written for the person deciding what to do next. */
export const SCRIPT_STATE_HINTS: Record<ScriptState, string> = {
    draft: 'Not in front of anyone yet. Approving is what puts it on live client calls.',
    approved: 'Live. AEs see this checklist on client calls for this product.',
    superseded: 'A newer approved version is live. Kept because finished calls ran under this one.',
    retired: 'Switched off. No AE sees it. It can be restored.',
};

// ─── the wire types ──────────────────────────────────────────────────────────

/** One fact the call has to surface, and the question that surfaces it.
 *  `_item_out` in the router. */
export interface ScriptItem {
    item_id: string;
    /** 1-based, assigned SERVER-side from list order — the client never sends it.
     *  Reorder by reordering the array you `PUT`. */
    sequence: number;
    /** What the extractor and the wrap-up's coverage list match on. */
    fact_key: string;
    /** What the AE reads out loud. Separate from `fact_key` because they change
     *  independently: rewording a question must not silently retarget the
     *  extractor. */
    prompt_text: string;
    /** `"object.property"`, or a bare property, or NULL — and NULL is a complete
     *  answer, not a gap. Some facts feed the SOW rather than the CRM (hours per
     *  unit, a rate) and no HubSpot property holds them: "surface it in the
     *  wrap-up, write it nowhere". Coverage discards the object half when
     *  matching, so `company.industry` and `industry` behave alike. */
    hubspot_target: string | null;
    /** Defaults TRUE server-side, and the asymmetry is the point: a wrongly
     *  required fact shows a gap the AE dismisses in a second, a wrongly optional
     *  one never shows a gap at all. */
    required: boolean;
    help_text: string | null;
}

/** A script WITHOUT its items — what `GET /api/scripts` returns for the
 *  catalogue. `item_count` / `required_item_count` are why the list is still
 *  legible without them. */
export interface ScriptSummary {
    script_id: string;
    product_key: string;
    name: string;
    version: number;
    hubspot_product_id: string | null;
    /** The AUTHOR'S on/off switch. Not the same question as "is this live". */
    is_active: boolean;
    /** The server's own answer to the only question an AE's screen asks: is this
     *  the newest approved, active version for this product. Never re-derive it
     *  from the other columns — `scriptState()` is the one reader. */
    is_live: boolean;
    /** A named human's signature, folded. NULL means nobody has approved it, and
     *  no `is_active` value can substitute. */
    approved_by: string | null;
    approved_at: string | null;
    source: string;
    notes: string | null;
    item_count: number;
    required_item_count: number;
    created_at: string | null;
    updated_at: string | null;
}

/** A script WITH its items. Returned by `GET /api/scripts/{product_key}`,
 *  `POST /api/scripts`, `PUT`, `POST /{id}/approve` and `DELETE` — i.e. by every
 *  endpoint except the catalogue. */
export interface ScriptDetail extends ScriptSummary {
    items: ScriptItem[];
}

export interface ScriptCatalogue {
    scripts: ScriptSummary[];
    /** The server's own mirror of the authoring gate, so a screen can hide the
     *  editor rather than render a control that 403s. Never a substitute for the
     *  gate: every mutating endpoint re-checks. */
    can_author: boolean;
}

/** `PUT` on an approved script comes back as a NEW row — see rule 2 at the top.
 *  `forked` is absent (not false) on an in-place draft edit, so test it as
 *  truthy rather than reading it as a boolean the server always sends. */
export interface ScriptSaveResult extends ScriptDetail {
    forked?: boolean;
    forked_from_script_id?: string;
}

/** `already_approved: true` means a second click landed on an already-signed
 *  script: the ORIGINAL approver and timestamp come back untouched. The router
 *  never re-signs — overwriting a signature would quietly reassign
 *  accountability for content that is already live. */
export interface ScriptApproveResult extends ScriptDetail {
    already_approved: boolean;
}

/** `already_inactive: true` means it was already retired. Idempotent, 200. */
export interface ScriptRetireResult extends ScriptDetail {
    already_inactive: boolean;
}

// ─── request bodies ──────────────────────────────────────────────────────────

/** One item as the router accepts it. NO `sequence`: the ORDER OF THE ARRAY is
 *  the order the panel renders, and a client-supplied sequence is one more way
 *  for two items to collide or for a gap to appear in a checklist a human reads
 *  top to bottom. */
export interface ScriptItemInput {
    fact_key: string;
    prompt_text: string;
    hubspot_target?: string | null;
    required: boolean;
    help_text?: string | null;
}

export interface ScriptCreate {
    product_key: string;
    name: string;
    hubspot_product_id?: string | null;
    notes?: string | null;
    items: ScriptItemInput[];
}

/** Every field optional, and `undefined` (omitted) is NOT the same as `null` —
 *  rule 3 at the top of this file. Build this object by leaving keys OFF, never
 *  by setting them to `null`, unless you mean "clear it".
 *
 *  `product_key` is deliberately absent: a product key is the join key the whole
 *  feature matches on, and the router does not accept a change to it. A script
 *  for a different product is a different script. */
export interface ScriptUpdate {
    name?: string;
    hubspot_product_id?: string | null;
    notes?: string | null;
    /** Send this ALONE. Bundled with content it forks a new version instead of
     *  toggling this one — rule 4. */
    is_active?: boolean;
    /** Omit to keep the current checklist; `[]` empties it. Never send `[]`
     *  because you could not read the current one. */
    items?: ScriptItemInput[];
}

// ─── shape rules, mirrored from the router so a typo costs no round trip ─────

/** `_MAX_ITEMS`. One product's checklist is typically about five facts. A
 *  "checklist" of fifty is a form, and an AE reading a form during a client
 *  call reads none of it. */
export const MAX_SCRIPT_ITEMS = 50;

/** Pydantic `max_length` on each field, so the form can stop a body the server
 *  would 422 with a message about pydantic rather than about the script. */
export const MAX_KEY_CHARS = 64;
export const MAX_NAME_CHARS = 200;
export const MAX_PROMPT_CHARS = 1000;
export const MAX_HELP_CHARS = 1000;
export const MAX_TARGET_CHARS = 128;
export const MAX_NOTES_CHARS = 4000;

/** `_KEY_RE` in the router, character for character. Stricter than the engine's
 *  model-facing pattern on purpose: a key TYPED BY A HUMAN has no reason to
 *  carry spaces, and a `product_key` of "annual review" would never match the
 *  folded, slug-shaped keys the rest of the feature joins on. Rejecting it in
 *  the form is the only place the mistake is VISIBLE — everywhere downstream it
 *  silently renders an empty panel, or reports a discussed fact as missing. */
export const SCRIPT_KEY_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/** `_TARGET_RE`. Permissive about the object half because coverage discards it. */
export const SCRIPT_TARGET_RE = /^[a-z0-9][a-z0-9_.-]{0,127}$/;

/** The router folds every key with `strip().lower()` before matching. Folding
 *  here too means the form shows the author the value that will actually be
 *  stored, rather than accepting "Annual_Review" and silently storing something
 *  else. */
export const foldKey = (value: string): string => (value ?? '').trim().toLowerCase();

/** Null when the key is fine; otherwise the sentence to show under the field.
 *  Deliberately the same rule the router enforces, so the form and the 400 can
 *  never disagree about what a key is. */
export function keyProblem(value: string, label: string): string | null {
    const key = foldKey(value);
    if (!key) return `${label} is required.`;
    if (key.length > MAX_KEY_CHARS) return `${label} must be ${MAX_KEY_CHARS} characters or fewer.`;
    if (!SCRIPT_KEY_RE.test(key)) {
        return `${label} must be lowercase letters, digits, underscores or hyphens — no spaces (try “${suggestKey(value)}”).`;
    }
    return null;
}

/** Null when the target is fine or empty (empty is a complete answer). */
export function targetProblem(value: string): string | null {
    const target = foldKey(value);
    if (!target) return null;
    if (target.length > MAX_TARGET_CHARS) return `HubSpot target must be ${MAX_TARGET_CHARS} characters or fewer.`;
    if (!SCRIPT_TARGET_RE.test(target)) return 'HubSpot target must look like “company.industry” or “industry”.';
    return null;
}

/** Turn a question into a slug an author can accept or overwrite.
 *
 *  This exists because typing a `fact_key` is the hardest part of this form for
 *  the people it is FOR: product owners know the products, not the join
 *  keys. The suggestion is offered only into an EMPTY field and stays editable —
 *  a fact key is matched exactly and never fuzzily, so an author who wants it to
 *  read `hours_per_unit` must always be able to say so. */
export function suggestKey(text: string, max = 48): string {
    const slug = (text ?? '')
        .toLowerCase()
        .replace(/['’]/g, '')
        .replace(/[^a-z0-9]+/g, '_')
        .replace(/^_+|_+$/g, '');
    if (slug.length <= max) return slug;
    // Cut at a word boundary so the suggestion still reads as words.
    const cut = slug.slice(0, max);
    const lastBreak = cut.lastIndexOf('_');
    return (lastBreak > 8 ? cut.slice(0, lastBreak) : cut).replace(/_+$/, '');
}

// ─── derived state, in ONE place ─────────────────────────────────────────────

/** What this version IS. See `SCRIPT_STATES` for why the order of these tests
 *  is not arbitrary. Every screen asks this function and none re-derives it —
 *  the editor saying "live" about a version the AE's panel does not render is a
 *  lie the author has no way to check. */
export function scriptState(script: ScriptSummary): ScriptState {
    if (!script.is_active) return 'retired';
    if (!script.approved_by) return 'draft';
    return script.is_live ? 'approved' : 'superseded';
}

export const scriptStateLabel = (script: ScriptSummary): string => SCRIPT_STATE_LABELS[scriptState(script)];

/** True when a save would FORK rather than edit in place — rule 2. Approval, not
 *  liveness, is the trigger: a superseded-but-approved version forks too,
 *  because some finished call ran under it. */
export const savingForks = (script: ScriptSummary): boolean => Boolean(script.approved_by);

/** One product and all of its versions, newest first.
 *
 *  PRODUCT order is the server's, first-seen wins — the catalogue arrives sorted
 *  `product_key ASC, version DESC` and re-sorting it here would be a second
 *  opinion that can drift. VERSION order is re-asserted rather than assumed,
 *  because "newest first" is load-bearing for the group's name and the
 *  eye-scan, and a list that quietly came back in another order would show the
 *  wrong version's name as the product's. */
export interface ScriptProductGroup {
    product_key: string;
    /** The newest version's name — the one an author recognises. Versions can be
     *  renamed, so this is "what it is called now", not a stable identifier. */
    name: string;
    versions: ScriptSummary[];
    /** The live version, when this product has one. Absent means no approved,
     *  active script exists yet: the normal early state, and it means AEs see NO
     *  checklist for this product. */
    live: ScriptSummary | null;
}

export function groupByProduct(scripts: ScriptSummary[]): ScriptProductGroup[] {
    const order: string[] = [];
    const byKey = new Map<string, ScriptSummary[]>();
    for (const script of scripts) {
        const key = script.product_key;
        if (!byKey.has(key)) {
            byKey.set(key, []);
            order.push(key);
        }
        byKey.get(key)?.push(script);
    }
    return order.map(key => {
        const versions = [...(byKey.get(key) ?? [])].sort((a, b) => b.version - a.version);
        return {
            product_key: key,
            name: versions[0]?.name || key,
            versions,
            live: versions.find(v => v.is_live) ?? null,
        };
    });
}

// ─── the six calls ───────────────────────────────────────────────────────────

/** The catalogue, WITHOUT items (rule 6).
 *
 *  `includeDrafts` is not a view preference — it is a permission. Without it the
 *  server returns LIVE scripts only, one row per product, which is what an AE
 *  may see; with it, every version of every script including unapproved and
 *  retired ones, and it 403s for anyone outside the authoring gate. A draft in
 *  front of an AE is a model's guess wearing the company's checklist.
 *
 *  Call it WITHOUT drafts first when you do not already know the caller's role:
 *  the response carries `can_author`, which is the server's own answer. */
export async function fetchScripts(opts?: {
    productKey?: string | null;
    includeDrafts?: boolean;
}): Promise<ScriptCatalogue> {
    const params: Record<string, string | boolean> = {};
    const productKey = foldKey(opts?.productKey ?? '');
    if (productKey) params.product_key = productKey;
    if (opts?.includeDrafts) params.include_drafts = true;
    const { data } = await client.get<Partial<ScriptCatalogue>>('/api/scripts', { params });
    return {
        scripts: data?.scripts ?? [],
        // Defaults FALSE: a malformed envelope must read as "you may not author",
        // never as permission. The controls stay hidden and the server, which is
        // the actual gate, is never asked to refuse a click.
        can_author: data?.can_author === true,
    };
}

/** The LIVE approved script for one product — the checklist an AE would be
 *  shown, items included. 404s (`SCRIPT_NOT_FOUND`) when the product has nothing
 *  but drafts, which is the honest answer rather than an error. */
export async function fetchActiveScript(productKey: string): Promise<ScriptDetail> {
    const { data } = await client.get<ScriptDetail>(`/api/scripts/${encodeURIComponent(foldKey(productKey))}`);
    return data;
}

/** Create a DRAFT. It lands unapproved whatever else is set, so nothing created
 *  here reaches an AE until somebody signs it. A product that already has
 *  scripts gets the NEXT version rather than a rejection. */
export async function createScript(body: ScriptCreate): Promise<ScriptDetail> {
    const { data } = await client.post<ScriptDetail>('/api/scripts', body);
    return data;
}

/** Edit a draft in place, or FORK an approved script into a new version.
 *
 *  The caller MUST read `script_id` off the result rather than reusing the one it
 *  sent: on a fork they differ, and approving the id you sent would approve the
 *  version you were trying to replace. */
export async function updateScript(scriptId: string, body: ScriptUpdate): Promise<ScriptSaveResult> {
    const { data } = await client.put<ScriptSaveResult>(`/api/scripts/${encodeURIComponent(scriptId)}`, body);
    return data;
}

/** Sign a script — THE ONLY THING THAT MAKES ONE LIVE.
 *
 *  Takes no body: `approved_by` is stamped from the authenticated caller and can
 *  never be supplied, because a signature a caller can supply is not a signature.
 *  Refuses an empty checklist (400 — a script with no items reports every call as
 *  complete) and a retired version (409). */
export async function approveScript(scriptId: string): Promise<ScriptApproveResult> {
    const { data } = await client.post<ScriptApproveResult>(`/api/scripts/${encodeURIComponent(scriptId)}/approve`, {});
    return data;
}

/** Retire a script by clearing `is_active`. NOTHING IS EVER HARD-DELETED: a
 *  finished call points at the exact version it ran under and recomputes its gap
 *  list from that version's items, so deleting one would turn an explicable call
 *  into an unexplainable one. Reversible through `updateScript(id, { is_active:
 *  true })`, which is why that one field edits an approved script in place
 *  instead of forking. */
export async function deactivateScript(scriptId: string): Promise<ScriptRetireResult> {
    const { data } = await client.delete<ScriptRetireResult>(`/api/scripts/${encodeURIComponent(scriptId)}`);
    return data;
}

/** Restore a retired version. Sent ALONE — see rule 4. */
export async function restoreScript(scriptId: string): Promise<ScriptSaveResult> {
    return updateScript(scriptId, { is_active: true });
}
