// Where a signed-out person goes after signing in, when they arrived on a What's
// New link (the "See it" link in the morning Slack post) or on the Chrome
// extension's Connect page (its Connect button; sessions last hours, so the
// person is often signed out by the time they press it). Without this the
// sign-in lands them on the home page and the page they clicked is lost.
//
// App.tsx's ProtectedRoute remembers the link when it sends someone to /login;
// the /login route sends them back to it once they have a token; the link's own
// route (pages/WhatsNewLink.tsx, pages/ChromeConnect.tsx) forgets it on
// arrival. Only those exact paths are ever remembered or followed, so this
// can't be turned into a redirect to anywhere else. Per tab (sessionStorage), and every access is in try/catch: a
// blocked store only costs the return trip.
//
// Import-free so it runs under Node's test runner (`returnTo.test.ts`).

export const RETURN_TO_KEY = 'whatsNew:returnTo';

/** The only paths followed after sign-in: a release note (a changelog entry
 *  id, the rule `isEntryId` in api/whatsNew.ts keeps) with nothing after it
 *  but the Slack marker; or the Chrome Connect page, exactly. */
export const RETURN_TO_PATTERN = /^(\/whats-new\/[A-Za-z0-9][A-Za-z0-9_-]{0,63}(\?src=slack)?|\/chrome\/connect)$/;

export const isWhatsNewReturnTo = (path: unknown): path is string =>
    typeof path === 'string' && RETURN_TO_PATTERN.test(path);

const sessionStore = (): Storage | null => {
    try {
        return globalThis.sessionStorage ?? null;
    } catch {
        return null;
    }
};

/** Remember `pathname + search` when it is a What's New link; anything else is
 *  ignored (the sign-in then lands on the home page as before). */
export function rememberReturnTo(
    pathname: string,
    search: string,
    storage: Storage | null = sessionStore(),
): void {
    const path = `${pathname}${search}`;
    if (!isWhatsNewReturnTo(path)) return;
    try {
        storage?.setItem(RETURN_TO_KEY, path);
    } catch {
        /* a blocked store costs the return trip, nothing else */
    }
}

/** The remembered link, if there is a valid one. Reads only: the link's own
 *  route clears it once the person has arrived. */
export function peekReturnTo(storage: Storage | null = sessionStore()): string | null {
    try {
        const path = storage?.getItem(RETURN_TO_KEY) ?? null;
        return isWhatsNewReturnTo(path) ? path : null;
    } catch {
        return null;
    }
}

export function clearReturnTo(storage: Storage | null = sessionStore()): void {
    try {
        storage?.removeItem(RETURN_TO_KEY);
    } catch {
        /* nothing to do */
    }
}
