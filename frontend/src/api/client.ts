import axios from 'axios';
import { jwtDecode } from 'jwt-decode';
import { activeToken, dropRejectedToken, readSupportToken } from '../brand/storage';

/**
 * Returns true when a stored token is present and not known to be expired.
 * A 60-second buffer is used so we don't send a token that will expire
 * mid-flight.
 */
export function isTokenValid(token: string | null): boolean {
    if (!token) return false;
    if (token === 'mock-token') return true;

    try {
        const decoded: { exp?: number } = jwtDecode(token);
        if (!decoded.exp) return true;
        // 60-second grace buffer
        return decoded.exp * 1000 > Date.now() + 60_000;
    } catch {
        // Local mock identity tokens can be opaque. The server will refuse any
        // other token it cannot verify.
        return true;
    }
}

const client = axios.create({
    // No baseURL needed - components use full paths like /api/topics/
    // 105s — deliberately ABOVE the backend's 90s REQUEST_TIMEOUT_SECONDS (+ its
    // pre-orchestrator work and network). The frontend clock starts earlier than
    // the backend's wall-clock, so an equal 90s here always aborted first and
    // surfaced the scary "heavy load" guess instead of the backend's graceful,
    // accurate "timed out after 90s" + request_id. Keep this strictly greater.
    timeout: 105_000,
});

// Request interceptor: attach token + request ID
/**
 * The admin impersonation headers, for every request — including the few that
 * use a raw `fetch` (chat streaming) instead of this
 * client. A raw `fetch` without them runs as the ADMIN while the page shows the
 * impersonated person: that is how a chat "as Jordan" answered about the
 * admin's own deals. A PERSON (read-only, no email/calendar/Drive/transcripts/
 * chat — enforced server-side) takes precedence over a department.
 */
export function impersonationHeaders(): Record<string, string> {
    if (readSupportToken()) return {};
    const impersonateUser = localStorage.getItem('impersonateUser');
    if (impersonateUser) return { 'X-Impersonate-User': impersonateUser };
    const impersonateDept = localStorage.getItem('impersonateDept');
    if (impersonateDept) return { 'X-Impersonate-Department': impersonateDept };
    return {};
}

client.interceptors.request.use(
    (config) => {
        // A known-expired support session returns to the home session; an
        // expired home session goes to sign-in. Reject before making a request.
        const token = activeToken();
        if (token && !isTokenValid(token)) {
            const rejected = dropRejectedToken(token);
            if (rejected === 'support') window.location.assign('/');
            else if (rejected === 'home' && window.location.pathname !== '/login') window.location.assign('/login');
            return Promise.reject(new Error('Session expired'));
        }
        if (token) {
            config.headers.Authorization = `Bearer ${token}`;
        }

        // Attach a unique request ID for traceability
        config.headers['X-Request-ID'] = crypto.randomUUID().slice(0, 8);

        // Apply admin impersonation context if it exists
        Object.assign(config.headers, impersonationHeaders());

        return config;
    },
    (error) => Promise.reject(error)
);

// Response interceptor: globally handle 401s.
//
// A 401 invalidates the exact token sent on that request. That matters when a
// late request from a revoked support session returns after the home session
// has been restored: it must never clear the home token.
client.interceptors.response.use(
    (response) => response,
    (error) => {
        const status: number | undefined = error?.response?.status;
        if (status === 401) {
            const header = error?.config?.headers?.Authorization ?? error?.config?.headers?.authorization;
            const sentToken = typeof header === 'string' && header.startsWith('Bearer ') ? header.slice(7) : '';
            const rejected = dropRejectedToken(sentToken);
            if (rejected === 'support') {
                window.location.assign('/');
            } else if (rejected === 'home' && window.location.pathname !== '/login') {
                window.location.assign('/login');
            }
        }
        return Promise.reject(error);
    },
);

export default client;
