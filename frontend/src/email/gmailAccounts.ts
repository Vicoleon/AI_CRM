/** Public account metadata only. OAuth tokens never reach the browser. */
export interface GmailAccount {
    id: string;
    email: string;
    status: string;
    is_default?: boolean;
    ai_processing_review_required?: boolean;
    ai_processing_unavailable?: boolean;
    ai_provider?: string;
    ai_model?: string;
    ai_provider_fingerprint?: string;
}

export interface GmailAccountsResponse {
    enabled: boolean;
    oauth_available?: boolean;
    default_account: string;
    accounts: GmailAccount[];
    ai_provider?: string;
    ai_model?: string;
    ai_provider_fingerprint?: string;
}

/** A selection is valid only while the current workspace response lists it. */
export function selectedGmailAccount(
    response: GmailAccountsResponse | null,
    accountId: string | null,
): GmailAccount | null {
    if (!response?.enabled || !accountId) return null;
    return response.accounts.find(account => account.id === accountId && account.status === 'connected') ?? null;
}

/** Address Gmail by email so identical thread IDs cannot open the wrong account. */
export function gmailThreadUrl(mailboxEmail: string, threadId: string): string {
    const query = new URLSearchParams({ authuser: mailboxEmail });
    return `https://mail.google.com/mail/?${query.toString()}#inbox/${encodeURIComponent(threadId)}`;
}
