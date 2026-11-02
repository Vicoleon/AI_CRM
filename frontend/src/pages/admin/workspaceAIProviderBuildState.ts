export type EmbeddingIndex = {
    storage: string;
    client_embeddings_available: boolean;
    corpora?: { key: string; label: string }[];
    selected_corpus?: string;
    active: { provider: string; model: string; dimension: number; version: string };
    candidate: { provider: string; model: string; dimension: number; version: string } | null;
    build?: {
        id?: string;
        status?: 'queued' | 'running' | 'ready' | 'failed' | 'stalled' | 'activated';
        completed_sources?: number;
        total_sources?: number | null;
        error?: string | null;
    } | null;
};

export type BuildMessage = {
    kind: 'success' | 'error';
    text: string;
    source?: 'poll';
};

/** The accepted Job is visible immediately, even if its first status read fails. */
export function queuedEmbeddingIndex(index: EmbeddingIndex | null, runId: string | undefined): EmbeddingIndex | null {
    if (!index) return null;
    return {
        ...index,
        build: { id: runId, status: 'queued', completed_sources: 0, total_sources: null, error: null },
    };
}

/** A recovered status read clears only an error caused by polling. */
export function clearRecoveredPollError(message: BuildMessage | null): BuildMessage | null {
    return message?.source === 'poll' ? null : message;
}
