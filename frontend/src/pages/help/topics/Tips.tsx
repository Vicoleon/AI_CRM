import React from 'react';
import { PRODUCT_NAME, SUPPORT_EMAIL, VENDOR_NAME } from '../../../brand/brand';

// Help → Tips & feedback. Served at /help/tips; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const Tips: React.FC = () => (
    <>
        <ul className="list-disc pl-5 space-y-1">
            <li>Use the 👍 / 😐 / 👎 buttons under Chat answers, on AI Priority cards, and at the bottom of a Meeting Prep briefing. Ratings guide future answers and help admins spot quality issues; older answer text and comments are not replayed into new prompts.</li>
            <li>You never upload meeting transcripts — they sync on their own every ~15 minutes.</li>
            <li>Gmail, Calendar and Drive features use your Google Workspace login; Slack features need Slack connected in Settings.</li>
            <li>You stay signed in for <span className="font-medium">5 hours</span> at a time. When the session ends, Cubie simply brings you back to the sign-in page; one click on “Sign in with Google” and you&apos;re back. Admins can change the session length in Administration → Google Workspace.</li>
            <li>Ask follow-up questions naturally — Chat remembers the conversation.</li>
            <li>
                Questions or something not working right? Reach out to your {PRODUCT_NAME} admin.
                Admins can write to {VENDOR_NAME} support at{' '}
                <a
                    href={`mailto:${SUPPORT_EMAIL}`}
                    className="rounded-sm text-brand hover:text-brand-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                >
                    {SUPPORT_EMAIL}
                </a>
                .
            </li>
        </ul>
    </>
);
