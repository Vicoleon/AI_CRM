// The Proposal page's brief copy, by where the proposal came from.
//
// A proposal drafted from a call says "what Cubie heard on the call". One
// with no call session (its call was since deleted) must not, so it gets
// neutral copy. The page reads the origin from the proposal itself (the API
// exposes no `brief_source`), with the same rule its back link uses.
//
// Pure and import-free so it runs under `node --test` (briefCopy.test.ts).

export type ProposalOrigin = 'call' | 'other';

export function proposalOrigin(p: { call_session_id?: string | null } | null | undefined): ProposalOrigin {
    return p?.call_session_id ? 'call' : 'other';
}

export interface BriefCopy {
    /** Under "1. Confirm the brief". */
    intro: string;
    scopeHint: string;
    /** A scope item that is not confirmed and carries a quote. */
    scopeInferredQuoted: string;
    /** A scope item that is not confirmed and has no quote. */
    scopeUnconfirmed: string;
    outcomesHint: string;
    /** An outcome with no quote from the prospect. */
    outcomeAdded: string;
    outcomesEmpty: string;
    pricingHint: string;
    /** A pricing line with no proposal price. */
    priceMissing: string;
    openQuestionsTitle: string;
}

/** Call-origin copy: unchanged from the page as it shipped. */
const CALL: BriefCopy = {
    intro: 'What Cubie heard on the call. Fix anything wrong, fill anything missing, then confirm. The document is built only from this.',
    scopeHint: 'Only what was discussed on the call. Cubie will not add to this list.',
    scopeInferredQuoted: 'Cubie inferred this — check it',
    scopeUnconfirmed: 'Not heard on the call — added by hand or inferred',
    outcomesHint: 'Goals the prospect stated, in their words. Cubie only lists what it heard them say.',
    outcomeAdded: 'Added by you — the prospect did not say this on the call',
    outcomesEmpty: 'Cubie did not hear the prospect state a goal. Add one if you know it; the section is left out otherwise.',
    pricingHint: 'Only figures that were said. The table in the document prints exactly these.',
    priceMissing: 'No price was said on the call — fill it in or remove the line',
    openQuestionsTitle: 'Not settled on the call — confirm before sending',
};

const OTHER: BriefCopy = {
    intro: 'Fix anything wrong, fill anything missing, then confirm. The document is built only from this.',
    scopeHint: 'What your company would do. Cubie will not add to this list.',
    scopeInferredQuoted: 'Cubie inferred this — check it',
    scopeUnconfirmed: 'Added by hand — confirm with the client',
    outcomesHint: 'Goals the prospect stated, in their own words.',
    outcomeAdded: 'Added by you — check these are the prospect’s own words',
    outcomesEmpty: 'Add the goals the prospect stated, in their own words; the section is left out otherwise.',
    pricingHint: 'Set each proposal price; the table prints exactly these.',
    priceMissing: 'Set the proposal price or remove the line',
    openQuestionsTitle: 'Confirm before sending',
};

export function briefCopy(origin: ProposalOrigin): BriefCopy {
    return origin === 'call' ? CALL : OTHER;
}
