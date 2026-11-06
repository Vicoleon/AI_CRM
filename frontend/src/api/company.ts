// Admin → Company setup: the company profile, the product/service catalog and
// the proposal templates.
//
// The server is `backend/routers/company.py` (prefix /company, mounted under
// /api on the copilot gate). Everyone the copilot lets in may read; only an
// admin may save. The profile feeds every AI prompt that needs to know what
// the company does, and with none saved the prompts say "the company". The
// catalog and the templates feed proposals, Customer X-Ray and Meeting Prep.
import client from './client';

export const COMPANY_PROFILE_PATH = '/api/company/profile';
export const COMPANY_CATALOG_PATH = '/api/company/catalog';
export const COMPANY_CATALOG_IMPORT_PATH = '/api/company/catalog/import';
export const PROPOSAL_TEMPLATES_PATH = '/api/company/proposal-templates';

/** The first row of a catalog upload, in this order. Only `name` is required. */
export const CATALOG_CSV_HEADER = 'name,description,category,price,unit,url';

/** GET / PUT `/api/company/profile`. Every field may be empty. */
export interface CompanyProfile {
    name: string;
    legal_name: string;
    website: string;
    address: string;
    blurb: string;
    industry: string;
    offering: string;
    customers: string;
    sales_process: string;
    /** Main products or services, one short line each. */
    products: string[];
}

export const emptyCompanyProfile = (): CompanyProfile => ({
    name: '',
    legal_name: '',
    website: '',
    address: '',
    blurb: '',
    industry: '',
    offering: '',
    customers: '',
    sales_process: '',
    products: [],
});

/** One catalog row (`catalog.row_to_dict`). */
export interface CatalogItem {
    name: string;
    description: string;
    category: string;
    /** A plain decimal ("1200.00"), or null when the row has no price. */
    price: string | null;
    unit: string;
    url: string;
    /** "$1,200/month", or "" when the row has no price. */
    price_label: string;
}

/** POST `/api/company/catalog/import`: all or nothing. */
export interface CatalogImportResult {
    /** Rows now in the catalog; 0 when the file had errors. */
    created: number;
    /** "row 3: name is required", one per problem. */
    errors: string[];
}

/** A proposal template (`templates.template_to_dict`). */
export interface ProposalTemplateRow {
    id: number;
    /** Stable slug the proposal brief stores; it never changes on rename. */
    key: string;
    name: string;
    body_md: string;
    catalog_item_names: string[];
    active: boolean;
}

export type ProposalTemplateDraft = Pick<ProposalTemplateRow, 'name' | 'body_md' | 'catalog_item_names' | 'active'>;

export const emptyTemplateDraft = (): ProposalTemplateDraft => ({
    name: '',
    body_md: '',
    catalog_item_names: [],
    active: true,
});

export async function fetchCompanyProfile(): Promise<CompanyProfile> {
    const { data } = await client.get<CompanyProfile>(COMPANY_PROFILE_PATH);
    return { ...emptyCompanyProfile(), ...data };
}

export async function saveCompanyProfile(profile: CompanyProfile): Promise<CompanyProfile> {
    const { data } = await client.put<CompanyProfile>(COMPANY_PROFILE_PATH, profile);
    return { ...emptyCompanyProfile(), ...data };
}

export async function fetchCatalog(): Promise<CatalogItem[]> {
    const { data } = await client.get<{ items: CatalogItem[] }>(COMPANY_CATALOG_PATH);
    return data.items;
}

export async function importCatalog(csvText: string): Promise<CatalogImportResult> {
    const { data } = await client.post<CatalogImportResult>(COMPANY_CATALOG_IMPORT_PATH, csvText, {
        headers: { 'Content-Type': 'text/csv' },
    });
    return data;
}

export async function fetchProposalTemplates(): Promise<ProposalTemplateRow[]> {
    const { data } = await client.get<{ templates: ProposalTemplateRow[] }>(PROPOSAL_TEMPLATES_PATH);
    return data.templates;
}

export async function createProposalTemplate(draft: ProposalTemplateDraft): Promise<ProposalTemplateRow> {
    const { data } = await client.post<ProposalTemplateRow>(PROPOSAL_TEMPLATES_PATH, draft);
    return data;
}

export async function updateProposalTemplate(id: number, draft: ProposalTemplateDraft): Promise<ProposalTemplateRow> {
    const { data } = await client.put<ProposalTemplateRow>(`${PROPOSAL_TEMPLATES_PATH}/${id}`, draft);
    return data;
}

export async function deleteProposalTemplate(id: number): Promise<void> {
    await client.delete(`${PROPOSAL_TEMPLATES_PATH}/${id}`);
}

/** The server's sentence for a refused save, or `fallback`. */
export function companyAdminError(err: unknown, fallback: string): string {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return typeof detail === 'string' && detail.trim() ? detail : fallback;
}

/** Products as the textarea shows them (one per line) and back. */
export const productsToText = (products: string[]): string => products.join('\n');
export const textToProducts = (text: string): string[] =>
    text
        .split('\n')
        .map(line => line.trim())
        .filter(Boolean);

/** The name Cubie copy uses for the company: the profile's, else "your company". */
export const companyNameOrDefault = (name: string | null | undefined, fallback = 'your company'): string =>
    (name ?? '').trim() || fallback;
