// Ask Cubie while an admin impersonates a person — HubSpot only, nothing saved.
//
// Rendered by Chat.tsx in place of the normal chat whenever `/api/users/me`
// reports an impersonation. It talks only to `POST /api/chat/impersonated`
// (backend/routers/chat_impersonated.py): two HubSpot tools, no sub-agents, no
// chat history written for the person. The conversation lives in this
// component's state and is gone when the page is left — follow-ups carry their
// context in `history`.
import React, { useEffect, useRef, useState } from 'react';
import { Bot, Send, ShieldCheck } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import client from '../api/client';

interface Turn {
    role: 'user' | 'assistant';
    content: string;
    error?: boolean;
}

const EXAMPLES = [
    'Which of my deals in a proposal or negotiation stage have been waiting on the decision-maker for more than 3 business days?',
    'Which deals closing this month need action today?',
    'Which of my deals have had their close date pushed more than twice?',
];

const errorMessage = (e: unknown): string => {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return typeof detail === 'string' && detail ? detail : 'Cubie could not answer just now. Try again.';
};

export const ImpersonatedChat: React.FC<{ targetName: string }> = ({ targetName }) => {
    const [turns, setTurns] = useState<Turn[]>([]);
    const [input, setInput] = useState('');
    const [busy, setBusy] = useState(false);
    const endRef = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        endRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [turns, busy]);

    const send = async (text: string) => {
        const query = text.trim();
        if (!query || busy) return;
        const history = turns.filter(t => !t.error).map(t => ({ role: t.role, content: t.content }));
        setTurns(prev => [...prev, { role: 'user', content: query }]);
        setInput('');
        setBusy(true);
        try {
            const { data } = await client.post<{ answer: string }>('/api/chat/impersonated', {
                query,
                history,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
            });
            setTurns(prev => [...prev, { role: 'assistant', content: data.answer }]);
        } catch (e) {
            setTurns(prev => [...prev, { role: 'assistant', content: errorMessage(e), error: true }]);
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="flex h-full flex-col bg-gray-50">
            <div className="border-b border-gray-200 bg-white px-6 py-4">
                <h1 className="text-lg font-semibold text-gray-900">Ask Cubie as {targetName}</h1>
                <p className="mt-1 flex items-start gap-2 text-sm text-gray-600">
                    <ShieldCheck size={16} className="mt-0.5 flex-shrink-0 text-green-700" aria-hidden="true" />
                    <span>
                        HubSpot only: deals, pipeline and contacts, answered as {targetName}. No email, calendar,
                        files or call transcripts, and nothing is saved to {targetName}&apos;s chat history. This
                        conversation disappears when you leave the page.
                    </span>
                </p>
            </div>

            <div className="flex-1 overflow-y-auto px-6 py-6">
                <div className="mx-auto max-w-3xl space-y-4">
                    {turns.length === 0 && (
                        <div className="space-y-2">
                            <p className="text-sm text-gray-500">Try one of these:</p>
                            {EXAMPLES.map(q => (
                                <button
                                    key={q}
                                    type="button"
                                    onClick={() => send(q)}
                                    className="block w-full rounded-lg border border-gray-200 bg-white px-4 py-3 text-left text-sm text-gray-700 hover:border-brand"
                                >
                                    {q}
                                </button>
                            ))}
                        </div>
                    )}
                    {turns.map((t, i) => (
                        <div key={i} className={`flex ${t.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                            <div
                                className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm ${
                                    t.role === 'user'
                                        ? 'rounded-tr-sm bg-brand text-white'
                                        : t.error
                                          ? 'rounded-tl-sm border border-amber-200 bg-amber-50 text-amber-900'
                                          : 'rounded-tl-sm border border-gray-100 bg-white text-gray-800'
                                }`}
                            >
                                {t.role === 'assistant' && !t.error ? (
                                    <div className="prose prose-sm max-w-none prose-ol:list-decimal prose-ul:list-disc prose-li:my-0.5">
                                        <ReactMarkdown
                                            remarkPlugins={[remarkGfm]}
                                            urlTransform={url => (/^(https?:|mailto:|tel:|#|\/)/i.test(url) ? url : '')}
                                        >
                                            {t.content}
                                        </ReactMarkdown>
                                    </div>
                                ) : (
                                    <p className="whitespace-pre-wrap">{t.content}</p>
                                )}
                            </div>
                        </div>
                    ))}
                    {busy && (
                        <div className="flex items-center gap-2 text-sm text-gray-500" role="status">
                            <Bot size={16} aria-hidden="true" /> Checking HubSpot…
                        </div>
                    )}
                    <div ref={endRef} />
                </div>
            </div>

            <form
                className="border-t border-gray-200 bg-white px-6 py-4"
                onSubmit={e => {
                    e.preventDefault();
                    send(input);
                }}
            >
                <div className="mx-auto flex max-w-3xl items-end gap-2">
                    <textarea
                        value={input}
                        onChange={e => setInput(e.target.value)}
                        onKeyDown={e => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                                e.preventDefault();
                                send(input);
                            }
                        }}
                        rows={2}
                        placeholder={`Ask about ${targetName}'s deals…`}
                        aria-label={`Ask about ${targetName}'s deals`}
                        className="flex-1 resize-none rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none"
                    />
                    <button
                        type="submit"
                        disabled={busy || !input.trim()}
                        aria-label="Send"
                        className="rounded-lg bg-brand p-3 text-white disabled:opacity-40"
                    >
                        <Send size={16} aria-hidden="true" />
                    </button>
                </div>
            </form>
        </div>
    );
};
