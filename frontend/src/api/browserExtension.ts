// Cubie for Chrome — connecting and disconnecting browsers.
//
// THE ROUTER IS THE CONTRACT: every field here was read out of
// `backend/routers/browser_extension.py` and `connections.to_public` in
// `rubik_core/browser_extension/connections.py`. The web app only ever calls
// the three session-authed endpoints below; the extension's own calls (its
// new tab, its ticks) use the per-browser key and never pass through here.
//
// 404 means the feature is switched off (CHROME_EXTENSION_ENABLED), 403 that
// the reader is not on the `chrome_extension` roster. /api/users/me already
// folds both into `features.chrome_extension`, so a page that is gated on it
// only meets them if an admin changes the roster while the page is open.
//
// THE KEY. `connectBrowser` returns the raw key once. The Connect page hands it
// straight to the extension (`handToExtension`) and drops it: it is never put
// in React state, the URL, storage or a log.
import client from './client';

/** `connections.LABEL_MAX` — the server trims a longer label to this. */
export const LABEL_MAX = 60;

/** One connected browser, as Settings lists it. Never the key or its hash. */
export interface BrowserConnection {
    id: string;
    /** "Chrome on macOS", set when the browser connected. */
    label: string;
    /** The key's last four characters, so two browsers with the same label can
     *  be told apart. Shown as "…a1b2". */
    hint: string;
    /** Instants: ISO with a trailing Z. Render with formatInstant*. */
    created_at: string | null;
    /** Connections lapse 90 days after they are made. */
    expires_at: string | null;
    /** Roughly when the extension last read Cubie (stamped at most every ten
     *  minutes). Null until it first does. */
    last_used_at: string | null;
}

export interface ConnectResponse {
    /** Shown once. Hand it to the extension, then drop it. */
    token: string;
    connection: BrowserConnection;
}

const BASE = '/api/browser-extension/connections';

/** Mint a key for this browser. `replaces` is the connection the extension
 *  held before (it is revoked, so a reconnect never leaves the old key live). */
export async function connectBrowser(body: { label?: string; replaces?: string }): Promise<ConnectResponse> {
    const { data } = await client.post<ConnectResponse>(BASE, body);
    return data;
}

/** The reader's live connections, newest first. */
export async function listConnections(): Promise<BrowserConnection[]> {
    const { data } = await client.get<{ connections: BrowserConnection[] }>(BASE);
    return data.connections ?? [];
}

export async function disconnectBrowser(id: string): Promise<void> {
    await client.delete(`${BASE}/${encodeURIComponent(id)}`);
}

/** A sentence for a refused request: the server's own words for a 403 (they
 *  are written for the reader), a plain one for the switched-off 404. */
export function browserExtensionError(e: unknown, fallback: string): string {
    const res = (e as { response?: { status?: number; data?: { detail?: unknown } } })?.response;
    if (res?.status === 404) return 'Cubie for Chrome is switched off right now.';
    const detail = res?.data?.detail;
    if (res?.status === 403 && typeof detail === 'string' && detail) return detail;
    return fallback;
}

// ── This browser ─────────────────────────────────────────────────────────────

/** The connection's name in Settings: "Chrome on macOS", "Chrome on Windows",
 *  or just "Chrome". User-Agent Client Hints first; the user-agent string for
 *  a browser without them. */
export function browserLabel(nav: Navigator = navigator): string {
    const hinted = (nav as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ?? '';
    const ua = nav.userAgent ?? '';
    if (/mac/i.test(hinted) || (!hinted && /Macintosh|Mac OS X/.test(ua))) return 'Chrome on macOS';
    if (/win/i.test(hinted) || (!hinted && /Windows/.test(ua))) return 'Chrome on Windows';
    return 'Chrome';
}

// ── The handshake with the extension ────────────────────────────────────────
//
// The extension's content script runs on Cubie's own pages. At document_start
// it marks the page with two data attributes, which is how this page knows the
// extension is installed and which connection it holds:
//
//   <html data-cubie-for-chrome="<version>"
//         data-cubie-for-chrome-connection="<connection id, or empty>">
//
// The key travels by window.postMessage to this page's own origin, and the
// extension answers the same way. Both sides check the message came from this
// very window and origin, so another tab or a frame cannot speak for either.

// The message protocol, shared with chrome_extension/src/bridge.js: the page
// speaks as PAGE_SOURCE, the extension as EXTENSION_SOURCE. Both change together.
export const PAGE_SOURCE = 'cubie-web';
export const EXTENSION_SOURCE = 'cubie-for-chrome';
export const CONNECT_MESSAGE = 'cubie-for-chrome:connect';
export const CONNECTED_MESSAGE = 'cubie-for-chrome:connected';

/** How long the page waits for the extension to say it saved the key. */
export const HANDSHAKE_TIMEOUT_MS = 10_000;

export interface InstalledExtension {
    version: string;
    /** The connection the extension holds now; empty when it holds none. */
    connectionId: string;
}

/** The installed extension, read from the marks its content script left, or
 *  null when it is not installed (or not allowed on this site). */
export function readInstalledExtension(root: HTMLElement = document.documentElement): InstalledExtension | null {
    const version = root.dataset.cubieForChrome;
    if (!version) return null;
    return { version, connectionId: root.dataset.cubieForChromeConnection ?? '' };
}

export interface HandshakeResult {
    ok: boolean;
    /** The extension's own reason, when it had one. */
    error?: string;
    firstName?: string;
}

/** Post the key to the extension and wait for its answer. Resolves with the
 *  extension's answer, or `null` when none came within the timeout. The
 *  listener is in place before the key is posted, so a fast answer is never
 *  missed. `signal` lets the page stop waiting when it unmounts. */
export function handToExtension(
    token: string,
    connectionId: string,
    { timeoutMs = HANDSHAKE_TIMEOUT_MS, signal }: { timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<HandshakeResult | null> {
    return new Promise(resolve => {
        const finish = (result: HandshakeResult | null) => {
            window.removeEventListener('message', onMessage);
            window.clearTimeout(timer);
            signal?.removeEventListener('abort', onAbort);
            resolve(result);
        };
        const onMessage = (event: MessageEvent) => {
            if (event.source !== window || event.origin !== window.location.origin) return;
            const data = event.data as { source?: unknown; type?: unknown; ok?: unknown; error?: unknown; firstName?: unknown } | null;
            if (!data || data.source !== EXTENSION_SOURCE || data.type !== CONNECTED_MESSAGE) return;
            finish({
                ok: data.ok === true,
                error: typeof data.error === 'string' && data.error ? data.error.slice(0, 200) : undefined,
                firstName: typeof data.firstName === 'string' && data.firstName ? data.firstName.slice(0, 60) : undefined,
            });
        };
        const onAbort = () => finish(null);
        const timer = window.setTimeout(() => finish(null), timeoutMs);
        window.addEventListener('message', onMessage);
        signal?.addEventListener('abort', onAbort);
        window.postMessage(
            { source: PAGE_SOURCE, type: CONNECT_MESSAGE, token, connectionId },
            window.location.origin,
        );
    });
}
