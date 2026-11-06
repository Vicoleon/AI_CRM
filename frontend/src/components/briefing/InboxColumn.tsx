// Inbox column extracted from pages/Homefeed.tsx in UC-39 so it can be
// rendered both inside the legacy 3-column homefeed AND as a tab in the
// new /productivity route.
import React from 'react';
import { AlertCircle, Mail, ExternalLink, Inbox, Sparkles } from 'lucide-react';
import type { EmailSummary } from './types';
import { gmailThreadUrl } from '../../email/gmailAccounts';


function senderName(from: string): string {
    const match = from.match(/^"?([^"<]+)"?\s*</);
    return match ? match[1].trim() : from.split('@')[0];
}


export const InboxColumn: React.FC<{ emails?: EmailSummary[]; loading?: boolean; mailboxEmail?: string }> = ({
    emails,
    loading,
    mailboxEmail,
}) => {
    if (loading) {
        return (
            <div className="animate-pulse space-y-3 p-4">
                {Array.from({ length: 4 }).map((_, i) => (
                    <div key={i} className={`h-3 bg-gray-200 rounded ${i === 0 ? 'w-3/4' : i % 2 === 0 ? 'w-full' : 'w-5/6'}`} />
                ))}
            </div>
        );
    }
    if (!emails || emails.length === 0) {
        return (
            <div className="flex flex-col items-center justify-center py-12 text-gray-400">
                <Inbox size={32} className="mb-2 opacity-50" />
                <p className="text-sm">You're all caught up</p>
                <p className="text-xs mt-1 text-gray-300">No unread mail addressed to you — bulk mail is hidden</p>
            </div>
        );
    }
    return (
        <>
        {emails.some(email => email.importance_unavailable) && (
            <p role="status" className="flex items-center gap-2 border-b border-amber-100 bg-amber-50 px-5 py-2 text-xs text-amber-800">
                <AlertCircle size={13} />AI triage is unavailable. Your email is still shown without importance badges.
            </p>
        )}
        <ul className="divide-y divide-gray-50">
            {emails.map(email => (
                <li key={email.thread_id}>
                    <a
                        href={mailboxEmail
                            ? gmailThreadUrl(mailboxEmail, email.thread_id)
                            : `https://mail.google.com/mail/u/0/#inbox/${encodeURIComponent(email.thread_id)}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="block px-5 py-3.5 hover:bg-blue-50/40 transition-colors group"
                    >
                        <div className="flex items-start justify-between gap-2">
                            <div className="min-w-0">
                                <div className="flex items-center gap-1.5 min-w-0">
                                    <p className={`text-sm truncate group-hover:text-blue-700 transition-colors ${email.is_unread ? 'font-semibold text-gray-900' : 'text-gray-700'}`}>
                                        {email.subject}
                                    </p>
                                    {email.addressing === 'cc' && (
                                        <span
                                            title="You were Cc'd on this email"
                                            className="flex-shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded bg-gray-100 text-gray-500"
                                        >
                                            Cc
                                        </span>
                                    )}
                                </div>
                                {email.may_be_important && (
                                    <span
                                        title="Cubie thinks this may need your attention"
                                        className="inline-flex items-center gap-1 mt-1 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-100 text-amber-700"
                                    >
                                        <Sparkles size={10} />
                                        May be important
                                    </span>
                                )}
                                <p className="text-xs text-gray-500 truncate mt-0.5">
                                    {senderName(email.sender)}
                                </p>
                                <p className="text-xs text-gray-400 mt-1 line-clamp-2">{email.snippet}</p>
                            </div>
                            <div className="flex-shrink-0 flex flex-col items-end gap-1.5">
                                {email.is_unread && (
                                    <span className="w-2 h-2 rounded-full bg-blue-500 mt-1" />
                                )}
                                <ExternalLink size={11} className="text-gray-300 group-hover:text-blue-400 transition-colors" />
                            </div>
                        </div>
                    </a>
                </li>
            ))}
        </ul>
        </>
    );
};

export default InboxColumn;
export { Mail };
