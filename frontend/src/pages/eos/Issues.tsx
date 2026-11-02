import React, { useEffect, useState } from 'react';
import client from '../../api/client';
import { useVoiceChanges } from '../../components/voice/useVoiceChanges';
import type { PersonLite, TeamScope } from './_utils';
import { IssuesView } from '../../components/eos/IssuesView';
import type { Issue, IssueChange } from '../../components/eos/IssuesView';
import type { Todo, TodoChange } from '../../components/eos/TodosView';

/**
 * Standalone Issues (IDS) page. Thin host: it fetches the team's issues +
 * child to-dos + people + meeting scopes, owns the `issues` and `todos`
 * arrays, and reconciles the write results the shared {@link IssuesView}
 * reports back. All rendering + the create/edit modal + the "add to-do for
 * this issue" modal live in IssuesView, which the in-meeting L10 "IDS" segment
 * reuses to deliver the Issue→To-Do linkage.
 */
export const Issues: React.FC = () => {
    const [issues, setIssues] = useState<Issue[]>([]);
    const [todos, setTodos] = useState<Todo[]>([]);
    const [people, setPeople] = useState<PersonLite[]>([]);
    const [teamScopes, setTeamScopes] = useState<TeamScope[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    // Reloads when a voice command changes something on this page.
    const voiceChanges = useVoiceChanges('/eos/issues');

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const [iRes, sRes, pRes, tRes] = await Promise.all([
                    client.get<Issue[]>('/api/eos/issues/'),
                    client.get<{ scopes: TeamScope[] }>('/api/eos/me/meeting-scopes'),
                    client.get<PersonLite[]>('/api/eos/people/'),
                    client.get<Todo[]>('/api/eos/todos/'),
                ]);
                if (cancelled) return;
                setIssues(iRes.data);
                setTeamScopes(sRes.data.scopes ?? []);
                setPeople(pRes.data);
                setTodos(tRes.data);
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

    const applyIssueChange = (c: IssueChange) => {
        setIssues(prev => {
            switch (c.type) {
                case 'upsert':
                    return prev.some(i => i.id === c.issue.id)
                        ? prev.map(i => (i.id === c.issue.id ? c.issue : i))
                        : [...prev, c.issue];
                case 'remove':
                    return prev.filter(i => i.id !== c.id);
                case 'set':
                    return prev.map(i => (i.id === c.id ? { ...i, ...c.patch } : i));
            }
        });
    };

    // A child to-do spun off from an issue arrives here as an 'upsert' so the
    // dependency chip + child list pick it up (the Issue→To-Do link).
    const applyTodoChange = (c: TodoChange) => {
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
        <IssuesView
            mode="page"
            issues={issues}
            todos={todos}
            people={people}
            teamScopes={teamScopes}
            loading={loading}
            error={error}
            onIssueChange={applyIssueChange}
            onTodoChange={applyTodoChange}
        />
    );
};
