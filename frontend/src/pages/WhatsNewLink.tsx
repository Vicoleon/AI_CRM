// The What's New deep link. Not a page: it opens the drawer and sends the
// person home.
//
//   /whats-new/:itemId — "See it" in the morning Slack post (with
//                        ?src=slack). Counts the Slack arrival, opens the
//                        drawer on that release note.
//
// Routed inside ProtectedRoute + Layout but NOT FullAppRoute (App.tsx): the
// channel is company-wide, so an EOS-only reader must land too. Home is '/',
// and FullAppRoute already sends someone without core access on to their L10
// meetings once the profile has loaded; the drawer's state lives in a module
// store, so it survives the navigation. A signed-out reader comes back here
// after signing in (components/whatsNew/returnTo.ts), and arriving forgets the
// remembered link.
//
// One effect does the whole job. In development React runs it twice
// (StrictMode): opening the drawer twice is the same as once, and the server
// counts an open once per person per note, so the second POST is a no-op.
import React, { useEffect } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { NO_LONGER_AVAILABLE, isEntryId, recordOpen } from '../api/whatsNew';
import { clearReturnTo } from '../components/whatsNew/returnTo';
import { openWhatsNew } from '../components/whatsNew/store';

export const WhatsNewItemLink: React.FC = () => {
    const { itemId } = useParams();
    const [searchParams] = useSearchParams();
    const navigate = useNavigate();
    const fromSlack = searchParams.get('src') === 'slack';

    useEffect(() => {
        clearReturnTo();
        if (!isEntryId(itemId)) {
            openWhatsNew({ notice: NO_LONGER_AVAILABLE });
        } else {
            // Only a Slack arrival is counted here; a failure (the note is for
            // another team, or gone) never blocks the drawer, which says so
            // itself.
            if (fromSlack) void recordOpen(itemId, 'slack').catch(() => {});
            openWhatsNew({ itemId, from: fromSlack ? 'slack' : undefined });
        }
        navigate('/', { replace: true });
    }, [itemId, fromSlack, navigate]);

    return null;
};
