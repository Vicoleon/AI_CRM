/** The only public reasons a signed-in Google account may lack a workspace. */
export type WorkspaceSetupReason = 'workspace_unknown' | 'workspace_personal' | 'workspace_suspended';

const SETUP_REASONS: readonly WorkspaceSetupReason[] = [
    'workspace_unknown',
    'workspace_personal',
    'workspace_suspended',
];

export function setupReason(body: unknown): WorkspaceSetupReason | null {
    if (!body || typeof body !== 'object') return null;
    const response = body as { code?: unknown; detail?: unknown };
    const detail = response.detail && typeof response.detail === 'object'
        ? response.detail as { code?: unknown }
        : null;
    const code = detail?.code ?? response.code;
    return typeof code === 'string' && SETUP_REASONS.includes(code as WorkspaceSetupReason)
        ? code as WorkspaceSetupReason
        : null;
}

function failureMessage(body: unknown): string {
    if (!body || typeof body !== 'object') return 'Sign-in failed. Please try again.';
    const detail = (body as { detail?: unknown }).detail;
    const message = typeof detail === 'string'
        ? detail
        : detail && typeof detail === 'object'
            ? (detail as { message?: unknown }).message
            : null;
    return typeof message === 'string' && message.trim()
        ? message.trim().slice(0, 240)
        : 'Sign-in failed. Please try again.';
}

export class SessionExchangeError extends Error {
    readonly status: number;
    readonly reason: WorkspaceSetupReason | null;

    constructor(
        message: string,
        status: number,
        reason: WorkspaceSetupReason | null,
    ) {
        super(message);
        this.name = 'SessionExchangeError';
        this.status = status;
        this.reason = reason;
    }
}

/** Exchange a Google credential for Cubie's own workspace-scoped session. */
export async function exchangeGoogleCredential(credential: string): Promise<string> {
    if (!credential) throw new SessionExchangeError('Sign-in failed. Please try again.', 0, null);
    const response = await fetch('/api/auth/session', {
        method: 'POST',
        headers: { Authorization: `Bearer ${credential}` },
    });
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) {
        throw new SessionExchangeError(
            failureMessage(body),
            response.status,
            response.status === 403 ? setupReason(body) : null,
        );
    }
    const token = body && typeof body === 'object' ? (body as { token?: unknown }).token : null;
    if (typeof token !== 'string' || !token) {
        throw new SessionExchangeError('Sign-in failed. Please try again.', response.status, null);
    }
    return token;
}
