import React, { useEffect, useState } from 'react';
import { AlertCircle, CheckCircle2, Link2, Loader2, Unlink } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import client from '../../api/client';
import type { GmailAccountsResponse } from '../../email/gmailAccounts';

const PROVIDER_NAME: Record<string, string> = {
    vertex_managed: 'Cubie-managed Vertex AI',
    vertex_client: 'your Vertex AI project',
    anthropic: 'Claude',
    deepseek: 'DeepSeek',
    openai: 'OpenAI',
    gemini: 'Gemini API',
    meta: 'Muse',
};

function modelName(provider?: string, model?: string): string {
    return `${PROVIDER_NAME[provider ?? ''] ?? 'the selected AI provider'}${model ? ` (${model})` : ''}`;
}

/** Each user connects only their own extra mailboxes. The work mailbox remains the default. */
export const GmailAccountsCard: React.FC = () => {
    const [searchParams] = useSearchParams();
    const connectionResult = searchParams.get('gmail_connection');
    const [accountState, setAccountState] = useState<GmailAccountsResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [reloadKey, setReloadKey] = useState(0);

    useEffect(() => {
        let active = true;
        client.get<GmailAccountsResponse>('/api/email/accounts')
            .then(({ data }) => { if (active) setAccountState(data); })
            .catch(() => { if (active) setError('Could not load your Gmail connections.'); })
            .finally(() => { if (active) setLoading(false); });
        return () => { active = false; };
    }, [reloadKey]);

    const connect = async () => {
        setBusy('connect');
        setError(null);
        try {
            const { data } = await client.post<{ authorization_url: string }>(
                '/api/email/accounts/connect',
                { fingerprint: accountState?.ai_provider_fingerprint },
            );
            const url = new URL(data.authorization_url);
            if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com') {
                throw new Error('Unexpected authorization destination');
            }
            window.location.assign(url.toString());
        } catch {
            setError('Could not start the Gmail connection. Try again.');
            setBusy(null);
        }
    };

    const disconnect = async (accountId: string) => {
        setBusy(accountId);
        setError(null);
        try {
            await client.delete(`/api/email/accounts/${encodeURIComponent(accountId)}`);
            const { data } = await client.get<GmailAccountsResponse>('/api/email/accounts');
            setAccountState(data);
        } catch {
            setError('Could not disconnect this Gmail account. Try again.');
        } finally {
            setBusy(null);
        }
    };

    const approveAiProvider = async (accountId: string, fingerprint: string) => {
        setBusy(`review:${accountId}`);
        setError(null);
        try {
            await client.post(`/api/email/accounts/${encodeURIComponent(accountId)}/ai-provider-consent`, { fingerprint });
            const { data } = await client.get<GmailAccountsResponse>('/api/email/accounts');
            setAccountState(data);
        } catch {
            setError('Could not confirm this AI provider. Refresh the page and try again.');
        } finally {
            setBusy(null);
        }
    };

    if (loading) return <div className="mt-8 flex items-center gap-2 text-sm text-gray-500"><Loader2 size={15} className="animate-spin" /> Loading Gmail accounts…</div>;
    if (!accountState && error) return (
        <div className="mt-8 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800" role="alert">
            <p>{error}</p>
            <button type="button" className="mt-2 font-semibold underline" onClick={() => { setError(null); setLoading(true); setReloadKey(key => key + 1); }}>Try again</button>
        </div>
    );
    if (!accountState || (!accountState.enabled && accountState.accounts.length === 0)) return null;

    return (
        <section data-tour="settings-gmail-accounts" className="mt-8 rounded-xl border border-gray-200 bg-white p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h3 className="text-base font-semibold text-gray-900">Connected Gmail accounts</h3>
                    <p className="mt-1 text-sm text-gray-600">
                        {accountState.enabled
                            ? `Your work mailbox stays the default. Choose another mailbox in Productivity → Email. AI Priorities currently uses ${modelName(accountState.ai_provider, accountState.ai_model)}.`
                            : 'Your admin has paused additional Gmail accounts. You can still disconnect accounts you connected earlier.'}
                    </p>
                </div>
                {accountState.enabled && (
                    <button type="button" onClick={connect} disabled={busy !== null || accountState.oauth_available === false} className="inline-flex items-center gap-2 rounded-md bg-brand px-3 py-2 text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50">
                        {busy === 'connect' ? <Loader2 size={15} className="animate-spin" /> : <Link2 size={15} />}
                        Connect another Gmail
                    </button>
                )}
            </div>
            {accountState.enabled && accountState.oauth_available === false && (
                <p role="alert" className="mt-3 flex items-center gap-2 text-sm text-amber-700"><AlertCircle size={15} />Your admin needs to finish Google Gmail connection setup before you can add another account. Your work mailbox remains available.</p>
            )}
            {error && <p role="alert" className="mt-3 flex items-center gap-2 text-sm text-red-700"><AlertCircle size={15} />{error}</p>}
            {connectionResult === 'connected' && <p role="status" className="mt-3 flex items-center gap-2 text-sm text-green-700"><CheckCircle2 size={15} />Gmail account connected.</p>}
            {connectionResult === 'denied' && <p role="alert" className="mt-3 flex items-center gap-2 text-sm text-amber-700"><AlertCircle size={15} />Google permission was not granted. No account was added.</p>}
            {connectionResult === 'failed' && <p role="alert" className="mt-3 flex items-center gap-2 text-sm text-red-700"><AlertCircle size={15} />The Gmail connection could not be completed. Please try again.</p>}
            <ul className="mt-4 divide-y divide-gray-100 rounded-md border border-gray-100">
                <li className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm">
                    <span className="font-medium text-gray-900">{accountState.default_account}</span>
                    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">Default work mailbox</span>
                </li>
                {accountState.accounts.map(account => (
                    <li key={account.id} className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm">
                        <div className="min-w-0 flex-1">
                            <p className="truncate font-medium text-gray-900">{account.email}</p>
                            {account.status === 'reauth_required' && (
                                <p className="text-xs text-amber-700" role="alert">
                                    Google access needs reconnection.{' '}
                                    {!accountState.enabled
                                        ? 'Your admin has paused extra accounts. You can disconnect this one now and reconnect when the option is restored.'
                                        : accountState.oauth_available === false
                                            ? 'Your admin needs to finish Google connection setup before you can reconnect.'
                                            : <>Choose Connect another Gmail above and select {account.email} at Google.</>}
                                    {' '}Until then, this mailbox is omitted from AI Priorities and scheduled summaries.
                                </p>
                            )}
                            {account.status !== 'connected' && account.status !== 'reauth_required' && <p className="text-xs text-amber-700">Connection needs attention</p>}
                            {accountState.enabled === false && <p className="text-xs text-amber-700">Paused by your workspace admin</p>}
                            {account.status === 'connected' && account.ai_processing_unavailable && (
                                <p className="mt-2 text-xs text-amber-700" role="alert">The selected AI provider is unavailable. Automatic mail processing is paused until your admin restores it.</p>
                            )}
                            {account.status === 'connected' && account.ai_processing_review_required && (
                                <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900" role="alert">
                                    <p>Your workspace changed its AI Priorities model to {modelName(account.ai_provider, account.ai_model)}. This mailbox stays available to open manually, but Cubie has paused its automatic AI Priorities and scheduled summaries until you agree to send its mail content to that model.</p>
                                    <button
                                        type="button"
                                        onClick={() => account.ai_provider_fingerprint && approveAiProvider(account.id, account.ai_provider_fingerprint)}
                                        disabled={busy !== null || !account.ai_provider_fingerprint || account.ai_processing_unavailable}
                                        className="mt-2 rounded-md bg-amber-800 px-2.5 py-1.5 font-medium text-white hover:bg-amber-900 disabled:opacity-50"
                                    >
                                        {busy === `review:${account.id}` ? 'Confirming…' : `Allow AI use of ${account.email}`}
                                    </button>
                                </div>
                            )}
                        </div>
                        <button type="button" onClick={() => disconnect(account.id)} disabled={busy !== null} className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50" aria-label={`Disconnect ${account.email}`}>
                            {busy === account.id ? <Loader2 size={13} className="animate-spin" /> : <Unlink size={13} />}
                            Disconnect
                        </button>
                    </li>
                ))}
            </ul>
            <p className="mt-3 text-xs text-gray-500">
                {accountState.enabled
                    ? <>Choose an extra mailbox to view its inbox in Productivity. Connected mailboxes are included automatically in your own AI Priorities and scheduled summaries, with the mailbox address shown on items that cite email. {modelName(accountState.ai_provider, accountState.ai_model)} processes included mail. If your admin changes that model, automatic inclusion pauses until you review it here. Personal mail stays out of team and manager views. Google&apos;s Gmail compose permission also permits sending; Cubie uses this connection for reading and creating drafts in the selected mailbox.</>
                    : <>Additional Gmail accounts are paused for this workspace, so they are not included in AI Priorities or scheduled summaries. You can disconnect an existing account above.</>}
            </p>
        </section>
    );
};
