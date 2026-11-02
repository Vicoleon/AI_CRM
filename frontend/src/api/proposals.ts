// Proposal Generator API client — start a proposal from a captured call,
// confirm its brief, generate a version, revise it, download it.
//
// Uses the shared axios instance (bearer token + request id attached by its
// interceptor); full /api/... paths, no baseURL, and no timeout of our own —
// client.ts is 105s, which the model steps here need (a brief extraction over
// a long transcript runs tens of seconds).
//
// EVERY TYPE IN HERE IS TRANSCRIBED FROM backend/routers/proposals.py and
// rubik_core/proposals/brief.py. The router is the server; where the two ever
// disagree the router wins and this file changes.
import { filenameFromDisposition } from '../utils/download';
import client from './client';

// ─── the brief ───────────────────────────────────────────────────────────────

export interface Person {
    name: string;
    title: string;
    company?: string;
    email?: string;
}

export interface ScopeItem {
    id: string;
    title: string;
    detail: string;
    /** A short verbatim quote that established the item, or ''. */
    quote: string;
    /** True when the call established it as fact; false is Cubie's inference
     *  (flagged on the form) or something the AE added by hand. */
    confirmed: boolean;
}

export interface DesiredOutcome {
    id: string;
    outcome: string;
    /** Verbatim words the prospect used. '' when the AE wrote the outcome. */
    quote: string;
    entry_sequence: number | null;
    confirmed: boolean;
}

export interface PricingLine {
    id: string;
    description: string;
    /** As a person would write them: "$6,500", "$225/hr", or ''. */
    standard_rate: string;
    proposal_price: string;
    /** Always false while proposal_price is empty — the server enforces it. */
    confirmed: boolean;
}

/** Which of the company's proposal templates the document carries.
 *  key '' = none: the proposal is the AI-written sections alone. */
export interface BriefTemplate {
    key: string;
}

export interface Brief {
    schema_version?: string;
    date: string;
    ref_code: string;
    prepared_for: Person;
    prepared_by: Person;
    service_title: string;
    engagement_summary: string;
    scope_items: ScopeItem[];
    desired_outcomes: DesiredOutcome[];
    pricing: PricingLine[];
    notes: string[];
    timeline: string;
    open_questions: string[];
    /** Absent on briefs stored before templates existed; the router fills it. */
    template?: BriefTemplate;
}

// ─── The company's templates (GET /api/proposals/templates) ─────────────────

export interface TemplatePriceOption {
    description: string;
    /** The catalog's list price, e.g. "$1,200/month". */
    standard_rate: string;
}

/** An active template, as the brief form offers it. Admins write them in
 *  Admin → Company setup; the list is empty until they do. */
export interface ProposalTemplate {
    key: string;
    name: string;
    /** The headings of its write-up, which prints after the scope of work. */
    headings: string[];
    /** Its catalog items that carry a price. */
    price_options: TemplatePriceOption[];
}

// ─── the content of one version ──────────────────────────────────────────────

export interface ContentBlock {
    id: string;
    heading: string;
    body: string;
}

export interface Content {
    document_title: string;
    headline: string;
    executive_summary: string;
    scope_intro: string;
    scope: ContentBlock[];
    outcomes_intro: string;
    outcomes: ContentBlock[];
    pricing_intro: string;
    notes: string[];
    closing: string;
}

export interface ProposalVersion {
    id: string;
    version_no: number;
    /** The AE's instruction that produced this version; null on one generated
     *  straight from the brief. Together with change_summary this IS the
     *  revision history. */
    instruction: string | null;
    change_summary: string | null;
    model: string | null;
    created_by: string;
    created_at: string | null;
    brief: Brief;
    content: Content;
}

export type ProposalStatus = 'brief' | 'drafted' | 'archived';

export interface Proposal {
    id: string;
    owner_email: string;
    call_session_id: string | null;
    hubspot_company_id: string | null;
    hubspot_deal_id: string | null;
    company_name: string | null;
    title: string;
    status: ProposalStatus;
    brief: Brief;
    brief_confirmed_at: string | null;
    /** Advisory: what still stops this brief from producing a sendable
     *  document. Shown above Generate; never blocks it. */
    gaps: string[];
    versions: ProposalVersion[];
    created_at: string | null;
    updated_at: string | null;
}

// ─── errors ──────────────────────────────────────────────────────────────────

export interface ProposalsError {
    status: number | null;
    message: string;
}

export function proposalsError(e: unknown, fallback = 'Cubie could not reach the proposal service.'): ProposalsError {
    const err = e as { response?: { status?: number; data?: { detail?: unknown } } };
    const status = err?.response?.status ?? null;
    const detail = err?.response?.data?.detail;
    const message = typeof detail === 'string' && detail.trim() ? detail : fallback;
    return { status, message };
}

/** 404 with the router's own words: the feature is switched off here. */
export const isNotEnabled = (e: ProposalsError): boolean =>
    e.status === 404 && /not enabled/i.test(e.message);

// ─── calls ───────────────────────────────────────────────────────────────────

export async function fetchProposalForCall(
    sessionId: string,
): Promise<{ proposal_id: string | null; status: ProposalStatus | null }> {
    const { data } = await client.get(`/api/proposals/for-call/${encodeURIComponent(sessionId)}`);
    return data;
}

export async function startProposalFromCall(sessionId: string): Promise<Proposal> {
    const { data } = await client.post<{ proposal: Proposal }>(
        `/api/proposals/from-call/${encodeURIComponent(sessionId)}`,
    );
    return data.proposal;
}

/** The Sales Coach's "Continue in Cubie": start (or open) the proposal for a
 *  Fathom recording. 404 = Cubie has not received the recording; 403 = another
 *  AE's call; 409 = Fathom has it but it is not a client call (see detail). */
export async function startProposalFromFathom(
    recordingId: string,
): Promise<{ proposal: Proposal; session_id: string }> {
    const { data } = await client.post(
        `/api/proposals/from-fathom/${encodeURIComponent(recordingId)}`,
    );
    return data;
}

export async function fetchProposalTemplates(): Promise<ProposalTemplate[]> {
    const { data } = await client.get<{ templates: ProposalTemplate[] }>('/api/proposals/templates');
    return data.templates;
}

export async function fetchProposal(proposalId: string): Promise<Proposal> {
    const { data } = await client.get<{ proposal: Proposal }>(
        `/api/proposals/${encodeURIComponent(proposalId)}`,
    );
    return data.proposal;
}

export async function saveBrief(proposalId: string, brief: Brief): Promise<Proposal> {
    const { data } = await client.put<{ proposal: Proposal }>(
        `/api/proposals/${encodeURIComponent(proposalId)}/brief`,
        { brief },
    );
    return data.proposal;
}

export async function generateProposal(
    proposalId: string,
): Promise<{ proposal: Proposal; version: ProposalVersion }> {
    const { data } = await client.post(`/api/proposals/${encodeURIComponent(proposalId)}/generate`);
    return data;
}

export async function reviseProposal(
    proposalId: string,
    instruction: string,
): Promise<{ proposal: Proposal; version: ProposalVersion }> {
    const { data } = await client.post(`/api/proposals/${encodeURIComponent(proposalId)}/revise`, {
        instruction,
    });
    return data;
}

export async function downloadProposal(
    proposalId: string,
    versionNo: number,
    format: 'pdf' | 'docx',
): Promise<{ blob: Blob; filename: string }> {
    const res = await client.get(
        `/api/proposals/${encodeURIComponent(proposalId)}/versions/${versionNo}/download`,
        { params: { format }, responseType: 'blob' },
    );
    const disposition = res.headers?.['content-disposition'];
    const filename = filenameFromDisposition(disposition, `proposal_v${versionNo}.${format}`);
    return { blob: res.data as Blob, filename };
}

export async function archiveProposal(proposalId: string): Promise<Proposal> {
    const { data } = await client.post<{ proposal: Proposal }>(
        `/api/proposals/${encodeURIComponent(proposalId)}/archive`,
    );
    return data.proposal;
}

// ─── lists (the Proposals page) ──────────────────────────────────────────────

export interface ProposalSummary {
    id: string;
    owner_email: string;
    call_session_id: string | null;
    company_name: string | null;
    prepared_for: string | null;
    title: string;
    status: ProposalStatus;
    brief_confirmed_at: string | null;
    versions: number;
    created_at: string | null;
    updated_at: string | null;
}

export interface CapturedCall {
    call_session_id: string;
    ae_email: string;
    company_name: string | null;
    hubspot_company_id: string | null;
    /** Fathom's meeting title when the call came from a Fathom recording. */
    title: string | null;
    origin: string;
    status: string;
    ended_at: string | null;
    wrapped_at: string | null;
    proposal_id: string | null;
    proposal_status: ProposalStatus | null;
}

export async function fetchProposals(
    all = false,
): Promise<{ proposals: ProposalSummary[]; scope: 'mine' | 'all' }> {
    const { data } = await client.get('/api/proposals', { params: all ? { all: true } : {} });
    return data;
}

export async function fetchCapturedCalls(
    opts: { days?: number; all?: boolean } = {},
): Promise<{ calls: CapturedCall[]; scope: 'mine' | 'all'; days: number }> {
    const { data } = await client.get('/api/proposals/calls', {
        params: { days: opts.days ?? 14, ...(opts.all ? { all: true } : {}) },
    });
    return data;
}

// ─── helpers ─────────────────────────────────────────────────────────────────

/** A fresh, empty brief item with a client-side id the server will keep. */
let seq = 0;
export const newId = (prefix: 's' | 'o' | 'p'): string => `${prefix}new${++seq}`;

export const emptyScopeItem = (): ScopeItem => ({
    id: newId('s'),
    title: '',
    detail: '',
    quote: '',
    confirmed: false,
});

export const emptyOutcome = (): DesiredOutcome => ({
    id: newId('o'),
    outcome: '',
    quote: '',
    entry_sequence: null,
    confirmed: false,
});

export const emptyPricingLine = (): PricingLine => ({
    id: newId('p'),
    description: '',
    standard_rate: '',
    proposal_price: '',
    confirmed: false,
});

/** The brief's template choice after the AE picks ``key`` ('' = none). */
export const chooseTemplate = (templates: ProposalTemplate[], key: string): BriefTemplate => ({
    key: templates.some(x => x.key === key) ? key : '',
});

/** A pricing line seeded from a template's catalog list price. The proposal
 *  price stays blank: the AE sets it, as with every seeded line. */
export const pricingLineFromOption = (option: TemplatePriceOption): PricingLine => ({
    ...emptyPricingLine(),
    description: option.description,
    standard_rate: option.standard_rate,
});

export const latestVersion = (p: Proposal | null | undefined): ProposalVersion | null =>
    p && p.versions.length ? p.versions[p.versions.length - 1] : null;
