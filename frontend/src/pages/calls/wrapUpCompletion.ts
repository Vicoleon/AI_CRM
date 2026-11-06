import type { WrapUpSession } from '../../api/calls';

interface CompletionInput {
    session: Pick<WrapUpSession, 'status' | 'nothing_here'> | null;
    selectable: number;
    analysisUnavailable: boolean;
    outcomeRecorded: boolean;
    unresolved: number;
}

export function isWrapUpComplete({
    session,
    selectable,
    analysisUnavailable,
    outcomeRecorded,
    unresolved,
}: CompletionInput): boolean {
    // A call may already be wrapped when late transcript analysis fails. The
    // original review stays recorded, but its new review is still waiting.
    if (analysisUnavailable) return false;
    return (
        (outcomeRecorded && unresolved === 0) ||
        Boolean(session?.nothing_here) ||
        (session?.status === 'wrapped' && selectable === 0)
    );
}
