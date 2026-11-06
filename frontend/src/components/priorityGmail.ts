import { gmailThreadUrl } from '../email/gmailAccounts.ts';

export interface PriorityGmailAttrs {
    gmail_mailbox?: string | null;
    gmail_account_id?: string | null;
    personal_gmail?: boolean | null;
    private_gmail_context?: boolean | null;
}

function singleMailboxAddress(attrs: PriorityGmailAttrs | null | undefined): string | null {
    const mailbox = attrs?.gmail_mailbox?.trim();
    // A card can cite several mailboxes. Never guess which account should
    // receive a reply or compose action in that case.
    if (!mailbox || mailbox.includes(',')) return null;
    return /^[^\s@,]+@[^\s@,]+\.[^\s@,]+$/.test(mailbox) ? mailbox : null;
}

function singlePersonalMailbox(attrs: PriorityGmailAttrs | null | undefined): string | null {
    if (!attrs?.personal_gmail || !attrs.gmail_account_id) return null;
    return singleMailboxAddress(attrs);
}

/** Resolve only a thread whose namespaced ID matches the cited account. */
export function personalPriorityThreadUrl(
    attrs: PriorityGmailAttrs | null | undefined,
    entityId: string | null | undefined,
): string | null {
    const mailbox = singlePersonalMailbox(attrs);
    const prefix = `${attrs?.gmail_account_id}:`;
    if (!mailbox || !entityId?.startsWith(prefix)) return null;
    const rawThreadId = entityId.slice(prefix.length);
    if (!rawThreadId || !/^[A-Za-z0-9_-]+$/.test(rawThreadId)) return null;
    return gmailThreadUrl(mailbox, rawThreadId);
}

/** Compose from one verified personal mailbox; mixed-mailbox cards have no action. */
export function personalPriorityComposeUrl(
    attrs: PriorityGmailAttrs | null | undefined,
    recipient: string | null | undefined,
): string | null {
    const mailbox = singlePersonalMailbox(attrs);
    if (!mailbox || !recipient || !/^[^\s@,]+@[^\s@,]+\.[^\s@,]+$/.test(recipient)) return null;
    const params = new URLSearchParams({ authuser: mailbox, view: 'cm', fs: '1', to: recipient });
    return `https://mail.google.com/mail/?${params.toString()}`;
}

/** Mail actions require a single cited mailbox, even for ordinary work mail. */
export function priorityThreadUrl(
    attrs: PriorityGmailAttrs | null | undefined,
    entityId: string | null | undefined,
): string | null {
    if (attrs?.personal_gmail) return personalPriorityThreadUrl(attrs, entityId);
    const mailbox = singleMailboxAddress(attrs);
    if (!mailbox || !entityId || !/^[A-Za-z0-9_-]+$/.test(entityId)) return null;
    return gmailThreadUrl(mailbox, entityId);
}

export function priorityComposeUrl(
    attrs: PriorityGmailAttrs | null | undefined,
    recipient: string | null | undefined,
): string | null {
    if (attrs?.personal_gmail) return personalPriorityComposeUrl(attrs, recipient);
    const mailbox = singleMailboxAddress(attrs);
    if (!mailbox || !recipient || !/^[^\s@,]+@[^\s@,]+\.[^\s@,]+$/.test(recipient)) return null;
    const params = new URLSearchParams({ authuser: mailbox, view: 'cm', fs: '1', to: recipient });
    return `https://mail.google.com/mail/?${params.toString()}`;
}
