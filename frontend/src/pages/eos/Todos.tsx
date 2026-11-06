import React, { useEffect, useState } from 'react';
import client from '../../api/client';
import { useVoiceChanges } from '../../components/voice/useVoiceChanges';
import type { PersonLite, TeamScope } from './_utils';
import { TodosView } from '../../components/eos/TodosView';
import type { Todo, TodoChange } from '../../components/eos/TodosView';

/**
 * Standalone To-Dos page. Thin host: it fetches the team's to-dos + people +
 * meeting scopes, owns the `todos` array, and reconciles the write results the
 * shared {@link TodosView} reports back. All rendering + the create/edit modal
 * live in TodosView, which the in-meeting L10 "To-Do List" segment reuses.
 */
export const Todos: React.FC = () => {
    const [todos, setTodos] = useState<Todo[]>([]);
    const [people, setPeople] = useState<PersonLite[]>([]);
    const [teamScopes, setTeamScopes] = useState<TeamScope[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    // Reloads when a voice command changes something on this page.
    const voiceChanges = useVoiceChanges('/eos/todos');

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const [tRes, pRes, sRes] = await Promise.all([
                    client.get<Todo[]>('/api/eos/todos/'),
                    client.get<PersonLite[]>('/api/eos/people/'),
                    client.get<{ scopes: TeamScope[] }>('/api/eos/me/meeting-scopes'),
                ]);
                if (cancelled) return;
                setTodos(tRes.data);
                setPeople(pRes.data);
                setTeamScopes(sRes.data.scopes ?? []);
                setError(null);
            } catch (err) {
                if (!cancelled) setError(err instanceof Error ? err.message : 'Unknown error');
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [voiceChanges]);

    const applyChange = (c: TodoChange) => {
        setTodos(prev => {
            switch (c.type) {
                case 'upsert':
                    return prev.some(t => t.id === c.todo.id)
                        ? prev.map(t => (t.id === c.todo.id ? c.todo : t))
                        : [...prev, c.todo];
                case 'remove':
                    return prev.filter(t => t.id !== c.id);
                case 'set-status':
                    return prev.map(t => (t.id === c.id ? { ...t, status: c.status } : t));
            }
        });
    };

    return (
        <TodosView
            mode="page"
            todos={todos}
            people={people}
            teamScopes={teamScopes}
            loading={loading}
            error={error}
            onChange={applyChange}
        />
    );
};
