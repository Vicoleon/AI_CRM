// Pure helpers for the Account Snapshot card and the "Built from" strip.
//
// The snapshot is built server-side from the CRM and X-Ray records verbatim
// (`rubik_core/meeting_prep/signals.py`), so the card's job is only to show
// those numbers faithfully — and to show nothing rather than an empty shell
// when the meeting has no client behind it.
//
// Import-free apart from `datetime.ts` (with its extension) so it runs under
// Node's test runner.

import { parseInstant } from '../../utils/datetime.ts';

export interface SnapshotDeal {
    name: string;
    stage?: string | null;
    amount?: number | null;
    close_date?: string | null;
    owner?: string | null;
    url?: string | null;
}

export interface SnapshotXray {
    version: number;
    churn_band?: string | null;
    churn_score?: number | null;
    summary?: string | null;
    created_at?: string | null;
    url: string;
}

export interface SnapshotCompany {
    name: string;
    domain?: string | null;
    hubspot_company_id?: string | null;
    hubspot_url?: string | null;
    lifecycle_stage?: string | null;
    open_deals?: SnapshotDeal[] | null;
    won_deals_count?: number | null;
    last_won?: { name: string; close_date?: string | null } | null;
    deal_last_contact?: string | null;
    company_last_contact?: string | null;
    xray?: SnapshotXray | null;
    ambiguous?: boolean | null;
    note?: string | null;
}

export interface AccountSnapshot {
    companies?: SnapshotCompany[] | null;
    as_of?: string | null;
}

export interface SignalUsed {
    key: string;
    label: string;
    detail?: string | null;
}

/** The companies worth a card: named ones only. `[]` for a null snapshot. */
export function snapshotCompanies(snapshot: AccountSnapshot | null | undefined): SnapshotCompany[] {
    return (snapshot?.companies ?? []).filter(c => !!c && !!(c.name || '').trim());
}

/** "$12,500" — whole dollars; `null` when there is no amount to show. */
export function formatMoney(amount: number | null | undefined): string | null {
    if (amount === null || amount === undefined || !Number.isFinite(amount)) return null;
    return new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
        maximumFractionDigits: 0,
    }).format(amount);
}

/** A CRM calendar date ("2026-10-15") as "Oct 15, 2026". A bare date is read
 *  as LOCAL midnight so it never slides to the previous day west of UTC. */
export function formatShortDate(value: string | null | undefined): string | null {
    const raw = (value ?? '').trim();
    if (!raw) return null;
    // A full timestamp: keep only its calendar date.
    const dateOnly = /^\d{4}-\d{2}-\d{2}/.test(raw) ? raw.slice(0, 10) : raw;
    const at = parseInstant(dateOnly);
    if (!at) return null;
    return at.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

/** "Gmail · 6 threads" — the chip text for one signal. */
export function signalChipText(signal: SignalUsed): string {
    const label = (signal.label || signal.key || '').trim();
    const detail = (signal.detail || '').trim();
    return detail ? `${label} · ${detail}` : label;
}

/** Signals worth a chip: the ones with a label to show. */
export function visibleSignals(signals: readonly SignalUsed[] | null | undefined): SignalUsed[] {
    return (signals ?? []).filter(s => !!s && !!(s.label || s.key || '').trim());
}
