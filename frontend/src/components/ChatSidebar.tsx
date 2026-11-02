import React, { useState, useEffect } from 'react';
import { PlusCircle, MessageSquare, Trash2, Clock } from 'lucide-react';
import client from '../api/client';
import { formatDistanceToNow } from 'date-fns';
import { parseInstant } from '../utils/datetime';

interface ChatSession {
    id: string;
    title: string;
    updated_at: string;
}

interface ChatSidebarProps {
    activeSessionId: string | null;
    onSessionSelect: (sessionId: string) => void;
    onNewChat: () => void;
    refreshTrigger: number;
}

const ChatSidebar: React.FC<ChatSidebarProps> = ({
    activeSessionId,
    onSessionSelect,
    onNewChat,
    refreshTrigger
}) => {
    const [sessions, setSessions] = useState<ChatSession[]>([]);
    const [isLoading, setIsLoading] = useState(true);

    const fetchSessions = async () => {
        try {
            const { data } = await client.get('/api/chat/sessions');
            setSessions(data);
        } catch (error) {
            console.error("Failed to fetch sessions:", error);
        } finally {
            setIsLoading(false);
        }
    };

    useEffect(() => {
        fetchSessions();
    }, [refreshTrigger]);

    const handleDelete = async (e: React.MouseEvent, sessionId: string) => {
        e.stopPropagation();
        if (!window.confirm("Are you sure you want to delete this conversation?")) return;

        try {
            await client.delete(`/api/chat/sessions/${sessionId}`);
            setSessions(prev => prev.filter(s => s.id !== sessionId));
            if (activeSessionId === sessionId) {
                onNewChat();
            }
        } catch (error) {
            console.error("Failed to delete session:", error);
        }
    };

    return (
        <div className="w-64 bg-white border-r border-gray-100 flex flex-col h-full shadow-sm">
            <div className="p-4 border-b border-gray-50">
                <button
                    onClick={onNewChat}
                    className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-brand text-white rounded-lg text-sm font-medium hover:bg-brand-hover transition-all shadow-sm"
                >
                    <PlusCircle size={16} />
                    New Chat
                </button>
            </div>

            <div className="flex-1 overflow-y-auto">
                <div className="p-2 space-y-1">
                    <h3 className="px-3 py-2 text-[10px] font-bold text-gray-400 uppercase tracking-widest">Recent Chats</h3>

                    {isLoading ? (
                        <div className="px-3 py-4 space-y-3">
                            {[1, 2, 3].map(i => (
                                <div key={i} className="h-10 bg-gray-50 rounded-lg animate-pulse" />
                            ))}
                        </div>
                    ) : sessions.length === 0 ? (
                        <div className="px-3 py-10 text-center text-gray-400">
                            <MessageSquare size={24} className="mx-auto mb-2 opacity-20" />
                            <p className="text-xs">No conversations yet</p>
                        </div>
                    ) : (
                        sessions.map((session) => (
                            <div
                                key={session.id}
                                onClick={() => onSessionSelect(session.id)}
                                className={`group relative p-3 rounded-lg cursor-pointer transition-all ${activeSessionId === session.id
                                    ? 'bg-brand-tint text-brand'
                                    : 'hover:bg-gray-50 text-gray-700'
                                    }`}
                            >
                                <div className="pr-6">
                                    <p className="text-sm font-medium truncate leading-tight">
                                        {session.title || "Untitled Chat"}
                                    </p>
                                    <div className="flex items-center gap-1 mt-1 text-[10px] opacity-60">
                                        <Clock size={10} />
                                        {/* `parseInstant`, not `new Date`: the API
                                            sends naive UTC, which made a chat you
                                            sent a minute ago read "7 hours ago". */}
                                        {formatDistanceToNow(
                                            parseInstant(session.updated_at) ?? new Date(),
                                            { addSuffix: true },
                                        )}
                                    </div>
                                </div>

                                <button
                                    onClick={(e) => handleDelete(e, session.id)}
                                    className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 text-gray-400 hover:text-red-600 opacity-0 group-hover:opacity-100 transition-opacity"
                                >
                                    <Trash2 size={14} />
                                </button>
                            </div>
                        ))
                    )}
                </div>
            </div>
        </div>
    );
};

export default ChatSidebar;
