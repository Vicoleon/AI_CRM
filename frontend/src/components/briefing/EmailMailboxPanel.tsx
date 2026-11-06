import React, { useEffect, useState } from 'react';
import { AlertCircle, Loader2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import client from '../../api/client';
import { selectedGmailAccount, type GmailAccountsResponse } from '../../email/gmailAccounts';
import type { EmailSummary } from './types';
import { InboxColumn } from './InboxColumn';

interface InboxResponse {
    account_id: string | null;
    items: EmailSummary[];
}

/** Secondary mailbox reads are opt-in and never enter the work Homefeed cache. */
export const EmailMailboxPanel: React.FC<{
    workEmails?: EmailSummary[];
    workLoading: boolean;
    workEmail?: string;
    refreshNonce: number;
}> = ({ workEmails, workLoading, workEmail, refreshNonce }) => {
    const [accounts, setAccounts] = useState<GmailAccountsResponse | null>(null);
    const [accountsError, setAccountsError] = useState(false);
    const [accountId, setAccountId] = useState<string | null>(null);
    const [extraResult, setExtraResult] = useState<{
        requestKey: string;
        items: EmailSummary[];
        error: boolean;
    } | null>(null);

    useEffect(() => {
        let active = true;
        client.get<GmailAccountsResponse>('/api/email/accounts')
            .then(({ data }) => { if (active) setAccounts(data); })
            .catch(() => { if (active) setAccountsError(true); });
        return () => { active = false; };
    }, []);

    const selected = selectedGmailAccount(accounts, accountId);
    const selectedId = selected?.id ?? null;
    const requestKey = JSON.stringify([selectedId, refreshNonce]);
    const extraLoading = selectedId !== null && extraResult?.requestKey !== requestKey;
    const extraError = selectedId !== null && extraResult?.requestKey === requestKey && extraResult.error;
    useEffect(() => {
        if (!selectedId) return;
        let active = true;
        client.get<InboxResponse>('/api/email/inbox', { params: { account_id: selectedId } })
            .then(({ data }) => {
                if (!active) return;
                if (data.account_id !== selectedId) {
                    setExtraResult({ requestKey, items: [], error: true });
                    return;
                }
                setExtraResult({ requestKey, items: data.items, error: false });
            })
            .catch(() => { if (active) setExtraResult({ requestKey, items: [], error: true }); });
        return () => { active = false; };
    }, [selectedId, requestKey]);

    const mailboxEmail = selected?.email ?? accounts?.default_account ?? workEmail;
    return (
        <>
            {accounts?.enabled && (
                <div data-tour="productivity-email-account" className="flex flex-wrap items-center gap-3 border-b border-gray-100 px-5 py-3">
                    <label htmlFor="productivity-mailbox" className="text-xs font-medium text-gray-600">Mailbox</label>
                    <select
                        id="productivity-mailbox"
                        value={selectedId ?? ''}
                        onChange={event => setAccountId(event.target.value || null)}
                        className="min-w-0 max-w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                    >
                        <option value="">{accounts.default_account} · work</option>
                        {accounts.accounts.filter(account => account.status === 'connected').map(account => (
                            <option key={account.id} value={account.id}>{account.email}</option>
                        ))}
                    </select>
                    <Link to="/settings?tab=email" className="text-xs font-medium text-brand hover:underline">Manage accounts</Link>
                </div>
            )}
            {accountsError && <p role="alert" className="flex items-center gap-2 px-5 py-2 text-xs text-amber-700"><AlertCircle size={13} />Additional mailboxes are unavailable; showing your work inbox.</p>}
            {extraError && <p role="alert" className="flex items-center gap-2 px-5 py-3 text-sm text-red-700"><AlertCircle size={15} />Could not load this mailbox. Reconnect it in Settings or try Refresh.</p>}
            {extraLoading && <p className="flex items-center gap-2 px-5 py-3 text-sm text-gray-500"><Loader2 size={15} className="animate-spin" />Loading {mailboxEmail}…</p>}
            {!extraError && <InboxColumn emails={selectedId ? (extraResult?.requestKey === requestKey ? extraResult.items : []) : workEmails} loading={selectedId ? extraLoading : workLoading} mailboxEmail={mailboxEmail} />}
        </>
    );
};
