// The sales daily rhythm — My Day's Morning / Mid-day / End-of-day section.
//
// THE ROUTER IS THE CONTRACT: every wire string here was read out of
// `backend/routers/sales_rhythm.py` (and the reason codes out of
// `rubik_core/sales/rhythm_checks.py`). `salesRhythm.test.ts` re-derives the
// action kinds, the reason codes and the overflow questions from those files,
// so a drift fails a test instead of a rep's afternoon.
//
// Rows reuse the cockpit's `CockpitItem` shape (the router imports the
// cockpit's `Item` schema), so they render through the same `ActionRow`.
import client from './client';
import type { CockpitItem } from './cockpit';
import type { RhythmPart } from '../pages/chatSuggestions';

/** Both kinds are LINKS, and both lead to a write: `open_hubspot` opens the
 *  record in a new tab (where the rep books the step or fills the field) and
 *  `ask_cubie` opens Ask Cubie in-app with the draft request ready to run. */
export const RHYTHM_ACTION_KINDS = ['ask_cubie', 'open_hubspot'] as const;
export type RhythmActionKind = (typeof RHYTHM_ACTION_KINDS)[number];

export const isRhythmActionKind = (v: unknown): v is RhythmActionKind =>
    typeof v === 'string' && (RHYTHM_ACTION_KINDS as readonly string[]).includes(v);

/** "Cubie could not look", by cause. An empty list with no reason genuinely
 *  means nothing qualifies. */
export const RHYTHM_UNAVAILABLE_REASONS = [
    'crm_unavailable',
    'crm_read_failed',
    'no_hubspot_owner',
    'no_hubspot_user',
] as const;
export type RhythmUnavailableReason = (typeof RHYTHM_UNAVAILABLE_REASONS)[number];

export const RHYTHM_UNAVAILABLE_COPY: Record<RhythmUnavailableReason, string> = {
    crm_unavailable:
        'HubSpot could not be reached, so this check is unavailable rather than empty. Nothing here does not mean nothing is wrong.',
    crm_read_failed:
        'HubSpot answered, but the search behind this check did not finish, so it is unavailable rather than empty.',
    no_hubspot_owner:
        'No HubSpot owner matches your Cubie email, so Cubie cannot find your deals. Ask Sales Ops to line the two up.',
    no_hubspot_user:
        'Your HubSpot owner record has no HubSpot user behind it, so Cubie cannot tell which records you touched today. Ask Sales Ops to check your HubSpot seat.',
};

export const rhythmUnavailableCopy = (reason: string | null | undefined): string | null => {
    if (!reason) return null;
    if ((RHYTHM_UNAVAILABLE_REASONS as readonly string[]).includes(reason)) {
        return RHYTHM_UNAVAILABLE_COPY[reason as RhythmUnavailableReason];
    }
    return `Cubie could not run this check (${reason}). An empty list here does not mean there is nothing to do.`;
};

export interface RhythmBlock {
    key: string;
    label: string;
    hint: string;
    /** At most three (the cockpit's anti-tsunami cap). */
    items: CockpitItem[];
    /** How many more qualify. Renders as "+N more", linking to `ask_prompt`. */
    suppressed_count: number;
    /** The denominator: what the qualifying rows are out of. */
    scanned: number;
    scanned_label: string;
    unavailable_reason?: string | null;
    /** A caveat to read with the rows — print it whenever it is present. */
    note?: string | null;
    /** The starter-prompt wording of this check (the Ask Cubie chip). */
    ask_prompt: string;
    /** The Ask Cubie question behind "+N more" — it lists every qualifying row. */
    more_prompt: string;
    /** What an empty, available block means, in this check's own words. */
    empty_line: string;
}

export interface RhythmResponse {
    part: RhythmPart;
    blocks: RhythmBlock[];
    crm_available: boolean;
    /** End of day only. */
    recap_prompt?: string | null;
}

export async function fetchRhythm(part: RhythmPart): Promise<RhythmResponse> {
    const { data } = await client.get<RhythmResponse>(`/api/sales/rhythm/${part}`);
    return data;
}

/** The in-app Ask Cubie link that runs `prompt` on arrival (Chat reads `?q=`). */
export const askCubieHref = (prompt: string): string => `/chat?q=${encodeURIComponent(prompt)}`;
