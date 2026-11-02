// How Cubie hands an AE over to Meet so the Cubie add-on is there when they land.
//
// The add-on is installed on the AE's WORK Google account. Plenty of people are
// signed into a personal account in the same browser too, and Meet opens a link
// on whichever account the browser picks — land on the wrong one and the Cubie
// add-on is simply missing from Activities, with nothing on screen saying why.
// `authuser=<email>` is Meet's own way to choose the account for a link, so the
// Join button sets it. An `authuser` already on the link is the organizer's
// choice and is left alone; anything that is not a meet.google.com link is
// returned untouched rather than rewritten.

/** What Today's Calls needs to hand an AE over to Meet with the add-on in
 *  reach: the WORK account to open Meet on. Present only for people Cubie
 *  answers inside Meet (`features.sales_meet_addon`); null for everyone else,
 *  whose Join link stays exactly as the calendar wrote it. */
export interface MeetHandoff {
    accountEmail: string | null;
}

export function meetJoinUrl(uri: string, accountEmail: string | null | undefined): string {
    const email = (accountEmail || '').trim();
    if (!email) return uri;
    try {
        const url = new URL(uri);
        if (url.protocol !== 'https:' || url.hostname !== 'meet.google.com') return uri;
        if (url.searchParams.has('authuser')) return uri;
        url.searchParams.set('authuser', email);
        return url.toString();
    } catch {
        return uri;
    }
}

/** The one-line pointer Today's Calls shows beside Join. Google gives an
 *  add-on no way to open itself, so the honest instruction is the click. */
export const MEET_PANEL_HINT =
    'Your checklist is in Meet too: once you join, open Activities (the shapes icon, ' +
    'bottom right) and choose Cubie.';
