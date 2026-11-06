import assert from 'node:assert/strict';
import test from 'node:test';
import { clearRecoveredPollError, queuedEmbeddingIndex } from './workspaceAIProviderBuildState.ts';
import type { EmbeddingIndex } from './workspaceAIProviderBuildState.ts';

const index: EmbeddingIndex = {
    storage: 'cloud_sql_pgvector',
    client_embeddings_available: true,
    corpora: [{ key: 'private_uploads', label: 'Private uploads' }],
    selected_corpus: 'private_uploads',
    active: { provider: 'vertex_managed', model: 'old', dimension: 768, version: 'old-version' },
    candidate: { provider: 'openai', model: 'old-candidate', dimension: 1536, version: 'candidate' },
};

test('accepted cloud rebuild exposes a queued run before any status GET succeeds', () => {
    const queued = queuedEmbeddingIndex(index, 'job-123');
    assert.equal(queued?.build?.status, 'queued');
    assert.equal(queued?.build?.id, 'job-123');
    assert.equal(queued?.build?.completed_sources, 0);
    assert.deepEqual(queued?.active, index.active);
    assert.deepEqual(queued?.corpora, index.corpora);
    assert.equal(index.build, undefined);
});

test('a successful status poll clears its transient error but preserves other messages', () => {
    assert.equal(clearRecoveredPollError({ kind: 'error', text: 'Temporary timeout', source: 'poll' }), null);
    assert.deepEqual(
        clearRecoveredPollError({ kind: 'success', text: 'Rebuild started.' }),
        { kind: 'success', text: 'Rebuild started.' },
    );
    assert.deepEqual(
        clearRecoveredPollError({ kind: 'error', text: 'Credential rejected' }),
        { kind: 'error', text: 'Credential rejected' },
    );
});
