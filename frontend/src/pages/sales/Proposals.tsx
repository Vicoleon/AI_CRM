/** My Work → Sales → Proposals — where a proposal is found again.
 *
 *  Two lists, and the second is the reason the page exists:
 *
 *  1. YOUR PROPOSALS — every proposal you started, newest first, with how many
 *     versions it has and whether the brief is confirmed. Open one to keep
 *     working on it.
 *  2. CAPTURED CALLS — the calls of the last two weeks Cubie has a transcript
 *     for, Fathom-recorded ones included, each with "Open proposal" or "Draft
 *     proposal". Today's Calls only lists PENDING wrap-ups, so a Fathom
 *     recording that arrived after you moved on, or a call you already wrapped,
 *     is otherwise invisible — and that is exactly the call a proposal is due
 *     for.
 *
 *  Admins get an "Everyone" switch for support. No transcript text is shown
 *  here; the wrap-up and the proposal own that, behind their own gates.
 */
import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { FileSignature, FileText, PhoneCall } from 'lucide-react';
import {
    fetchCapturedCalls,
    fetchProposals,
    isNotEnabled,
    proposalsError,
    type CapturedCall,
    type ProposalSummary,
} from '../../api/proposals';
import { useAuth } from '../../context/AuthContext';
import { DraftProposalCta } from '../../components/calls';
import { parseInstant } from '../../utils/datetime';
import { EmptyState, EosPageHeader, ErrorState, LoadingState, PageContainer, StatusBadge } from '../eos/_shared';

const CARD = 'rounded-lg border border-gray-200 bg-white shadow-sm';

const when = (iso: string | null): string => {
    if (!iso) return '';
    const d = parseInstant(iso);
    return d ? d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '';
};

const STATUS_TONE: Record<string, 'green' | 'amber' | 'blue' | 'gray'> = {
    brief: 'amber',
    drafted: 'green',
    archived: 'gray',
};

const ORIGIN_LABEL: Record<string, string> = {
    fathom: 'Fathom',
    calendar: 'Meet',
    cubie: 'Meet',
    adhoc: 'Meet',
};

export const Proposals: React.FC = () => {
    const { isAdmin, user } = useAuth();
    const [scopeAll, setScopeAll] = useState(false);
    const [proposals, setProposals] = useState<ProposalSummary[] | null>(null);
    const [calls, setCalls] = useState<CapturedCall[] | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        Promise.all([fetchProposals(scopeAll), fetchCapturedCalls({ days: 14, all: scopeAll })])
            .then(([p, c]) => {
                if (cancelled) return;
                setProposals(p.proposals);
                setCalls(c.calls);
            })
            .catch(e => {
                if (cancelled) return;
                const err = proposalsError(e, 'Cubie could not load your proposals.');
                setError(
                    isNotEnabled(err)
                        ? 'Proposal drafting is not switched on in this environment.'
                        : err.status === 403
                          ? 'Proposal drafting is not open to your team yet.'
                          : err.message,
                );
            });
        return () => {
            cancelled = true;
        };
    }, [scopeAll]);

    const loading = proposals === null && calls === null && !error;
    const me = (user?.email ?? '').toLowerCase();

    return (
        <PageContainer>
            <EosPageHeader
                icon={<FileSignature size={20} />}
                title="Proposals"
                subtitle="What a call produced for the prospect: your drafts, and the captured calls you could draft one from."
                right={
                    isAdmin ? (
                        <label className="flex items-center gap-2 text-xs text-gray-600">
                            <input
                                type="checkbox"
                                checked={scopeAll}
                                onChange={e => setScopeAll(e.target.checked)}
                                className="rounded border-gray-300 text-brand focus:ring-brand"
                            />
                            Everyone&apos;s (admin)
                        </label>
                    ) : undefined
                }
            />

            {loading && <LoadingState label="Loading proposals…" />}
            {error && <ErrorState message={error} />}

            {proposals !== null && (
                <section className={`${CARD} mb-6 overflow-hidden`} data-tour="proposals-list">
                    <header className="flex items-center gap-2 border-b border-gray-200 bg-gray-50 px-4 py-2.5">
                        <FileText size={15} className="text-brand" />
                        <h2 className="text-sm font-semibold text-gray-900">
                            {scopeAll ? 'All proposals' : 'Your proposals'}
                        </h2>
                        <span className="text-xs text-gray-500">{proposals.length}</span>
                    </header>
                    {proposals.length === 0 ? (
                        <div className="p-6">
                            <EmptyState
                                title="No proposals yet."
                                hint="Pick a captured call below and click Draft proposal. Cubie reads the call and fills the brief for you to confirm."
                            />
                        </div>
                    ) : (
                        <ul className="divide-y divide-gray-100">
                            {proposals.map(p => (
                                <li key={p.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                                    <div className="min-w-0 flex-1">
                                        <Link
                                            to={`/sales/proposals/${encodeURIComponent(p.id)}`}
                                            className="block truncate text-sm font-medium text-gray-900 hover:text-brand hover:underline"
                                        >
                                            {p.title || 'Proposal'}
                                        </Link>
                                        <p className="truncate text-xs text-gray-500">
                                            {[p.company_name, p.prepared_for].filter(Boolean).join(' · ') || '—'}
                                            {scopeAll && p.owner_email !== me ? ` · ${p.owner_email}` : ''}
                                        </p>
                                    </div>
                                    <StatusBadge
                                        value={p.status === 'brief' ? (p.brief_confirmed_at ? 'confirmed' : 'brief') : p.status}
                                        tone={STATUS_TONE[p.status] ?? 'gray'}
                                    />
                                    <span className="w-24 text-right text-xs text-gray-500">
                                        {p.versions === 0 ? 'not generated' : `${p.versions} version${p.versions === 1 ? '' : 's'}`}
                                    </span>
                                    <span className="w-36 text-right text-xs text-gray-400">{when(p.updated_at)}</span>
                                    <Link
                                        to={`/sales/proposals/${encodeURIComponent(p.id)}`}
                                        className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
                                    >
                                        Open
                                    </Link>
                                </li>
                            ))}
                        </ul>
                    )}
                </section>
            )}

            {calls !== null && (
                <section className={`${CARD} overflow-hidden`} data-tour="proposals-calls">
                    <header className="flex items-center gap-2 border-b border-gray-200 bg-gray-50 px-4 py-2.5">
                        <PhoneCall size={15} className="text-brand" />
                        <h2 className="text-sm font-semibold text-gray-900">Captured calls, last 14 days</h2>
                        <span className="text-xs text-gray-500">{calls.length}</span>
                        <span className="ml-auto text-xs text-gray-500">
                            Fathom recordings and Meet captures alike. Open the wrap-up to read the call.
                        </span>
                    </header>
                    {calls.length === 0 ? (
                        <div className="p-6">
                            <EmptyState
                                title="No captured calls in the last two weeks."
                                hint="Calls you start from Today's Calls, or that Fathom records for you, appear here once their transcript is in."
                            />
                        </div>
                    ) : (
                        <ul className="divide-y divide-gray-100">
                            {calls.map(c => (
                                <li key={c.call_session_id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                                    <div className="min-w-0 flex-1">
                                        <p className="truncate text-sm font-medium text-gray-900">
                                            {c.company_name || c.title || 'Call'}
                                            {c.company_name && c.title ? (
                                                <span className="ml-2 font-normal text-gray-500">{c.title}</span>
                                            ) : null}
                                        </p>
                                        <p className="truncate text-xs text-gray-500">
                                            {when(c.ended_at)}
                                            {scopeAll && c.ae_email !== me ? ` · ${c.ae_email}` : ''}
                                            {!c.company_name ? ' · company not matched — the wrap-up asks' : ''}
                                        </p>
                                    </div>
                                    <StatusBadge value={ORIGIN_LABEL[c.origin] ?? c.origin} tone="blue" />
                                    <StatusBadge
                                        value={c.wrapped_at ? 'wrapped' : c.status === 'extracted' ? 'wrap-up ready' : c.status}
                                        tone={c.wrapped_at ? 'gray' : 'amber'}
                                    />
                                    <Link
                                        to={`/calls/${encodeURIComponent(c.call_session_id)}/wrapup`}
                                        className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
                                    >
                                        Wrap-up
                                    </Link>
                                    {c.ae_email === me || isAdmin ? (
                                        <DraftProposalCta sessionId={c.call_session_id} emphasized={!c.proposal_id} />
                                    ) : null}
                                </li>
                            ))}
                        </ul>
                    )}
                </section>
            )}
        </PageContainer>
    );
};

export default Proposals;
