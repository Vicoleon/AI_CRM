// The client's CRM and X-Ray picture, beside the brief.
//
// Built server-side from the HubSpot and Customer X-Ray records verbatim — the
// model never writes these numbers — so this card shows them as they are and
// links back to where they live. Nothing renders when the meeting has no
// client behind it.
import React from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, Building2, ExternalLink, Radar, Trophy } from 'lucide-react';

import { CHURN_BAND_COLOR } from '../../api/xray';
import {
    formatMoney,
    formatShortDate,
    snapshotCompanies,
    type AccountSnapshot,
    type SnapshotCompany,
    type SnapshotDeal,
} from '../../pages/meetingPrep/snapshot';
import { formatVersionWhen } from '../../pages/meetingPrep/sections';
import { SectionCard } from './SectionCard';

const MaybeLink: React.FC<{ href?: string | null; className?: string; children: React.ReactNode }> = ({
    href,
    className,
    children,
}) =>
    href ? (
        <a href={href} target="_blank" rel="noopener noreferrer" className={className}>
            {children}
        </a>
    ) : (
        <span className={className}>{children}</span>
    );

const DealRow: React.FC<{ deal: SnapshotDeal }> = ({ deal }) => {
    const facts = [
        deal.stage,
        formatMoney(deal.amount),
        deal.close_date ? `closes ${formatShortDate(deal.close_date) ?? deal.close_date}` : null,
        deal.owner,
    ].filter(Boolean);
    return (
        <li className="rounded-xl border border-slate-100 bg-slate-50 px-3 py-2">
            <MaybeLink
                href={deal.url}
                className="flex items-start gap-1.5 text-sm font-semibold text-slate-800 hover:text-brand"
            >
                <span className="min-w-0">{deal.name || 'Untitled deal'}</span>
                {deal.url && <ExternalLink className="mt-0.5 h-3 w-3 shrink-0 text-slate-400" />}
            </MaybeLink>
            {facts.length > 0 && <p className="mt-0.5 text-xs text-slate-500">{facts.join(' · ')}</p>}
        </li>
    );
};

const LastContact: React.FC<{ label: string; date?: string | null }> = ({ label, date }) => (
    <div className="flex-1 rounded-xl border border-slate-100 bg-slate-50 px-3 py-2">
        <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">{label}</p>
        <p className="text-sm font-semibold text-slate-700">{formatShortDate(date) ?? '—'}</p>
    </div>
);

const CompanyBlock: React.FC<{ company: SnapshotCompany }> = ({ company }) => {
    const deals = company.open_deals ?? [];
    const xray = company.xray;
    const bandColor = xray?.churn_band ? CHURN_BAND_COLOR[xray.churn_band] : undefined;
    const showWon = company.won_deals_count !== null && company.won_deals_count !== undefined;
    const ambiguityNote =
        company.note ||
        (company.ambiguous
            ? 'More than one HubSpot company could match this meeting — check this is the right one.'
            : null);

    return (
        <div className="space-y-3 border-b border-slate-100 pb-4 last:border-0 last:pb-0">
            <div>
                <div className="flex flex-wrap items-center gap-2">
                    <MaybeLink
                        href={company.hubspot_url}
                        className="inline-flex items-center gap-1.5 text-base font-black text-slate-900 hover:text-brand"
                    >
                        {company.name}
                        {company.hubspot_url && <ExternalLink className="h-3.5 w-3.5 text-slate-400" />}
                    </MaybeLink>
                    {company.lifecycle_stage && (
                        <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-brand">
                            {company.lifecycle_stage}
                        </span>
                    )}
                </div>
                {company.domain && <p className="text-xs text-slate-400">{company.domain}</p>}
            </div>

            {ambiguityNote && (
                <p className="flex items-start gap-1.5 rounded-lg bg-amber-50 px-2.5 py-1.5 text-xs text-amber-800">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    {ambiguityNote}
                </p>
            )}

            <div>
                <p className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                    Open deals{deals.length ? ` (${deals.length})` : ''}
                </p>
                {deals.length ? (
                    <ul className="space-y-1.5">
                        {deals.map((d, i) => (
                            <DealRow key={`${d.name}-${i}`} deal={d} />
                        ))}
                    </ul>
                ) : (
                    <p className="text-xs italic text-slate-400">No open deals.</p>
                )}
            </div>

            {(showWon || company.last_won) && (
                <p className="flex items-start gap-1.5 text-xs text-slate-600">
                    <Trophy className="mt-0.5 h-3.5 w-3.5 shrink-0 text-brand" />
                    <span>
                        {showWon && (
                            <>
                                {company.won_deals_count} won deal{company.won_deals_count === 1 ? '' : 's'}
                            </>
                        )}
                        {company.last_won?.name && (
                            <>
                                {showWon ? ' · last: ' : 'Last won: '}
                                <span className="font-semibold">{company.last_won.name}</span>
                                {company.last_won.close_date &&
                                    ` (${formatShortDate(company.last_won.close_date) ?? company.last_won.close_date})`}
                            </>
                        )}
                    </span>
                </p>
            )}

            {(company.deal_last_contact || company.company_last_contact) && (
                <div>
                    <p className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-400">Last contact</p>
                    <div className="flex gap-2">
                        <LastContact label="Deal" date={company.deal_last_contact} />
                        <LastContact label="Company" date={company.company_last_contact} />
                    </div>
                </div>
            )}

            {xray && (
                <div className="rounded-xl border border-slate-200 bg-white p-3">
                    <div className="flex flex-wrap items-center gap-2">
                        <Radar className="h-4 w-4 text-brand" />
                        {xray.churn_band && (
                            <span
                                className="rounded-full px-2 py-0.5 text-[11px] font-bold text-white"
                                style={{ background: bandColor ?? '#64748b' }}
                            >
                                {xray.churn_band}
                                {typeof xray.churn_score === 'number' ? ` · ${Math.round(xray.churn_score)}` : ''}
                            </span>
                        )}
                        <Link
                            to={xray.url}
                            className="ml-auto text-xs font-bold text-brand hover:underline"
                        >
                            Open X-Ray v{xray.version}
                        </Link>
                    </div>
                    {xray.summary && (
                        <p className="mt-2 line-clamp-3 text-xs leading-relaxed text-slate-600">{xray.summary}</p>
                    )}
                    {xray.created_at && (
                        <p className="mt-1 text-[10px] text-slate-400">X-Ray from {formatVersionWhen(xray.created_at)}</p>
                    )}
                </div>
            )}
        </div>
    );
};

export const AccountSnapshotCard: React.FC<{ snapshot?: AccountSnapshot | null }> = ({ snapshot }) => {
    const companies = snapshotCompanies(snapshot);
    if (!companies.length) return null;
    return (
        <SectionCard icon={Building2} title="Account Snapshot" className="bg-white border-slate-200 shadow-sm">
            <div className="space-y-4">
                <p className="text-xs text-slate-400">
                    Straight from HubSpot and Customer X-Ray
                    {snapshot?.as_of ? ` · as of ${formatVersionWhen(snapshot.as_of)}` : ''}.
                </p>
                {companies.map((c, i) => (
                    <CompanyBlock key={`${c.hubspot_company_id || c.domain || c.name}-${i}`} company={c} />
                ))}
            </div>
        </SectionCard>
    );
};
