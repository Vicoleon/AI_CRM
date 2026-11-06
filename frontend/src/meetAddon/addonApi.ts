// The side panel's own sign-in and its one API call.
//
// WHY THE PANEL SIGNS IN BY ITSELF. It is an iframe inside meet.google.com, and
// browsers partition an embedded page's storage by the site around it. So the
// Cubie session in the AE's Cubie tab is invisible here — same origin, different
// partition — and the panel has to make its own, with the same two steps as
// `pages/Login.tsx`: a Google ID credential from GIS, exchanged once
// at `POST /api/auth/session` for a Cubie session JWT. The JWT is kept under
// its own key so a developer opening the panel page directly never shares (or
// clobbers) the main app's `token`.
//
// Plain `fetch`, deliberately not the app's axios client: that client's 401
// handler sends the page to /login, and inside Meet that would navigate the
// side panel to the Cubie login screen.
import { jwtDecode } from 'jwt-decode';
import { classifyFailure, normalizeContext, type AddonContext, type LoadFailure } from './panelState';
import { MEET_ADDON_TOKEN_KEY, readMeetAddonToken } from '../brand/storage';
import { exchangeGoogleCredential } from '../auth/session';

/** Storage can be refused outright inside a third-party iframe (a browser set
 *  to block third-party site data). The panel then keeps the session in memory
 *  and the AE signs in again the next time the panel opens — never a crash. */
let memoryToken: string | null = null;

function usable(token: string | null): boolean {
    if (!token) return false;
    try {
        const { exp } = jwtDecode<{ exp?: number }>(token);
        // Same 60-second buffer as the app's client: never send a token that
        // expires mid-flight.
        return !exp || exp * 1000 > Date.now() + 60_000;
    } catch {
        return false;
    }
}

export function readToken(): string | null {
    // Storage refused or empty — fall back to memory (see above).
    const stored = readMeetAddonToken() ?? memoryToken;
    if (usable(stored)) return stored;
    clearToken();
    return null;
}

export function storeToken(token: string): void {
    memoryToken = token;
    try {
        window.localStorage.setItem(MEET_ADDON_TOKEN_KEY, token);
    } catch {
        // Memory only for this panel load.
    }
}

export function clearToken(): void {
    memoryToken = null;
    try {
        window.localStorage.removeItem(MEET_ADDON_TOKEN_KEY);
    } catch {
        // Nothing stored to clear.
    }
}

async function detail(res: Response): Promise<string | null> {
    const body: unknown = await res.json().catch(() => null);
    if (body && typeof body === 'object' && 'detail' in body) {
        const value = (body as { detail?: unknown }).detail;
        if (typeof value === 'string') return value;
    }
    return null;
}

/** GIS ID credential → Cubie session JWT. The flagged Meet compatibility flow
 * may pass a one-time access token; the backend verifies that path online. */
export const exchangeForSession = exchangeGoogleCredential;

export class AddonLoadError extends Error {
    readonly failure: LoadFailure;

    constructor(failure: LoadFailure) {
        super('message' in failure ? failure.message : failure.kind);
        this.failure = failure;
    }
}

/** `GET /api/meet-addon/context` for one meeting code. Rejects with an
 *  `AddonLoadError` whose `failure` says which of the distinct cases it was. */
export async function fetchAddonContext(meetingCode: string, token: string): Promise<AddonContext> {
    let res: Response;
    try {
        res = await fetch(`/api/meet-addon/context?meeting_code=${encodeURIComponent(meetingCode)}`, {
            headers: {
                Authorization: `Bearer ${token}`,
                'X-Request-ID': crypto.randomUUID().slice(0, 8),
            },
        });
    } catch {
        throw new AddonLoadError({
            kind: 'error',
            message: 'Cubie could not be reached. Check your connection and try again.',
        });
    }
    if (!res.ok) throw new AddonLoadError(classifyFailure(res.status, await detail(res)));
    return normalizeContext(await res.json());
}
