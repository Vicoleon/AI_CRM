import assert from 'node:assert/strict';
import test from 'node:test';
import { compatibleRouteProvider } from './workspaceAIProviderSelection.ts';

const catalog = {
    anthropic: { 'claude-text': ['text'] },
    vertex_managed: { 'gemini-text': ['text'], 'gemini-json': ['json'] },
    openai: { 'gpt-json': ['json'] },
};

test('switching from text to JSON resolves an incompatible provider to managed Vertex', () => {
    assert.equal(compatibleRouteProvider(catalog, 'text', 'anthropic'), 'anthropic');
    assert.equal(compatibleRouteProvider(catalog, 'json', 'anthropic'), 'vertex_managed');
});

test('keeps a compatible selection and fails closed if no provider supports the workload', () => {
    assert.equal(compatibleRouteProvider(catalog, 'json', 'openai'), 'openai');
    assert.equal(compatibleRouteProvider(catalog, 'vision', 'anthropic'), '');
});
