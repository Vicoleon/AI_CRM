// The side panel's pure logic — everything that decides WHAT the panel says,
// kept apart from the Meet SDK and from React so `node --test` can pin it.
//
// The payload is `backend/routers/meet_addon.py::build_context`. Three rules the
// rest of the panel leans on, and each one is a way this screen could otherwise
// lie to an AE who is talking to a client:
//
// 1. "WE COULD NOT LOOK" IS NEVER "THERE IS NOTHING". A calendar outage, a
//    script table that could not be read and a meeting Cubie does not know are
//    three different sentences. `normalizeContext` keeps the flags that tell
//    them apart and never defaults one to the reassuring value.
// 2. ONE SECTION PER PRODUCT, IN THE ORDER THE CALL CARRIES THEM. The server's
//    `selected` (what the call is attached to, or what was remembered for this
//    client) comes first and cannot be removed here; a product the AE adds in
//    the panel comes after and says it is not attached to the call.
// 3. A TICK IS THE AE'S, NOT CUBIE'S. Cubie is not listening to the call. Ticks
//    live in this panel only (see `localStateKey`), and nothing here ever
//    reports a fact as covered.
import type { ScriptSection } from '../api/calls';

export type AddonMatch = 'calendar' | 'session' | 'none';
export type ProductsSource = 'call' | 'remembered' | 'none';
export type CompanyState = 'confirmed' | 'suggested' | 'uncertain';

export interface AddonCompany {
    state: CompanyState;
    hubspot_company_id: string | null;
    name: string | null;
    domain: string | null;
    /** On `uncertain` only: the calendar row's own verdict (`ambiguous`,
     *  `not_found`, `skipped`, `error`). */
    reason?: string;
}

export interface AddonCall {
    title: string | null;
    start_local: string | null;
    is_live: boolean;
    call_session_id: string | null;
    capture_state: string;
    confidential: boolean;
}

export interface AddonSelected {
    product_key: string;
    name: string;
    has_checklist: boolean;
}

export interface AddonContext {
    meeting_code: string;
    match: AddonMatch;
    calendar_available: boolean;
    calls_sharing_code: number;
    call: AddonCall | null;
    company: AddonCompany | null;
    products_source: ProductsSource;
    selected: AddonSelected[];
    catalogue_available: boolean;
    catalogue: ScriptSection[];
}

const MATCHES: readonly AddonMatch[] = ['calendar', 'session', 'none'];
const SOURCES: readonly ProductsSource[] = ['call', 'remembered', 'none'];
const COMPANY_STATES: readonly CompanyState[] = ['confirmed', 'suggested', 'uncertain'];

const isObject = (value: unknown): value is Record<string, unknown> =>
    typeof value === 'object' && value !== null && !Array.isArray(value);

const str = (value: unknown): string | null =>
    typeof value === 'string' && value.length > 0 ? value : null;

function normalizeSection(value: unknown): ScriptSection | null {
    if (!isObject(value)) return null;
    const productKey = str(value.product_key);
    if (!productKey) return null;
    const items = Array.isArray(value.items) ? value.items : [];
    return {
        script_id: str(value.script_id) ?? '',
        product_key: productKey,
        name: str(value.name) ?? productKey,
        items: items.filter(isObject).flatMap(item => {
            const factKey = str(item.fact_key);
            if (!factKey) return [];
            return [
                {
                    fact_key: factKey,
                    prompt_text: str(item.prompt_text) ?? factKey,
                    // Required is the default and optional the marked case —
                    // the same asymmetry the server applies.
                    required: item.required !== false,
                    help_text: str(item.help_text),
                },
            ];
        }),
    };
}

/** The wire payload, made safe to render. Throws only when it is not the
 *  add-on's payload at all — a partial one renders its honest parts. */
export function normalizeContext(raw: unknown): AddonContext {
    if (!isObject(raw) || !str(raw.meeting_code)) {
        throw new Error('Cubie sent an answer this panel cannot read.');
    }
    const match = MATCHES.includes(raw.match as AddonMatch) ? (raw.match as AddonMatch) : 'none';
    const source = SOURCES.includes(raw.products_source as ProductsSource)
        ? (raw.products_source as ProductsSource)
        : 'none';

    let company: AddonCompany | null = null;
    if (isObject(raw.company)) {
        const state = COMPANY_STATES.includes(raw.company.state as CompanyState)
            ? (raw.company.state as CompanyState)
            : 'uncertain';
        company = {
            state,
            // Only a confirmed or suggested company is ever named.
            hubspot_company_id: state === 'uncertain' ? null : str(raw.company.hubspot_company_id),
            name: state === 'uncertain' ? null : str(raw.company.name),
            domain: str(raw.company.domain),
            reason: str(raw.company.reason) ?? undefined,
        };
    }

    let call: AddonCall | null = null;
    if (isObject(raw.call)) {
        call = {
            title: str(raw.call.title),
            start_local: str(raw.call.start_local),
            is_live: raw.call.is_live === true,
            call_session_id: str(raw.call.call_session_id),
            capture_state: str(raw.call.capture_state) ?? 'not_started',
            confidential: raw.call.confidential === true,
        };
    }

    const selected: AddonSelected[] = [];
    for (const entry of Array.isArray(raw.selected) ? raw.selected : []) {
        if (!isObject(entry)) continue;
        const key = str(entry.product_key);
        if (!key || selected.some(s => s.product_key === key)) continue;
        selected.push({
            product_key: key,
            name: str(entry.name) ?? key,
            has_checklist: entry.has_checklist === true,
        });
    }

    const catalogue = (Array.isArray(raw.catalogue) ? raw.catalogue : [])
        .map(normalizeSection)
        .filter((s): s is ScriptSection => s !== null);

    return {
        meeting_code: str(raw.meeting_code) as string,
        match,
        // Missing means "we do not know", which must not read as "fine".
        calendar_available: raw.calendar_available === true,
        calls_sharing_code: typeof raw.calls_sharing_code === 'number' ? raw.calls_sharing_code : 0,
        call,
        company,
        products_source: source,
        selected,
        catalogue_available: raw.catalogue_available === true,
        catalogue,
    };
}

// ─── Why a load failed, in the four sentences that differ ────────────────────

export type LoadFailure =
    /** 401 — the panel's own session expired or was never made. */
    | { kind: 'signed_out' }
    /** 403 — Cubie answered, and this person is not on the add-on's roster. */
    | { kind: 'not_for_you'; message: string }
    /** 404 — the add-on is switched off in this environment. */
    | { kind: 'switched_off' }
    /** 422 — Meet handed over something that is not a meeting code. */
    | { kind: 'bad_code' }
    | { kind: 'error'; message: string };

export function classifyFailure(status: number, detail?: string | null): LoadFailure {
    if (status === 401) return { kind: 'signed_out' };
    if (status === 403) {
        return {
            kind: 'not_for_you',
            message: detail || 'Cubie in Google Meet is not enabled for your account yet.',
        };
    }
    if (status === 404) return { kind: 'switched_off' };
    if (status === 422) return { kind: 'bad_code' };
    return {
        kind: 'error',
        message: detail || 'Cubie could not load this call’s checklist. Try again in a moment.',
    };
}

// ─── What the panel renders ──────────────────────────────────────────────────

export type ProductOrigin = 'call' | 'remembered' | 'added';

export interface PanelProduct {
    product_key: string;
    name: string;
    /** Null when there is no approved checklist for it (yet) — still shown. */
    section: ScriptSection | null;
    origin: ProductOrigin;
}

/** Server-selected products first, in their order, then the AE's additions. */
export function panelProducts(ctx: AddonContext, added: readonly string[]): PanelProduct[] {
    const byKey = new Map(ctx.catalogue.map(section => [section.product_key, section]));
    const serverOrigin: ProductOrigin = ctx.products_source === 'call' ? 'call' : 'remembered';
    const out: PanelProduct[] = ctx.selected.map(s => ({
        product_key: s.product_key,
        name: byKey.get(s.product_key)?.name || s.name,
        section: byKey.get(s.product_key) ?? null,
        origin: serverOrigin,
    }));
    for (const key of added) {
        if (out.some(p => p.product_key === key)) continue;
        const section = byKey.get(key);
        // An addition whose checklist has since been retired drops out rather
        // than rendering an empty "added" section nobody can act on.
        if (!section) continue;
        out.push({ product_key: key, name: section.name, section, origin: 'added' });
    }
    return out;
}

/** The checklists the AE can still add — every live one not already shown. */
export function pickableSections(ctx: AddonContext, added: readonly string[]): ScriptSection[] {
    const shown = new Set(panelProducts(ctx, added).map(p => p.product_key));
    return ctx.catalogue.filter(section => !shown.has(section.product_key));
}

// ─── The AE's own ticks, kept in the panel ───────────────────────────────────

export interface LocalState {
    ticks: string[];
    added: string[];
}

export const EMPTY_LOCAL: LocalState = { ticks: [], added: [] };

/** One key per meeting code per LOCAL day. A recurring series reuses one code
 *  for every occurrence, so without the day next week's call would open with
 *  this week's questions already ticked. */
export const localStateKey = (meetingCode: string, localDay: string): string =>
    `cubie.meetAddon.v1.${meetingCode}.${localDay}`;

export const tickKey = (productKey: string, factKey: string): string =>
    `${productKey}:${factKey}`;

/** Tolerant: anything unreadable is an empty panel, never a crash mid-call. */
export function parseLocalState(raw: string | null): LocalState {
    if (!raw) return EMPTY_LOCAL;
    try {
        const parsed: unknown = JSON.parse(raw);
        if (!isObject(parsed)) return EMPTY_LOCAL;
        const strings = (value: unknown): string[] =>
            Array.isArray(value)
                ? [...new Set(value.filter((v): v is string => typeof v === 'string' && v.length > 0))]
                : [];
        return { ticks: strings(parsed.ticks), added: strings(parsed.added) };
    } catch {
        return EMPTY_LOCAL;
    }
}

export function toggleTick(state: LocalState, key: string): LocalState {
    const has = state.ticks.includes(key);
    return { ...state, ticks: has ? state.ticks.filter(k => k !== key) : [...state.ticks, key] };
}

export function addProduct(state: LocalState, productKey: string): LocalState {
    if (state.added.includes(productKey)) return state;
    return { ...state, added: [...state.added, productKey] };
}

export function removeProduct(state: LocalState, productKey: string): LocalState {
    return {
        ticks: state.ticks.filter(k => !k.startsWith(`${productKey}:`)),
        added: state.added.filter(k => k !== productKey),
    };
}

export interface SectionProgress {
    asked: number;
    total: number;
    /** Required questions not yet ticked — what the AE still has to raise. */
    requiredLeft: number;
}

export function sectionProgress(section: ScriptSection, ticks: readonly string[]): SectionProgress {
    const ticked = new Set(ticks);
    let asked = 0;
    let requiredLeft = 0;
    for (const item of section.items) {
        const done = ticked.has(tickKey(section.product_key, item.fact_key));
        if (done) asked += 1;
        else if (item.required) requiredLeft += 1;
    }
    return { asked, total: section.items.length, requiredLeft };
}
