/** Keep a multi-request save bound to the workspace session that started it. */
import { jwtDecode } from 'jwt-decode';

export function tokenMatchesWorkspace(token: string, slug: string, allowLocalMock = false): boolean {
    if (allowLocalMock && token === 'mock-token') return true;
    try {
        const claims = jwtDecode<{ ws?: unknown }>(token);
        return typeof claims.ws === 'string' && claims.ws === slug;
    } catch {
        return false;
    }
}

export interface WorkspaceRequestIdentity {
    slug: string;
    token: string;
    headers: Readonly<Record<string, string>>;
}

export type WorkspacePatch = (
    path: string,
    payload: unknown,
    identity: WorkspaceRequestIdentity,
) => Promise<{ data: unknown }>;

export interface WorkspaceSaveResult {
    operations: unknown | null;
    timeZone: unknown | null;
    cancelled: boolean;
}

export async function saveWorkspaceCrmChanges(
    patch: WorkspacePatch,
    identity: WorkspaceRequestIdentity,
    payload: Record<string, unknown>,
    isCurrent: () => boolean,
): Promise<{ data: unknown } | null> {
    if (!isCurrent()) return null;
    const response = await patch('/api/users/workspace/crm-settings', payload, identity);
    return isCurrent() ? response : null;
}

export async function saveWorkspaceChanges(
    patch: WorkspacePatch,
    identity: WorkspaceRequestIdentity,
    operationsPayload: Record<string, string | string[] | null> | null,
    timeZone: string | null,
    isCurrent: () => boolean,
): Promise<WorkspaceSaveResult> {
    let operations: unknown | null = null;
    let savedTimeZone: unknown | null = null;
    if (!isCurrent()) return { operations, timeZone: savedTimeZone, cancelled: true };

    if (operationsPayload) {
        operations = (await patch('/api/users/workspace/operations', operationsPayload, identity)).data;
    }
    // A support workspace switch can happen while the first request is pending.
    // Never send the second request after that switch, even with a pinned token.
    if (!isCurrent()) return { operations, timeZone: savedTimeZone, cancelled: true };

    if (timeZone !== null) {
        savedTimeZone = (await patch('/api/users/workspace/time-zone', { time_zone: timeZone }, identity)).data;
    }
    return { operations, timeZone: savedTimeZone, cancelled: !isCurrent() };
}
